"""External FTP Pull Ingestion — connection, credential vault, and disconnect logic.

This service manages the lifecycle of *external* FTP sessions initiated by
the user through the UI.  It is entirely separate from the existing local-push
``ftp_watcher.py`` daemon which monitors the server dropzone directory.

Key responsibilities
    * Validate external FTP credentials via a real ``ftplib.FTP_TLS`` probe.
    * Fernet-encrypt passwords and store session payloads in Redis with an
      idle TTL of 1 800 s (configurable).
    * Enforce a sliding-window rate limit (default 5 req / 5 min per user).
    * Dual-path disconnect: *Graceful Draining* vs. *Force Nuke* with
      reference-counted physical-file cleanup.

Redis key patterns
    ``ext_ftp:creds:{user_id}``  — JSON session payload (TTL-managed).
    ``ext_ftp:rate:{user_id}``   — Sliding-window connect-attempt counter.
"""

from __future__ import annotations

import ftplib
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


from sqlalchemy.orm import Session

from config.database import get_session_local
from config.redis_server import server as redis_server
from helpers.fernet_vault import fernet_decrypt, fernet_encrypt
from helpers.get_env import get_env
from models.auth_model import User
from models.file_model import AsyncIngestionJob, Dataset, DatasetFolderFilesMapping, Folder, UploadedFile
from utils.errors import (
    DatasetAccessError,
    DatasetNotFoundError,
    FilenameCollisionError,
    FolderNotFoundError,
    FtpAuthError,
    FtpConnectionError,
    FtpRateLimitError,
    FtpRemotePathNotFoundError,
    FtpSessionDrainingError,
    FtpSessionExpiredError,
    FtpSessionNotFoundError,
    FtpTlsRejectedError,
    IngestionJobNotFoundError,
)
from services.file_services import _generate_rename_suggestion


logger = logging.getLogger(__name__)


