"""Unit tests for the External FTP service (services/ext_ftp_service.py).

These tests mock Redis and ftplib to isolate the service logic from
external dependencies.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is importable
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
for p in (str(SERVER_ROOT), str(SRC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", "")


# ---------------------------------------------------------------------------
# Shared mock Redis
# ---------------------------------------------------------------------------


class _MockRedisForFtp:
    """Minimal Redis mock supporting the ext_ftp_service key patterns."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}
        self._counters: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self._store:
            return False
        self._store[key] = value
        if ex:
            self._ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def delete(self, *keys: str) -> int:
        deleted = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                deleted += 1
            self._ttls.pop(k, None)
            self._counters.pop(k, None)
        return deleted

    def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def decr(self, key: str) -> int:
        val = self._counters.get(key, 0) - 1
        self._counters[key] = max(0, val)
        return self._counters[key]


    def expire(self, key: str, ttl: int) -> bool:
        self._ttls[key] = ttl
        return True

    def ttl(self, key: str) -> int:
        return self._ttls.get(key, -1)

    def exists(self, key: str) -> int:
        return int(key in self._store)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_redis(monkeypatch):
    """Replace the real Redis instance with a mock for all tests in this module."""
    mock = _MockRedisForFtp()
    import services.ext_ftp_service as svc
    monkeypatch.setattr(svc, "redis_server", mock)
    return mock


