"""Local FTP Server Daemon background thread service.

Starts pyftpdlib listening on FTP_SERVER_PORT (default 21) when FastAPI ASGI starts.
Saves incoming files into uploads/ftp_dropzone for ftp_watcher.py to ingest.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from helpers.get_env import get_env
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

logger = logging.getLogger(__name__)

# Singleton thread control
_ftp_server_thread: threading.Thread | None = None
_ftp_server_lock = threading.Lock()
_ftpd_instance: FTPServer | None = None


def _run_ftp_server_loop() -> None:
    """Initialize pyftpdlib authorizer, handler, and serve forever."""
    global _ftpd_instance

    try:
        host = get_env("FTP_SERVER_HOST", default="0.0.0.0", required=False)
        port = int(get_env("FTP_SERVER_PORT", default="21", required=False))
        user = get_env("FTP_SERVER_USER", default="admin", required=False)
        password = get_env("FTP_SERVER_PASSWORD", default="password123", required=False)
        masquerade_address = get_env("ADDRESS", default=None, required=False)

        upload_root = Path(
            get_env(
                "UPLOAD_STORAGE_DIR",
                default=str(Path(__file__).resolve().parents[2] / "uploads"),
                required=False,
            )
        )
        dropzone_dir_name = get_env("FTP_DROPZONE_PATH", default="ftp_dropzone", required=False)
        dropzone = upload_root / dropzone_dir_name
        dropzone.mkdir(parents=True, exist_ok=True)

        authorizer = DummyAuthorizer()
        authorizer.add_user(user, password, str(dropzone), perm="elradfmw")
        authorizer.add_anonymous(str(dropzone), perm="elradfmw")

        handler = FTPHandler
        handler.authorizer = authorizer
        handler.banner = "File Ingestion Local FTP Server Ready."

        if masquerade_address and masquerade_address != "0.0.0.0":
            handler.masquerade_address = masquerade_address

        _ftpd_instance = FTPServer((host, port), handler)
        logger.info("Local FTP Server Daemon listening on %s:%s (Dropzone: %s)", host, port, dropzone)
        _ftpd_instance.serve_forever()
    except OSError as exc:
        if getattr(exc, "errno", None) in (10048, 98) or "10048" in str(exc):
            logger.info("Local FTP Server Daemon is already active on port %s.", port)
        else:
            logger.warning("Local FTP Server Daemon could not start on port %s: %s", port, exc)
    except Exception as exc:
        logger.warning("Local FTP Server Daemon could not start on port %s: %s", port, exc)


def start_ftp_server() -> threading.Thread | None:
    """Start the local FTP server daemon thread if enabled and not already running.

    Returns:
        threading.Thread | None: The active daemon thread.
    """
    global _ftp_server_thread
    with _ftp_server_lock:
        if _ftp_server_thread is not None and _ftp_server_thread.is_alive():
            return _ftp_server_thread

        _ftp_server_thread = threading.Thread(
            target=_run_ftp_server_loop,
            name="local-ftp-server-daemon",
            daemon=True,
        )
        _ftp_server_thread.start()
        logger.info("Local FTP Server daemon thread launched")
        return _ftp_server_thread