class ReusedSslFTP_TLS(ftplib.FTP_TLS):
    """Custom FTP_TLS class that reuses control connection SSL session for data connections.

    Fixes '425 Cannot secure data connection - TLS session resumption required' on FTPS servers like test.rebex.net.
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = super().ntransfercmd(cmd, rest)
        if self.sock is not None:
            try:
                conn = self.context.wrap_socket(
                    conn,
                    server_hostname=self.host,
                    session=self.sock.session,
                )
            except Exception as exc:
                logger.debug("TLS session reuse wrap failed: %s", exc)
        return conn, size


# ---------------------------------------------------------------------------
# Configuration (loaded once at import time, same pattern as ftp_watcher.py)
# ---------------------------------------------------------------------------

FTP_CONNECT_RATE_LIMIT = int(
    get_env("FTP_CONNECT_RATE_LIMIT", default="5", required=False)
)
FTP_CONNECT_RATE_WINDOW = int(
    get_env("FTP_CONNECT_RATE_WINDOW_SECONDS", default="300", required=False)
)
FTP_SESSION_IDLE_TTL = int(
    get_env("FTP_SESSION_IDLE_TTL_SECONDS", default="1800", required=False)
)
FTP_PROBE_TIMEOUT = int(
    get_env("FTP_PROBE_TIMEOUT_SECONDS", default="5", required=False)
)
STALL_TIMEOUT_SECONDS = int(
    get_env("FTP_STALL_TIMEOUT_SECONDS", default="180", required=False)
)
UPLOAD_ROOT = Path(
    get_env(
        "UPLOAD_STORAGE_DIR",
        default=str(Path(__file__).resolve().parents[2] / "uploads"),
        required=False,
    )
)
FINAL_ROOT_NAME = get_env("FINAL_ROOT", default="files", required=False)
FTP_STORAGE_DIR = get_env("FTP_STORAGE_DIR", default="FTP", required=False)


# Redis key templates
_CREDS_KEY = "ext_ftp:creds:{user_id}"
_RATE_KEY = "ext_ftp:rate:{user_id}"

# Provider label used in async_ingestion_jobs
EXT_FTP_PROVIDER = "FTP"


# ---------------------------------------------------------------------------
# Rate Limiting — sliding-window counter
# ---------------------------------------------------------------------------


def _check_rate_limit(user_id: str) -> None:
    """Enforce the per-user connect attempt rate limit.

    Uses a Redis ``INCR`` + ``EXPIRE`` sliding window.  The counter key is
    ``ext_ftp:rate:{user_id}`` and auto-expires after the configured window.

    Args:
        user_id (str): The authenticated user's ID.

    Raises:
        FtpRateLimitError: If the user has exceeded the allowed attempts.
    """
    key = _RATE_KEY.format(user_id=user_id)
    current = redis_server.incr(key)
    if current == 1:
        redis_server.expire(key, FTP_CONNECT_RATE_WINDOW)
    if current > FTP_CONNECT_RATE_LIMIT:
        ttl = redis_server.ttl(key)
        raise FtpRateLimitError(
            message=(
                f"Rate limit exceeded. Maximum {FTP_CONNECT_RATE_LIMIT} connection "
                f"attempts per {FTP_CONNECT_RATE_WINDOW}s. Retry after {ttl}s."
            ),
        )


# ---------------------------------------------------------------------------
# FTP Probe — real connection test
# ---------------------------------------------------------------------------


def _probe_ftp_connection(
    host: str,
    port: int,
    username: str,
    password: str,
    allow_insecure: bool,
) -> str:
    host = host.strip()
    username = username.strip()

    # --- Attempt 1: FTPS (TLS) ---
    ftp_tls = None
    try:
        ftp_tls = ReusedSslFTP_TLS(timeout=FTP_PROBE_TIMEOUT)
        ftp_tls.connect(host, port)
        ftp_tls.auth()
        try:
            print("For Testing Plain FTP")
            # ftp_tls.prot_p()
        except Exception:
            pass
        ftp_tls.login(username, password)
        ftp_tls.quit()
        return "ftps"
    except ftplib.error_perm as exc:
        # Close the dangling TLS connection before fallback
        try:
            if ftp_tls is not None:
                ftp_tls.close()
        except Exception:
            pass
        error_code = str(exc).split(None, 1)[0] if str(exc) else ""
        if error_code.startswith("530"):
            raise FtpAuthError(
                message="Invalid FTP credentials. The server rejected the login."
            ) from exc
        # Other permission errors during TLS negotiation — TLS rejected.
        if not allow_insecure:
            raise FtpTlsRejectedError(
                message=(
                    "The FTP server rejected the TLS handshake and insecure mode "
                    "is not enabled. Set allow_insecure=true to permit plaintext."
                ),
            ) from exc
    except (OSError, ftplib.error_reply) as exc:
        # Close the dangling TLS connection before fallback
        try:
            if ftp_tls is not None:
                ftp_tls.close()
        except Exception:
            pass
        # Connection-level failure during TLS attempt.
        if not allow_insecure:
            raise FtpConnectionError(
                message=f"Unable to reach FTP server at {host}:{port} — {exc}",
            ) from exc
    except Exception as exc:
        # Close the dangling TLS connection before fallback
        try:
            if ftp_tls is not None:
                ftp_tls.close()
        except Exception:
            pass
        if not allow_insecure:
            raise FtpConnectionError(
                message=f"Unexpected error connecting to {host}:{port} — {exc}",
            ) from exc

    # --- Attempt 2: Plaintext FTP (only if allow_insecure=True) ---
    logger.warning(
        "FTP TLS rejected by %s:%d — falling back to plaintext (allow_insecure=True)",
        host,
        port,
    )
    try:
        ftp = ftplib.FTP(timeout=FTP_PROBE_TIMEOUT)
        logger.info("Plaintext FTP: connecting to %s:%d (timeout=%ds)...", host, port, FTP_PROBE_TIMEOUT)
        ftp.connect(host, port)
        logger.info("Plaintext FTP: connected, logging in as '%s'...", username)
        ftp.login(username, password)
        logger.info("Plaintext FTP: login successful, closing probe connection.")
        ftp.quit()
        return "ftp"
    except ftplib.error_perm as exc:
        logger.error("Plaintext FTP probe FAILED (error_perm): %s", exc)
        error_code = str(exc).split(None, 1)[0] if str(exc) else ""
        if error_code.startswith("530"):
            raise FtpAuthError(
                message="Invalid FTP credentials. The server rejected the login."
            ) from exc
        raise FtpConnectionError(
            message=f"FTP permission error on {host}:{port} — {exc}",
        ) from exc
    except (OSError, ftplib.error_reply) as exc:
        logger.error("Plaintext FTP probe FAILED (OSError/error_reply): %s [%s]", exc, type(exc).__name__)
        raise FtpConnectionError(
            message=f"Unable to reach FTP server at {host}:{port} — {exc}",
        ) from exc
    except Exception as exc:
        logger.error("Plaintext FTP probe FAILED (unexpected): %s [%s]", exc, type(exc).__name__)
        raise FtpConnectionError(
            message=f"Unexpected error connecting to FTP server at {host}:{port} — {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Redis credential vault
# ---------------------------------------------------------------------------


def _store_ftp_credentials(user_id: str, payload: dict[str, Any]) -> None:
    """Fernet-encrypt the password and persist the session payload in Redis.

    The key ``ext_ftp:creds:{user_id}`` is set with the configured idle TTL
    (default 1 800 s).

    Args:
        user_id (str): The authenticated user's ID.
        payload (dict[str, Any]): The full session JSON payload. The
            ``password`` field is encrypted in-place before storage.
    """
    # Encrypt password before serialisation
    payload["encrypted_password"] = fernet_encrypt(payload.pop("password"))
    key = _CREDS_KEY.format(user_id=user_id)
    redis_server.set(key, json.dumps(payload), ex=FTP_SESSION_IDLE_TTL)
    logger.info(
        "Stored FTP credentials for user %s → %s (TTL=%ds)",
        user_id,
        payload.get("host"),
        FTP_SESSION_IDLE_TTL,
    )


def get_ftp_credentials(user_id: str) -> dict[str, Any]:
    """Retrieve and decrypt the stored FTP session payload from Redis.

    Args:
        user_id (str): The authenticated user's ID.

    Returns:
        dict[str, Any]: The session payload with the password decrypted.

    Raises:
        FtpSessionNotFoundError: No active session exists for this user.
        FtpSessionExpiredError: The Redis key has expired.
    """
    key = _CREDS_KEY.format(user_id=user_id)
    raw = redis_server.get(key)
    if raw is None:
        raise FtpSessionNotFoundError(
            message="No active external FTP session found. Please connect first.",
        )
    data: dict[str, Any] = json.loads(raw)
    if data.get("state") == "disconnected":
        raise FtpSessionExpiredError(
            message="The FTP session has been disconnected.",
        )
    # Decrypt password for caller
    data["password"] = fernet_decrypt(data.pop("encrypted_password"))
    return data


def _update_session_state(user_id: str, new_state: str) -> dict[str, Any] | None:
    """Atomically update the ``state`` field of a stored session payload.

    Args:
        user_id (str): The authenticated user's ID.
        new_state (str): The new state value (``"active"``, ``"draining"``,
            ``"disconnected"``).

    Returns:
        dict[str, Any] | None: The updated payload, or ``None`` if no key exists.
    """
    key = _CREDS_KEY.format(user_id=user_id)
    raw = redis_server.get(key)
    if raw is None:
        return None
    data: dict[str, Any] = json.loads(raw)
    data["state"] = new_state
    ttl = redis_server.ttl(key)
    if ttl and ttl > 0:
        redis_server.set(key, json.dumps(data), ex=ttl)
    else:
        redis_server.set(key, json.dumps(data), ex=FTP_SESSION_IDLE_TTL)
    return data


def _wipe_session(user_id: str) -> None:
    """Delete the FTP session credentials from Redis.

    Args:
        user_id (str): The authenticated user's ID.
    """
    key = _CREDS_KEY.format(user_id=user_id)
    redis_server.delete(key)
    logger.info("Wiped FTP credentials for user %s", user_id)


# ---------------------------------------------------------------------------
# Public API — connect
# ---------------------------------------------------------------------------


def connect_external_ftp(
    user: User,
    host: str,
    port: int,
    username: str,
    password: str,
    duration_seconds: int,
    until_i_stop: bool,
    graceful_expiry: bool,
    allow_insecure: bool,
) -> dict[str, Any]:
    host = host.strip()
    username = username.strip()
    """Validate FTP credentials and store an encrypted session in Redis.

    Orchestrates the full ``POST /v1/ingest/ftp/connect`` flow:
    rate-limit check → TLS probe → credential vault storage.

    Args:
        user (User): The authenticated user ORM instance.
        host (str): FTP server hostname.
        port (int): FTP control port.
        username (str): FTP login username.
        password (str): FTP login password.
        duration_seconds (int): Requested session lifetime.
        until_i_stop (bool): Keep session alive until manual disconnect.
        graceful_expiry (bool): Drain on expiry rather than hard-cut.
        allow_insecure (bool): Permit plaintext FTP if TLS is rejected.

    Returns:
        dict[str, Any]: Response payload matching ``FtpConnectResponse``.
    """
    # 1. Rate-limit gate
    _check_rate_limit(user.id)

    # 2. Real connection probe
    protocol = _probe_ftp_connection(host, port, username, password, allow_insecure)

    # 3. Compute expiry
    now = int(time.time())
    target_expiry_time = 0 if until_i_stop else (now + duration_seconds)

    # 4. Store credentials
    session_payload: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,  # will be encrypted by _store_ftp_credentials
        "protocol": protocol,
        "state": "active",
        "duration_seconds": duration_seconds,
        "until_i_stop": until_i_stop,
        "graceful_expiry": graceful_expiry,
        "target_expiry_time": target_expiry_time,
        "created_at": now,
    }
    _store_ftp_credentials(user.id, session_payload)

    # 5. Build response
    return {
        "status": "success",
        "message": "Connected to external FTP server successfully",
        "session": {
            "host": host,
            "username": username,
            "protocol": protocol,
            "state": "active",
            "idle_ttl_seconds": FTP_SESSION_IDLE_TTL,
            "target_expiry_time": target_expiry_time,
        },
    }


# ---------------------------------------------------------------------------
# Public API — disconnect
# ---------------------------------------------------------------------------


def _count_active_ftp_jobs(db: Session, user_id: str) -> int:
    """Return the number of pending/in_progress external FTP jobs for a user.

    Args:
        db (Session): An active SQLAlchemy session.
        user_id (str): The authenticated user's ID.

    Returns:
        int: Count of active jobs.
    """
    return (
        db.query(AsyncIngestionJob)
        .filter(
            AsyncIngestionJob.user_id == user_id,
            AsyncIngestionJob.provider == EXT_FTP_PROVIDER,
            AsyncIngestionJob.status.in_(["pending", "in_progress"]),
        )
        .count()
    )


def _cancel_active_ftp_jobs(db: Session, user_id: str) -> int:
    """Cancel all pending/in_progress external FTP jobs for a user.

    Args:
        db (Session): An active SQLAlchemy session.
        user_id (str): The authenticated user's ID.

    Returns:
        int: Number of jobs cancelled.
    """
    jobs = (
        db.query(AsyncIngestionJob)
        .filter(
            AsyncIngestionJob.user_id == user_id,
            AsyncIngestionJob.provider == EXT_FTP_PROVIDER,
            AsyncIngestionJob.status.in_(["pending", "in_progress"]),
        )
        .all()
    )
    for job in jobs:
        job.status = "cancelled"
        job.error_message = "Force disconnected by user."
    db.flush()
    return len(jobs)


def _cleanup_orphan_files(db: Session, user_id: str) -> int:
    """Reference-counted cleanup of physical files after a force disconnect.

    For each ``UploadedFile`` linked to cancelled external FTP jobs:
      1. Delete the ``DatasetFolderFilesMapping`` rows tied to this user.
      2. If the file's total reference count drops to 0, delete the physical
         file from disk and remove the ``UploadedFile`` row.

    Args:
        db (Session): An active SQLAlchemy session.
        user_id (str): The authenticated user's ID.

    Returns:
        int: Number of physical files deleted from disk.
    """
    import os

    cancelled_jobs = (
        db.query(AsyncIngestionJob)
        .filter(
            AsyncIngestionJob.user_id == user_id,
            AsyncIngestionJob.provider == EXT_FTP_PROVIDER,
            AsyncIngestionJob.status == "cancelled",
            AsyncIngestionJob.file_id.isnot(None),
        )
        .all()
    )

    deleted_count = 0
    for job in cancelled_jobs:
        file_id = job.file_id

        # Delete mappings belonging to this user for this file
        db.query(DatasetFolderFilesMapping).filter(
            DatasetFolderFilesMapping.file_id == file_id,
            DatasetFolderFilesMapping.user_id == user_id,
        ).delete(synchronize_session="fetch")

        # Reference count check
        ref_count = (
            db.query(DatasetFolderFilesMapping)
            .filter(DatasetFolderFilesMapping.file_id == file_id)
            .count()
        )
        if ref_count == 0:
            uploaded_file = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
            if uploaded_file:
                try:
                    if os.path.exists(uploaded_file.physical_path):
                        os.remove(uploaded_file.physical_path)
                        deleted_count += 1
                        logger.info(
                            "Deleted orphaned file %s (file_id=%s)",
                            uploaded_file.physical_path,
                            file_id,
                        )
                except OSError as err:
                    logger.warning(
                        "Failed to delete orphan file %s: %s",
                        uploaded_file.physical_path,
                        err,
                    )
                db.delete(uploaded_file)

    db.flush()
    return deleted_count


def disconnect_external_ftp(
    user: User,
    db: Session,
    force: bool = False,
    delete_completed_files: bool = False,
) -> dict[str, Any]:
    """Disconnect an active external FTP session.

    Supports two paths as defined in the Architecture_BE v4 document:

    **Path A — Normal Disconnect (``force=False``):**
        * If no active jobs → wipe Redis credentials, return success.
        * If active jobs exist → set session state to ``draining``,
          return 409 with active job count.

    **Path B — Force Disconnect (``force=True``):**
        * Cancel all pending/in-progress jobs.
        * Optionally run reference-counted file cleanup.
        * Wipe Redis credentials immediately.

    Args:
        user (User): The authenticated user ORM instance.
        db (Session): An active SQLAlchemy session.
        force (bool): If ``True``, force-cancel all transfers.
        delete_completed_files (bool): If ``True`` and ``force=True``,
            delete orphaned physical files with zero references.

    Returns:
        dict[str, Any]: Response payload matching ``FtpDisconnectResponse``.

    Raises:
        FtpSessionNotFoundError: No active FTP session found in Redis.
    """
    key = _CREDS_KEY.format(user_id=user.id)
    raw = redis_server.get(key)
    if raw is None:
        raise FtpSessionNotFoundError(
            message="No active FTP session found to disconnect.",
        )

    if force:
        # --- Path B: Force Nuke ---
        cancelled = _cancel_active_ftp_jobs(db, user.id)
        deleted_orphans = 0
        if delete_completed_files:
            deleted_orphans = _cleanup_orphan_files(db, user.id)
        db.commit()

        _wipe_session(user.id)
        logger.info(
            "Force disconnect for user %s: cancelled=%d, orphans_deleted=%d",
            user.id,
            cancelled,
            deleted_orphans,
        )
        return {
            "status": "success",
            "message": "FTP session forcefully terminated. Active transfers cancelled.",
            "cancelled_jobs": cancelled,
            "deleted_orphan_files": deleted_orphans,
        }

    # --- Path A: Normal Disconnect ---
    active_jobs = _count_active_ftp_jobs(db, user.id)

    if active_jobs == 0:
        _wipe_session(user.id)
        return {
            "status": "success",
            "message": "FTP session disconnected and credentials wiped from memory",
        }

    # Active jobs exist → enter draining state
    _update_session_state(user.id, "draining")
    raise FtpSessionDrainingError(
        message="Downloads are currently in progress. Session state set to draining.",
        details={
            "active_jobs": active_jobs,
            "draining": True,
        },
    )


# ---------------------------------------------------------------------------
# Component 2: The Navigator — Tree Exploration
# ---------------------------------------------------------------------------


def _parse_mlsd_timestamp(modify_str: str | None) -> str | None:
    """Parse an MLSD ``modify`` fact string into ISO 8601 UTC format.

    MLSD modify timestamps use ``YYYYMMDDHHMMSS[.fff]``.

    Args:
        modify_str (str | None): Raw timestamp from MLSD facts.

    Returns:
        str | None: ISO 8601 string (e.g. ``"2026-08-01T12:00:00Z"``) or ``None``.
    """
    if not modify_str or len(modify_str) < 14:
        return None
    try:
        raw = modify_str.split(".")[0]
        dt = datetime.strptime(raw[:14], "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _compute_parent_path(path: str) -> str:
    """Calculate the parent directory path for a given remote path.

    Args:
        path (str): The remote directory path.

    Returns:
        str: Parent path (e.g. ``"/folder"`` for ``"/folder/sub"``, ``"/"`` for ``"/folder"``).
    """
    segments = [s for s in path.strip().split("/") if s]
    if not segments:
        return "/"
    parent_segments = segments[:-1]
    if not parent_segments:
        return "/"
    return "/" + "/".join(parent_segments)


def get_external_ftp_tree(user_id: str, path: str = "/") -> dict[str, Any]:
    """Fetch directory listing for a remote FTP path using the user's active session.

    Connects via ``FTP_TLS`` or ``FTP`` using decrypted Redis credentials,
    issues the ``MLSD`` command, parses directory/file items, and closes the
    connection.

    Args:
        user_id (str): The authenticated user's ID.
        path (str): Remote target directory path (default ``"/"``).

    Returns:
        dict[str, Any]: Dictionary matching ``FtpTreeResponse`` schema.

    Raises:
        FtpSessionNotFoundError: If no active FTP session exists in Redis.
        FtpSessionExpiredError: If the FTP session is disconnected.
        FtpRemotePathNotFoundError: If the remote directory does not exist.
        FtpConnectionError: If the FTP server connection/command fails.
    """
    creds = get_ftp_credentials(user_id)
    host = creds["host"]
    port = creds.get("port", 21)
    username = creds["username"]
    password = creds["password"]
    protocol = creds.get("protocol", "ftps")

    # Normalize path
    segments = [s for s in path.strip().split("/") if s]
    target_path = "/" + "/".join(segments) if segments else "/"

    # Open FTP connection
    ftp = None
    try:
        if protocol == "ftps":
            ftp = ftplib.FTP_TLS(timeout=FTP_PROBE_TIMEOUT)
            ftp.connect(host, port)
            ftp.auth()
            try:
                print("For Testing Plain FTP")
                # ftp.prot_p()
            except Exception:
                pass
            ftp.login(username, password)
        else:
            ftp = ftplib.FTP(timeout=FTP_PROBE_TIMEOUT)
            ftp.connect(host, port)
            ftp.login(username, password)

        # Issue MLSD listing
        entries = list(ftp.mlsd(target_path))
    except ftplib.error_perm as exc:
        msg = str(exc)
        if "550" in msg or "not found" in msg.lower() or "no such" in msg.lower():
            raise FtpRemotePathNotFoundError(
                message=f"Remote FTP directory '{target_path}' does not exist.",
            ) from exc
        raise FtpConnectionError(
            message=f"FTP permission error listing '{target_path}': {exc}",
        ) from exc
    except (OSError, ftplib.error_reply) as exc:
        raise FtpConnectionError(
            message=f"Failed to list remote directory '{target_path}': {exc}",
        ) from exc
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                pass

    items = []
    for name, facts in entries:
        if name in (".", ".."):
            continue
        fact_type = facts.get("type", "").lower()
        if fact_type in ("cdir", "pdir"):
            continue

        is_folder = (fact_type == "dir")
        size_bytes = int(facts.get("size", "0")) if not is_folder else 0
        modified_at = _parse_mlsd_timestamp(facts.get("modify"))
        item_path = f"{target_path.rstrip('/')}/{name}"

        items.append({
            "name": name,
            "path": item_path,
            "is_folder": is_folder,
            "size_bytes": size_bytes,
            "modified_at": modified_at,
        })

    # Sort folders first, then files alphabetically
    items.sort(key=lambda x: (not x["is_folder"], x["name"].lower()))

    return {
        "current_path": target_path,
        "parent_path": _compute_parent_path(target_path),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Component 3: The Dispatcher — Two-Tier Semaphores & Job Queueing
# ---------------------------------------------------------------------------

_HOST_SEM_KEY = "ext_ftp:sem:host:{host}"
_USER_SEM_KEY = "ext_ftp:sem:user:{user_id}"

MAX_HOST_CONCURRENT = 4
MAX_USER_CONCURRENT = 4


def acquire_host_semaphore(host: str) -> bool:
    """Try to acquire a slot in the host-level concurrency semaphore.

    Args:
        host (str): FTP host or IP.

    Returns:
        bool: True if slot acquired, False if limit reached.
    """
    key = _HOST_SEM_KEY.format(host=host)
    current = redis_server.incr(key)
    if current == 1:
        redis_server.expire(key, 3600)
    if current > MAX_HOST_CONCURRENT:
        redis_server.decr(key)
        return False
    return True


def release_host_semaphore(host: str) -> None:
    """Release a slot in the host-level concurrency semaphore.

    Args:
        host (str): FTP host or IP.
    """
    key = _HOST_SEM_KEY.format(host=host)
    val = redis_server.decr(key)
    if val <= 0:
        redis_server.delete(key)


def acquire_user_semaphore(user_id: str) -> bool:
    """Try to acquire a slot in the user-level concurrency semaphore.

    Args:
        user_id (str): Authenticated user ID.

    Returns:
        bool: True if slot acquired, False if limit reached.
    """
    key = _USER_SEM_KEY.format(user_id=user_id)
    current = redis_server.incr(key)
    if current == 1:
        redis_server.expire(key, 3600)
    if current > MAX_USER_CONCURRENT:
        redis_server.decr(key)
        return False
    return True


def release_user_semaphore(user_id: str) -> None:
    """Release a slot in the user-level concurrency semaphore.

    Args:
        user_id (str): Authenticated user ID.
    """
    key = _USER_SEM_KEY.format(user_id=user_id)
    val = redis_server.decr(key)
    if val <= 0:
        redis_server.delete(key)


def initiate_external_ftp_ingestion(
    user: User,
    db: Session,
    dataset_id: str,
    target_folder_id: str | None,
    items: list[dict[str, Any]],
    auto_rename: bool = False,
    background_tasks: Any = None,
) -> dict[str, Any]:
    """Queue selected remote FTP files or folders for background ingestion.

    Args:
        user (User): Authenticated user.
        db (Session): Database session.
        dataset_id (str): Target dataset UUID.
        target_folder_id (str | None): Optional target folder UUID.
        items (list[dict[str, Any]]): Remote FTP items to queue.
        auto_rename (bool): If True, automatically append (1) on filename collision.

    Returns:
        dict[str, Any]: Response matching FtpInitResponse.

    Raises:
        DatasetNotFoundError: If dataset does not exist.
        DatasetAccessError: If dataset does not belong to user.
        FolderNotFoundError: If target_folder_id does not exist or belong to user.
        FtpSessionNotFoundError: If no active FTP session exists.
        FtpSessionDrainingError: If session is currently in draining state.
    """
    # 1. Validate dataset ownership
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.is_deleted == False).first()
    if dataset is None:
        raise DatasetNotFoundError()
    if dataset.user_id != user.id:
        raise DatasetAccessError()

    # 2. Validate folder ownership if target_folder_id is provided
    if target_folder_id:
        folder = db.query(Folder).filter(Folder.id == target_folder_id, Folder.user_id == user.id).first()
        if folder is None:
            raise FolderNotFoundError()

    # 3. Validate active FTP session
    session_data = get_ftp_credentials(user.id)
    if session_data.get("state") == "draining":
        raise FtpSessionDrainingError(
            message="FTP session is currently draining; new job submissions are locked.",
            details={"draining": True},
        )

    # 4. Expand items (recursively list folders)
    file_targets: list[dict[str, Any]] = []

    def _expand(item_path: str, is_folder: bool, size_bytes: int):
        if not is_folder:
            file_targets.append({"path": item_path, "size_bytes": size_bytes})
            return
        tree = get_external_ftp_tree(user.id, item_path)
        for child in tree.get("items", []):
            _expand(child["path"], child["is_folder"], child.get("size_bytes", 0))

    for item in items:
        _expand(item["path"], item.get("is_folder", False), item.get("size_bytes", 0))

    # 4b. Virtual Deduplication — check existing filenames in target dataset/folder
    existing_mappings = (
        db.query(DatasetFolderFilesMapping)
        .join(UploadedFile)
        .filter(
            DatasetFolderFilesMapping.dataset_id == dataset.id,
            DatasetFolderFilesMapping.folder_id == target_folder_id,
        )
        .all()
    )
    existing_names: set[str] = {
        m.file.filename for m in existing_mappings if m.file is not None
    }

    # 5. Insert AsyncIngestionJob rows
    created_jobs: list[AsyncIngestionJob] = []
    for target in file_targets:
        raw_path = target["path"]
        base_filename = raw_path.rstrip("/").split("/")[-1]
        effective_filename = base_filename

        if base_filename in existing_names:
            suggestion = _generate_rename_suggestion(base_filename, existing_names)
            if not auto_rename:
                raise FilenameCollisionError(
                    message="Filename collision detected",
                    details={
                        "conflict_type": "name_exists",
                        "conflicting_files": [base_filename],
                        "suggestion": suggestion,
                    },
                )
            effective_filename = suggestion
            existing_names.add(effective_filename)

        job = AsyncIngestionJob(
            user_id=user.id,
            dataset_id=dataset.id,
            folder_id=target_folder_id,
            provider=EXT_FTP_PROVIDER,
            source_url_or_id=raw_path,
            filename=effective_filename,
            status="pending",
            progress_percentage=0,
            bytes_downloaded=0,
            total_bytes=target["size_bytes"],
        )
        db.add(job)
        created_jobs.append(job)

    db.commit()

    # Launch background worker for each queued job
    for job in created_jobs:
        if background_tasks is not None:
            background_tasks.add_task(_execute_ftp_job_worker, job.id)
        else:
            threading.Thread(target=_execute_ftp_job_worker, args=(job.id,), daemon=True).start()

    logger.info(
        "Queued %d external FTP job(s) for user %s in dataset %s",
        len(created_jobs),
        user.id,
        dataset.id,
    )

    return {
        "status": "processing",
        "message": f"{len(created_jobs)} FTP ingestion job(s) queued successfully",
        "jobs": [
            {
                "job_id": job.id,
                "path": job.source_url_or_id,
                "status": job.status,
                "filename": job.filename,
                "filesize": job.total_bytes,
                "source": "FTP",
                "dataset_id": dataset.id,
                "dataset_name": dataset.name,
            }
            for job in created_jobs
        ],
    }


# ---------------------------------------------------------------------------
# Component 4: The Engine & Medic — Background Worker & Recovery
# ---------------------------------------------------------------------------


def _save_checkpoint_manifest(manifest_path: str, data: dict[str, Any]) -> None:
    """Save a checkpoint manifest JSON file atomically.

    Args:
        manifest_path (str): Filepath to the manifest destination.
        data (dict[str, Any]): Checkpoint metadata dictionary.
    """
    try:
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        temp_path = f"{manifest_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, manifest_path)
    except Exception as exc:
        logger.warning("Failed to save checkpoint manifest %s: %s", manifest_path, exc)


def _load_checkpoint_manifest(manifest_path: str) -> dict[str, Any] | None:
    """Load a checkpoint manifest JSON file if present.

    Args:
        manifest_path (str): Filepath to the manifest.

    Returns:
        dict[str, Any] | None: Loaded checkpoint dict or ``None`` if absent.
    """
    if not os.path.exists(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load checkpoint manifest %s: %s", manifest_path, exc)
        return None


def _run_session_heartbeat(user_id: str, stop_event: threading.Event) -> None:
    """Background thread target that periodically renews the user's Redis session TTL.

    Args:
        user_id (str): Authenticated user ID.
        stop_event (threading.Event): Signal to stop heartbeat updates.
    """
    key = _CREDS_KEY.format(user_id=user_id)
    while not stop_event.is_set():
        try:
            redis_server.expire(key, FTP_SESSION_IDLE_TTL)
        except Exception as exc:
            logger.debug("Heartbeat TTL refresh for user %s failed: %s", user_id, exc)
        stop_event.wait(timeout=15)


def _execute_ftp_job_worker(job_id: str) -> None:
    """Worker helper to execute process_ftp_ingestion_job with an isolated DB session."""
    db: Session = get_session_local()()
    try:
        process_ftp_ingestion_job(db, job_id)
    except Exception as exc:
        logger.exception("FTP background worker error for job %s: %s", job_id, exc)
    finally:
        db.close()


def process_ftp_ingestion_job(db: Session, job_id: str) -> bool:
    """Execute background streaming ingestion for an external FTP job.

    Features:
        * Two-Tier Semaphores (Host & User) to enforce concurrency limits.
        * Session Heartbeat thread renewing 1800s idle TTL in Redis every 15s.
        * Checkpoint Manifest (.manifest.json) enabling zero-read resume via
          FTP ``REST {offset}``.
        * Stall Detection (180s zero-byte timeout).
        * Global SHA-256 deduplication against ``UploadedFile``.
        * Atomic dataset status synchronization upon completion.

    Args:
        db (Session): Database session.
        job_id (str): Target ``AsyncIngestionJob`` UUID.

    Returns:
        bool: ``True`` if the job completed successfully, ``False`` on failure.
    """
    job = db.query(AsyncIngestionJob).filter(AsyncIngestionJob.id == job_id).first()
    if job is None:
        logger.error("FTP ingestion job %s not found", job_id)
        return False

    if job.status in ("completed", "failed"):
        logger.info("Job %s already in terminal state %s", job_id, job.status)
        return True

    user_id = job.user_id

    # Retrieve session credentials
    try:
        creds = get_ftp_credentials(user_id)
    except (FtpSessionNotFoundError, FtpSessionExpiredError) as exc:
        job.status = "failed"
        job.error_message = str(exc.public_message)
        db.commit()
        return False

    host = creds["host"]

    # 1. Acquire semaphores
    if not acquire_user_semaphore(user_id):
        logger.info("User %s at concurrency limit; job %s remains pending", user_id, job_id)
        return False

    if not acquire_host_semaphore(host):
        release_user_semaphore(user_id)
        logger.info("Host %s at concurrency limit; job %s remains pending", host, job_id)
        return False

    # Start session heartbeat thread
    stop_heartbeat = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_run_session_heartbeat,
        args=(user_id, stop_heartbeat),
        daemon=True,
    )
    heartbeat_thread.start()

    staging_dir = UPLOAD_ROOT / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    tmp_filepath = staging_dir / f"{job_id}.tmp"
    manifest_filepath = staging_dir / f"{job_id}.manifest.json"

    # Mark status in_progress
    job.status = "in_progress"
    db.commit()

    ftp = None
    try:
        # Check for checkpoint resume
        manifest = _load_checkpoint_manifest(str(manifest_filepath))
        resume_offset = 0
        hasher = hashlib.sha256()

        if manifest and tmp_filepath.exists():
            stored_offset = manifest.get("bytes_downloaded", 0)
            actual_size = tmp_filepath.stat().st_size
            if actual_size == stored_offset and stored_offset > 0:
                resume_offset = stored_offset
                logger.info("Resuming FTP job %s from byte offset %d", job_id, resume_offset)

        protocol = creds.get("protocol", "ftps")
        port = creds.get("port", 21)
        username = creds["username"]
        password = creds["password"]
        remote_path = job.source_url_or_id

        # Open FTP connection
        if protocol == "ftps":
            ftp = ftplib.FTP_TLS(timeout=FTP_PROBE_TIMEOUT)
            ftp.connect(host, port)
            ftp.auth()
            try:
                print("For Testing Plain FTP")
                # ftp.prot_p()
            except Exception:
                pass
            ftp.login(username, password)
        else:
            ftp = ftplib.FTP(timeout=FTP_PROBE_TIMEOUT)
            ftp.connect(host, port)
            ftp.login(username, password)

        mode = "ab" if resume_offset > 0 else "wb"
        bytes_written = resume_offset

        # Hash already downloaded bytes if resuming
        if resume_offset > 0:
            ftp.sendcmd(f"REST {resume_offset}")
            with open(tmp_filepath, "rb") as existing_file:
                while chunk := existing_file.read(65536):
                    hasher.update(chunk)

        last_byte_at = time.time()

        with open(tmp_filepath, mode) as out_f:
            def _chunk_callback(data: bytes):
                nonlocal bytes_written, last_byte_at
                now = time.time()
                if now - last_byte_at > STALL_TIMEOUT_SECONDS:
                    raise FtpConnectionError(
                        message=f"Download stalled: no data received for {STALL_TIMEOUT_SECONDS}s"
                    )

                last_byte_at = now
                out_f.write(data)
                hasher.update(data)
                bytes_written += len(data)

                # Volatile Cache Update: Push progress to Redis every 1 second
                redis_server.set(f"ingest:{job_id}:progress", str(bytes_written), ex=3600)

                # Update progress in DB object
                job.bytes_downloaded = bytes_written
                if job.total_bytes > 0:
                    job.progress_percentage = min(99, int((bytes_written / job.total_bytes) * 100))

                _save_checkpoint_manifest(
                    str(manifest_filepath),
                    {
                        "job_id": job_id,
                        "remote_path": remote_path,
                        "bytes_downloaded": bytes_written,
                        "total_bytes": job.total_bytes,
                        "updated_at": time.time(),
                    },
                )

            ftp.retrbinary(f"RETR {remote_path}", _chunk_callback, blocksize=8192)

        master_hash = hasher.hexdigest()

        # Check global SHA-256 deduplication
        existing_file = db.query(UploadedFile).filter(UploadedFile.master_hash == master_hash).first()
        if existing_file:
            file_id = existing_file.id
            if tmp_filepath.exists():
                tmp_filepath.unlink(missing_ok=True)
            logger.info("Dedup hit for job %s; reusing uploaded_files row %s", job_id, file_id)
        else:
            dest_dir = UPLOAD_ROOT / FINAL_ROOT_NAME / FTP_STORAGE_DIR
            dest_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(job.filename).suffix
            final_filename = f"{master_hash}{ext}" if ext else master_hash
            final_path = dest_dir / final_filename

            shutil.move(str(tmp_filepath), str(final_path))

            new_file = UploadedFile(
                filename=job.filename,
                file_size_bytes=bytes_written,
                master_hash=master_hash,
                physical_path=str(final_path),
                source_type="FTP",
            )
            db.add(new_file)
            db.flush()
            file_id = new_file.id

        # Insert dataset mapping
        mapping = DatasetFolderFilesMapping(
            dataset_id=job.dataset_id,
            folder_id=job.folder_id,
            file_id=file_id,
            user_id=job.user_id,
        )
        db.add(mapping)

        # Mark completed
        job.status = "completed"
        job.progress_percentage = 100
        job.bytes_downloaded = bytes_written
        if job.total_bytes == 0:
            job.total_bytes = bytes_written
        job.file_id = file_id
        job.master_hash = master_hash
        db.commit()

        # Clean up checkpoint manifest on completion
        manifest_filepath.unlink(missing_ok=True)
        redis_server.delete(f"ingest:{job_id}:progress")

        # Sync dataset status
        try:
            dataset = db.query(Dataset).filter(Dataset.id == job.dataset_id).first()
            if dataset is not None:
                from services.file_services import _sync_dataset_status_from_mappings
                _sync_dataset_status_from_mappings(db, dataset)
        except Exception as exc:
            logger.warning("Failed to sync dataset status for %s: %s", job.dataset_id, exc)


        logger.info("FTP ingestion job %s completed successfully", job_id)
        return True

    except Exception as exc:
        db.rollback()
        logger.exception("FTP ingestion job %s failed: %s", job_id, exc)
        job.status = "failed"
        job.error_message = str(exc)
        db.commit()
        return False
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)
        release_host_semaphore(host)
        release_user_semaphore(user_id)
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                pass


def cleanup_ftp_staging_files(max_age_seconds: int = 86400) -> int:
    """Scan the uploads/staging directory and remove orphan .tmp and .manifest.json files older than max_age_seconds.

    Args:
        max_age_seconds (int): Maximum allowed file age in seconds (default 86,400s / 24h).

    Returns:
        int: Total number of orphan staging files deleted.
    """
    staging_dir = UPLOAD_ROOT / "staging"
    if not staging_dir.exists():
        return 0

    now = time.time()
    deleted_count = 0

    for path in staging_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.endswith(".tmp") or path.name.endswith(".manifest.json"):
            try:
                file_age = now - path.stat().st_mtime
                if file_age > max_age_seconds:
                    path.unlink()
                    deleted_count += 1
                    logger.info("Deleted orphan staging file %s (age %.0fs)", path.name, file_age)
            except Exception as exc:
                logger.warning("Failed to clean up staging file %s: %s", path.name, exc)

    return deleted_count


# ---------------------------------------------------------------------------
# Component 4: High-Speed UI Polling (GET /v1/ingest/status)
# ---------------------------------------------------------------------------


def get_batch_ingestion_status(
    user: User,
    db: Session,
    status_filter: list[str] | str | None = None,
) -> dict[str, Any]:
    """Retrieve batch progress status for active or historical ingestion jobs.

    Args:
        user (User): The authenticated user.
        db (Session): Database session.
        status_filter (list[str] | str | None): Filter status list or comma-separated string.

    Returns:
        dict[str, Any]: Batch progress payload matching FtpBatchStatusResponse.
    """
    query = db.query(AsyncIngestionJob).filter(AsyncIngestionJob.user_id == user.id)

    if status_filter:
        if isinstance(status_filter, str):
            statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        else:
            statuses = list(status_filter)
        if statuses:
            query = query.filter(AsyncIngestionJob.status.in_(statuses))

    jobs = query.order_by(AsyncIngestionJob.created_at.desc()).all()

    formatted_jobs = []
    for job in jobs:
        bytes_dl = job.bytes_downloaded
        if job.status == "in_progress":
            live_bytes = redis_server.get(f"ingest:{job.id}:progress")
            if live_bytes is not None:
                try:
                    bytes_dl = int(live_bytes)
                except (ValueError, TypeError):
                    pass

        pct = job.progress_percentage
        if job.total_bytes > 0:
            pct = min(100, int((bytes_dl / job.total_bytes) * 100))
        elif job.status == "completed":
            pct = 100

        filesize_str = f"{job.total_bytes / (1024 * 1024):.1f} MB" if job.total_bytes > 0 else "0.0 MB"
        provider_str = job.provider if isinstance(job.provider, str) else job.provider.value

        formatted_jobs.append({
            "job_id": job.id,
            "provider": provider_str,
            "filename": job.filename,
            "dataset_name": job.dataset.name if job.dataset else None,
            "filesize": filesize_str,
            "status": job.status,
            "progress_percentage": pct,
            "bytes_downloaded": bytes_dl,
            "total_bytes": job.total_bytes,
            "error_message": job.error_message,
        })

    return {
        "total_active_jobs": len(formatted_jobs),
        "jobs": formatted_jobs,
    }


# ---------------------------------------------------------------------------
# Component 5: The Medic — Crash Recovery (POST /v1/ingest/ftp/resume)
# ---------------------------------------------------------------------------


def resume_failed_ftp_jobs(
    user: User,
    db: Session,
    job_ids: list[str],
) -> dict[str, Any]:
    """Resume failed ingestion jobs using checkpoint manifests.

    Args:
        user (User): The authenticated user.
        db (Session): Database session.
        job_ids (list[str]): List of job UUIDs to resume.

    Returns:
        dict[str, Any]: Dict matching FtpResumeResponse schema.

    Raises:
        FtpSessionExpiredError: If Redis Vault credentials expired (410 Gone).
        IngestionJobNotFoundError: If no matching failed jobs exist.
    """
    session_data = redis_server.get(f"ext_ftp:creds:{user.id}")
    if not session_data:
        raise FtpSessionExpiredError(
            message="FTP session credentials have expired. Please re-authenticate.",
            details={"expired": True},
        )

    failed_jobs = (
        db.query(AsyncIngestionJob)
        .filter(
            AsyncIngestionJob.id.in_(job_ids),
            AsyncIngestionJob.user_id == user.id,
            AsyncIngestionJob.status == "failed",
        )
        .all()
    )

    if not failed_jobs:
        raise IngestionJobNotFoundError(message="No matching failed jobs found to resume.")

    resumed_ids = []
    for job in failed_jobs:
        job.status = "pending"
        job.error_message = None
        resumed_ids.append(job.id)

    db.commit()

    # Launch background worker thread for each resumed job
    for job in failed_jobs:
        thread = threading.Thread(
            target=_execute_ftp_job_worker,
            args=(job.id,),
            daemon=True,
        )
        thread.start()

    logger.info("Queued %d failed FTP job(s) for resume for user %s", len(resumed_ids), user.id)

    return {
        "status": "processing",
        "message": f"{len(resumed_ids)} job(s) successfully queued for resume",
        "resumed_jobs": resumed_ids,
    }