@pytest.fixture()
def mock_redis(_patch_redis):
    """Expose the mock Redis instance to individual tests."""
    return _patch_redis


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for the sliding-window rate limiter."""

    def test_first_request_passes(self, mock_redis):
        """The very first connection attempt must not be rate-limited."""
        from services.ext_ftp_service import _check_rate_limit

        _check_rate_limit("user-1")  # should not raise

    def test_within_limit_passes(self, mock_redis):
        """Requests up to the limit (5) must pass without error."""
        from services.ext_ftp_service import _check_rate_limit

        for _ in range(5):
            _check_rate_limit("user-2")

    def test_exceeding_limit_raises_429(self, mock_redis):
        """The 6th request within the window must raise FtpRateLimitError."""
        from services.ext_ftp_service import _check_rate_limit
        from utils.errors import FtpRateLimitError

        for _ in range(5):
            _check_rate_limit("user-3")

        with pytest.raises(FtpRateLimitError):
            _check_rate_limit("user-3")

    def test_different_users_independent(self, mock_redis):
        """Rate limits are per-user — one user's exhaustion doesn't affect another."""
        from services.ext_ftp_service import _check_rate_limit

        for _ in range(5):
            _check_rate_limit("user-a")

        # user-b should be unaffected
        _check_rate_limit("user-b")  # should not raise


# ---------------------------------------------------------------------------
# Credential Vault
# ---------------------------------------------------------------------------


class TestCredentialVault:
    """Tests for Fernet-encrypted Redis credential storage and retrieval."""

    def test_store_and_retrieve(self, mock_redis):
        """Credentials stored via _store_ftp_credentials are retrievable and decrypted."""
        from services.ext_ftp_service import _store_ftp_credentials, get_ftp_credentials

        payload = {
            "host": "ftp.example.com",
            "port": 21,
            "username": "testuser",
            "password": "s3cret!",
            "protocol": "ftps",
            "state": "active",
        }
        _store_ftp_credentials("user-1", dict(payload))

        result = get_ftp_credentials("user-1")
        assert result["host"] == "ftp.example.com"
        assert result["username"] == "testuser"
        assert result["password"] == "s3cret!"
        assert result["protocol"] == "ftps"

    def test_password_is_encrypted_in_redis(self, mock_redis):
        """The raw Redis value must NOT contain the plaintext password."""
        from services.ext_ftp_service import _store_ftp_credentials, _CREDS_KEY

        _store_ftp_credentials("user-2", {
            "host": "ftp.example.com",
            "port": 21,
            "username": "user",
            "password": "plaintext_secret",
            "protocol": "ftps",
            "state": "active",
        })

        raw = mock_redis.get(_CREDS_KEY.format(user_id="user-2"))
        assert raw is not None
        assert "plaintext_secret" not in raw
        # The encrypted_password key should be present
        data = json.loads(raw)
        assert "encrypted_password" in data
        assert "password" not in data

    def test_retrieve_missing_session_raises(self, mock_redis):
        """get_ftp_credentials must raise FtpSessionNotFoundError for unknown user."""
        from services.ext_ftp_service import get_ftp_credentials
        from utils.errors import FtpSessionNotFoundError

        with pytest.raises(FtpSessionNotFoundError):
            get_ftp_credentials("nonexistent-user")

    def test_disconnected_session_raises_expired(self, mock_redis):
        """get_ftp_credentials must raise FtpSessionExpiredError for disconnected sessions."""
        from services.ext_ftp_service import (
            _store_ftp_credentials,
            _update_session_state,
            get_ftp_credentials,
        )
        from utils.errors import FtpSessionExpiredError

        _store_ftp_credentials("user-3", {
            "host": "ftp.test.com",
            "port": 21,
            "username": "u",
            "password": "p",
            "protocol": "ftps",
            "state": "active",
        })
        _update_session_state("user-3", "disconnected")

        with pytest.raises(FtpSessionExpiredError):
            get_ftp_credentials("user-3")

    def test_session_state_update(self, mock_redis):
        """_update_session_state correctly modifies the state field."""
        from services.ext_ftp_service import (
            _store_ftp_credentials,
            _update_session_state,
            _CREDS_KEY,
        )

        _store_ftp_credentials("user-4", {
            "host": "h",
            "port": 21,
            "username": "u",
            "password": "p",
            "protocol": "ftps",
            "state": "active",
        })
        _update_session_state("user-4", "draining")
        raw = mock_redis.get(_CREDS_KEY.format(user_id="user-4"))
        data = json.loads(raw)
        assert data["state"] == "draining"

    def test_wipe_session(self, mock_redis):
        """_wipe_session removes the Redis key entirely."""
        from services.ext_ftp_service import (
            _store_ftp_credentials,
            _wipe_session,
            _CREDS_KEY,
        )

        _store_ftp_credentials("user-5", {
            "host": "h",
            "port": 21,
            "username": "u",
            "password": "p",
            "protocol": "ftps",
            "state": "active",
        })
        assert mock_redis.get(_CREDS_KEY.format(user_id="user-5")) is not None

        _wipe_session("user-5")
        assert mock_redis.get(_CREDS_KEY.format(user_id="user-5")) is None


# ---------------------------------------------------------------------------
# FTP Probe
# ---------------------------------------------------------------------------


class TestFtpProbe:
    """Tests for the real FTP connection probe (mocked ftplib)."""

    @patch("services.ext_ftp_service.ReusedSslFTP_TLS")
    def test_successful_ftps_connection(self, mock_ftp_tls_cls):
        """A successful FTPS probe returns 'ftps'."""
        from services.ext_ftp_service import _probe_ftp_connection

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp

        result = _probe_ftp_connection("ftp.example.com", 21, "user", "pass", False)
        assert result == "ftps"
        mock_ftp.connect.assert_called_once_with("ftp.example.com", 21)
        mock_ftp.auth.assert_called_once()
        mock_ftp.login.assert_called_once_with("user", "pass")
        mock_ftp.quit.assert_called_once()

    @patch("services.ext_ftp_service.ReusedSslFTP_TLS")
    def test_bad_credentials_raises_auth_error(self, mock_ftp_tls_cls):
        """A 530 error from the FTP server raises FtpAuthError."""
        import ftplib
        from services.ext_ftp_service import _probe_ftp_connection
        from utils.errors import FtpAuthError

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.login.side_effect = ftplib.error_perm("530 Login incorrect")

        with pytest.raises(FtpAuthError):
            _probe_ftp_connection("ftp.example.com", 21, "bad", "creds", False)

    @patch("services.ext_ftp_service.ReusedSslFTP_TLS")
    def test_tls_rejected_no_insecure_raises(self, mock_ftp_tls_cls):
        """TLS rejection with allow_insecure=False raises FtpTlsRejectedError."""
        import ftplib
        from services.ext_ftp_service import _probe_ftp_connection
        from utils.errors import FtpTlsRejectedError

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.auth.side_effect = ftplib.error_perm("500 Unknown command")

        with pytest.raises(FtpTlsRejectedError):
            _probe_ftp_connection("ftp.example.com", 21, "user", "pass", False)

    @patch("services.ext_ftp_service.ftplib.FTP")
    @patch("services.ext_ftp_service.ReusedSslFTP_TLS")
    def test_tls_rejected_insecure_fallback(self, mock_ftp_tls_cls, mock_ftp_cls):
        """TLS rejection with allow_insecure=True falls back to plaintext."""
        import ftplib
        from services.ext_ftp_service import _probe_ftp_connection

        # FTP_TLS fails
        mock_ftp_tls = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp_tls
        mock_ftp_tls.auth.side_effect = ftplib.error_perm("500 Unknown command")

        # Plaintext FTP succeeds
        mock_ftp = MagicMock()
        mock_ftp_cls.return_value = mock_ftp

        result = _probe_ftp_connection("ftp.example.com", 21, "user", "pass", True)
        assert result == "ftp"
        mock_ftp.login.assert_called_once_with("user", "pass")

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    def test_unreachable_host_raises_connection_error(self, mock_ftp_tls_cls):
        """An unreachable host raises FtpConnectionError."""
        from services.ext_ftp_service import _probe_ftp_connection
        from utils.errors import FtpConnectionError

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.connect.side_effect = OSError("Connection refused")

        with pytest.raises(FtpConnectionError):
            _probe_ftp_connection("unreachable.host", 21, "u", "p", False)


# ---------------------------------------------------------------------------
# Connect (full orchestration)
# ---------------------------------------------------------------------------


class TestConnectExternalFtp:
    """Tests for the connect_external_ftp orchestration function."""

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_successful_connect(self, mock_probe, mock_redis):
        """A valid connection stores credentials and returns session info."""
        from services.ext_ftp_service import connect_external_ftp, _CREDS_KEY

        user = MagicMock()
        user.id = "test-user-id"

        result = connect_external_ftp(
            user=user,
            host="ftp.example.com",
            port=21,
            username="ftp_user",
            password="secure_pass",
            duration_seconds=7200,
            until_i_stop=False,
            graceful_expiry=True,
            allow_insecure=False,
        )

        assert result["status"] == "success"
        assert result["session"]["host"] == "ftp.example.com"
        assert result["session"]["protocol"] == "ftps"
        assert result["session"]["state"] == "active"
        assert result["session"]["idle_ttl_seconds"] == 1800

        # Verify Redis has the key
        stored = mock_redis.get(_CREDS_KEY.format(user_id="test-user-id"))
        assert stored is not None
        data = json.loads(stored)
        assert "encrypted_password" in data
        assert "password" not in data

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_rate_limit_enforced(self, mock_probe, mock_redis):
        """After 5 successful connects, the 6th raises FtpRateLimitError."""
        from services.ext_ftp_service import connect_external_ftp
        from utils.errors import FtpRateLimitError

        user = MagicMock()
        user.id = "rate-test-user"

        for _ in range(5):
            connect_external_ftp(
                user=user,
                host="ftp.test.com",
                port=21,
                username="u",
                password="p",
                duration_seconds=3600,
                until_i_stop=False,
                graceful_expiry=True,
                allow_insecure=False,
            )

        with pytest.raises(FtpRateLimitError):
            connect_external_ftp(
                user=user,
                host="ftp.test.com",
                port=21,
                username="u",
                password="p",
                duration_seconds=3600,
                until_i_stop=False,
                graceful_expiry=True,
                allow_insecure=False,
            )

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_until_i_stop_sets_zero_expiry(self, mock_probe, mock_redis):
        """When until_i_stop=True, target_expiry_time must be 0."""
        from services.ext_ftp_service import connect_external_ftp

        user = MagicMock()
        user.id = "until-stop-user"

        result = connect_external_ftp(
            user=user,
            host="ftp.test.com",
            port=21,
            username="u",
            password="p",
            duration_seconds=7200,
            until_i_stop=True,
            graceful_expiry=True,
            allow_insecure=False,
        )
        assert result["session"]["target_expiry_time"] == 0


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


class TestDisconnectExternalFtp:
    """Tests for the disconnect_external_ftp dual-path logic."""

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_normal_disconnect_no_jobs(self, mock_probe, mock_redis):
        """Normal disconnect with no active jobs wipes credentials cleanly."""
        from services.ext_ftp_service import (
            connect_external_ftp,
            disconnect_external_ftp,
            _CREDS_KEY,
        )

        user = MagicMock()
        user.id = "dc-user-1"

        connect_external_ftp(
            user=user, host="h", port=21, username="u", password="p",
            duration_seconds=3600, until_i_stop=False,
            graceful_expiry=True, allow_insecure=False,
        )

        db = MagicMock()
        # No active jobs
        db.query.return_value.filter.return_value.count.return_value = 0

        result = disconnect_external_ftp(user=user, db=db, force=False)
        assert result["status"] == "success"
        assert mock_redis.get(_CREDS_KEY.format(user_id="dc-user-1")) is None

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_normal_disconnect_with_active_jobs_raises_draining(self, mock_probe, mock_redis):
        """Normal disconnect with active jobs raises FtpSessionDrainingError."""
        from services.ext_ftp_service import (
            connect_external_ftp,
            disconnect_external_ftp,
            _CREDS_KEY,
        )
        from utils.errors import FtpSessionDrainingError

        user = MagicMock()
        user.id = "dc-user-2"

        connect_external_ftp(
            user=user, host="h", port=21, username="u", password="p",
            duration_seconds=3600, until_i_stop=False,
            graceful_expiry=True, allow_insecure=False,
        )

        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 3

        with pytest.raises(FtpSessionDrainingError) as exc_info:
            disconnect_external_ftp(user=user, db=db, force=False)

        assert exc_info.value.details["active_jobs"] == 3
        assert exc_info.value.details["draining"] is True

        # Session should now be in draining state
        raw = mock_redis.get(_CREDS_KEY.format(user_id="dc-user-2"))
        data = json.loads(raw)
        assert data["state"] == "draining"

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_force_disconnect_cancels_jobs(self, mock_probe, mock_redis):
        """Force disconnect cancels active jobs and wipes credentials."""
        from services.ext_ftp_service import (
            connect_external_ftp,
            disconnect_external_ftp,
            _CREDS_KEY,
        )

        user = MagicMock()
        user.id = "dc-user-3"

        connect_external_ftp(
            user=user, host="h", port=21, username="u", password="p",
            duration_seconds=3600, until_i_stop=False,
            graceful_expiry=True, allow_insecure=False,
        )

        # Mock DB with 2 active jobs
        mock_job_1 = MagicMock()
        mock_job_1.file_id = None
        mock_job_2 = MagicMock()
        mock_job_2.file_id = None

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [mock_job_1, mock_job_2]

        result = disconnect_external_ftp(
            user=user, db=db, force=True, delete_completed_files=False,
        )

        assert result["status"] == "success"
        assert result["cancelled_jobs"] == 2
        assert mock_redis.get(_CREDS_KEY.format(user_id="dc-user-3")) is None

    def test_disconnect_no_session_raises(self, mock_redis):
        """Disconnecting without an active session raises FtpSessionNotFoundError."""
        from services.ext_ftp_service import disconnect_external_ftp
        from utils.errors import FtpSessionNotFoundError

        user = MagicMock()
        user.id = "nonexistent"
        db = MagicMock()

        with pytest.raises(FtpSessionNotFoundError):
            disconnect_external_ftp(user=user, db=db, force=False)


# ---------------------------------------------------------------------------
# Component 2: The Navigator — Unit Tests
# ---------------------------------------------------------------------------


class TestNavigatorHelpers:
    """Unit tests for Component 2 helper functions."""

    def test_parse_mlsd_timestamp_valid(self):
        from services.ext_ftp_service import _parse_mlsd_timestamp

        res = _parse_mlsd_timestamp("20260801120000")
        assert res == "2026-08-01T12:00:00Z"

        res_ms = _parse_mlsd_timestamp("20260801120000.123")
        assert res_ms == "2026-08-01T12:00:00Z"

    def test_parse_mlsd_timestamp_invalid(self):
        from services.ext_ftp_service import _parse_mlsd_timestamp

        assert _parse_mlsd_timestamp(None) is None
        assert _parse_mlsd_timestamp("") is None
        assert _parse_mlsd_timestamp("invalid") is None
        assert _parse_mlsd_timestamp("123") is None

    def test_compute_parent_path(self):
        from services.ext_ftp_service import _compute_parent_path

        assert _compute_parent_path("/") == "/"
        assert _compute_parent_path("/2026_Assets") == "/"
        assert _compute_parent_path("/2026_Assets/logs") == "/2026_Assets"
        assert _compute_parent_path("/a/b/c/d") == "/a/b/c"


class TestGetExternalFtpTree:
    """Unit tests for the get_external_ftp_tree service function."""

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    @patch("services.ext_ftp_service.get_ftp_credentials")
    def test_get_tree_success(self, mock_get_creds, mock_ftp_tls_cls):
        from services.ext_ftp_service import get_external_ftp_tree

        mock_get_creds.return_value = {
            "host": "ftp.example.com",
            "port": 21,
            "username": "user",
            "password": "pass",
            "protocol": "ftps",
        }

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.mlsd.return_value = [
            (".", {"type": "cdir"}),
            ("..", {"type": "pdir"}),
            ("dataset.csv", {"type": "file", "size": "1048576", "modify": "20260801120000"}),
            ("logs", {"type": "dir", "modify": "20260802103000"}),
        ]

        res = get_external_ftp_tree("user-id", "/2026_Assets")

        assert res["current_path"] == "/2026_Assets"
        assert res["parent_path"] == "/"
        assert len(res["items"]) == 2

        # Folders sorted first
        folder_item = res["items"][0]
        assert folder_item["name"] == "logs"
        assert folder_item["path"] == "/2026_Assets/logs"
        assert folder_item["is_folder"] is True
        assert folder_item["size_bytes"] == 0
        assert folder_item["modified_at"] == "2026-08-02T10:30:00Z"

        file_item = res["items"][1]
        assert file_item["name"] == "dataset.csv"
        assert file_item["path"] == "/2026_Assets/dataset.csv"
        assert file_item["is_folder"] is False
        assert file_item["size_bytes"] == 1048576
        assert file_item["modified_at"] == "2026-08-01T12:00:00Z"

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    @patch("services.ext_ftp_service.get_ftp_credentials")
    def test_remote_path_not_found_raises_404(self, mock_get_creds, mock_ftp_tls_cls):
        import ftplib
        from services.ext_ftp_service import get_external_ftp_tree
        from utils.errors import FtpRemotePathNotFoundError

        mock_get_creds.return_value = {
            "host": "ftp.example.com",
            "port": 21,
            "username": "user",
            "password": "pass",
            "protocol": "ftps",
        }

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.mlsd.side_effect = ftplib.error_perm("550 Directory not found")

        with pytest.raises(FtpRemotePathNotFoundError):
            get_external_ftp_tree("user-id", "/nonexistent")


# ---------------------------------------------------------------------------
# Component 3: The Dispatcher — Unit Tests
# ---------------------------------------------------------------------------


class TestTwoTierSemaphores:
    """Unit tests for Two-Tier Semaphores (Host & User)."""

    def test_host_semaphore_limits_at_max(self, mock_redis):
        from services.ext_ftp_service import (
            acquire_host_semaphore,
            release_host_semaphore,
            MAX_HOST_CONCURRENT,
        )

        host = "ftp.host.com"
        for _ in range(MAX_HOST_CONCURRENT):
            assert acquire_host_semaphore(host) is True

        # 5th attempt fails
        assert acquire_host_semaphore(host) is False

        # Release one slot
        release_host_semaphore(host)
        # Now 1 slot is available
        assert acquire_host_semaphore(host) is True

    def test_user_semaphore_limits_at_max(self, mock_redis):
        from services.ext_ftp_service import (
            acquire_user_semaphore,
            release_user_semaphore,
            MAX_USER_CONCURRENT,
        )

        user_id = "user-sem-123"
        for _ in range(MAX_USER_CONCURRENT):
            assert acquire_user_semaphore(user_id) is True

        # 5th attempt fails
        assert acquire_user_semaphore(user_id) is False

        # Release one slot
        release_user_semaphore(user_id)
        assert acquire_user_semaphore(user_id) is True


class TestInitiateExternalFtpIngestion:
    """Unit tests for job queueing and dispatcher logic."""

    @patch("services.ext_ftp_service.get_ftp_credentials")
    def test_initiate_success(self, mock_get_creds, mock_redis):
        from services.ext_ftp_service import initiate_external_ftp_ingestion

        mock_get_creds.return_value = {
            "host": "h", "port": 21, "username": "u", "password": "p",
            "protocol": "ftps", "state": "active",
        }

        user = MagicMock()
        user.id = "usr-1"

        dataset = MagicMock()
        dataset.id = "ds-1"
        dataset.user_id = "usr-1"
        dataset.name = "My Dataset"
        dataset.is_deleted = False

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = dataset

        items = [
            {"path": "/2026_Assets/data.csv", "is_folder": False, "size_bytes": 1048576}
        ]

        res = initiate_external_ftp_ingestion(
            user=user,
            db=db,
            dataset_id="ds-1",
            target_folder_id=None,
            items=items,
        )

        assert res["status"] == "processing"
        assert len(res["jobs"]) == 1
        assert res["jobs"][0]["filename"] == "data.csv"
        assert res["jobs"][0]["filesize"] == 1048576
        assert db.add.called
        assert db.commit.called

    @patch("services.ext_ftp_service.get_ftp_credentials")
    def test_initiate_draining_session_raises_409(self, mock_get_creds, mock_redis):
        from services.ext_ftp_service import initiate_external_ftp_ingestion
        from utils.errors import FtpSessionDrainingError

        mock_get_creds.return_value = {
            "host": "h", "port": 21, "username": "u", "password": "p",
            "protocol": "ftps", "state": "draining",
        }

        user = MagicMock()
        user.id = "usr-1"

        dataset = MagicMock()
        dataset.id = "ds-1"
        dataset.user_id = "usr-1"
        dataset.is_deleted = False

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = dataset

        items = [{"path": "/data.csv", "is_folder": False, "size_bytes": 100}]

        with pytest.raises(FtpSessionDrainingError):
            initiate_external_ftp_ingestion(
                user=user, db=db, dataset_id="ds-1", target_folder_id=None, items=items,
            )

    def test_initiate_unowned_dataset_raises_403(self, mock_redis):
        from services.ext_ftp_service import initiate_external_ftp_ingestion
        from utils.errors import DatasetAccessError

        user = MagicMock()
        user.id = "user-a"

        dataset = MagicMock()
        dataset.id = "ds-foreign"
        dataset.user_id = "user-b"  # Different user
        dataset.is_deleted = False

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = dataset

        items = [{"path": "/data.csv", "is_folder": False, "size_bytes": 100}]

        with pytest.raises(DatasetAccessError):
            initiate_external_ftp_ingestion(
                user=user, db=db, dataset_id="ds-foreign", target_folder_id=None, items=items,
            )


# ---------------------------------------------------------------------------
# Component 4: The Engine & Medic — Unit Tests
# ---------------------------------------------------------------------------


class TestEngineAndMedic:
    """Unit tests for Component 4 streaming worker and recovery helpers."""

    def test_checkpoint_manifest_save_and_load(self, tmp_path):
        from services.ext_ftp_service import (
            _save_checkpoint_manifest,
            _load_checkpoint_manifest,
        )

        manifest_file = tmp_path / "test_job.manifest.json"
        data = {
            "job_id": "job-123",
            "remote_path": "/2026_Assets/data.csv",
            "bytes_downloaded": 1048576,
            "total_bytes": 2097152,
        }

        _save_checkpoint_manifest(str(manifest_file), data)
        assert manifest_file.exists()

        loaded = _load_checkpoint_manifest(str(manifest_file))
        assert loaded is not None
        assert loaded["job_id"] == "job-123"
        assert loaded["bytes_downloaded"] == 1048576

    def test_load_nonexistent_manifest_returns_none(self, tmp_path):
        from services.ext_ftp_service import _load_checkpoint_manifest

        assert _load_checkpoint_manifest(str(tmp_path / "nonexistent.json")) is None

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    @patch("services.ext_ftp_service.get_ftp_credentials")
    def test_process_job_success(self, mock_get_creds, mock_ftp_tls_cls, mock_redis, tmp_path, monkeypatch):
        import io
        from services.ext_ftp_service import process_ftp_ingestion_job

        mock_get_creds.return_value = {
            "host": "ftp.example.com",
            "port": 21,
            "username": "u",
            "password": "p",
            "protocol": "ftps",
        }

        # Setup mock job
        job = MagicMock()
        job.id = "job-engine-1"
        job.user_id = "user-eng-1"
        job.dataset_id = "ds-eng-1"
        job.folder_id = None
        job.source_url_or_id = "/2026_Assets/file.txt"
        job.filename = "file.txt"
        job.status = "pending"
        job.total_bytes = 12

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = job
        db.query.return_value.filter.return_value.first.side_effect = [
            job,   # AsyncIngestionJob query
            None,  # UploadedFile dedup query (no hit -> new file)
        ]

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp

        def mock_retrbinary(cmd, callback, blocksize=8192):
            callback(b"Hello World!")

        mock_ftp.retrbinary.side_effect = mock_retrbinary

        # Patch staging path to tmp_path
        monkeypatch.setattr("services.ext_ftp_service.Path", lambda *args: tmp_path if args and args[0] == "uploads" else Path(*args))

        res = process_ftp_ingestion_job(db, "job-engine-1")
        assert res is True
        assert job.status == "completed"

    def test_cleanup_ftp_staging_files(self, tmp_path, monkeypatch):
        import time
        from services import ext_ftp_service
        from services.ext_ftp_service import cleanup_ftp_staging_files

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir(parents=True)

        old_tmp = staging_dir / "old_job.tmp"
        old_manifest = staging_dir / "old_job.manifest.json"
        fresh_tmp = staging_dir / "fresh_job.tmp"

        old_tmp.write_text("old data")
        old_manifest.write_text("{}")
        fresh_tmp.write_text("fresh data")

        # Set mtime of old files to 48 hours ago
        past_mtime = time.time() - 172800
        os.utime(old_tmp, (past_mtime, past_mtime))
        os.utime(old_manifest, (past_mtime, past_mtime))

        monkeypatch.setattr(ext_ftp_service, "STAGING_ROOT", staging_dir)

        deleted = cleanup_ftp_staging_files(max_age_seconds=86400)
        assert deleted == 2
        assert not old_tmp.exists()
        assert not old_manifest.exists()
        assert fresh_tmp.exists()




