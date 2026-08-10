from __future__ import annotations

from typing import Any, ClassVar

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class APIErrors(HTTPException):
    """Base exception type for centralized API error handling."""

    code: ClassVar[str] = "internal_error"
    message: ClassVar[str] = "An unexpected error occurred."
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> None:
        self.public_message = message or self.message
        self.details = details or {}
        self.cause = cause
        resolved_status = http_status if http_status is not None else self.http_status
        self.http_status = resolved_status
        super().__init__(status_code=resolved_status, detail=self.public_message)

    def to_response(self) -> JSONResponse:
        payload = {
            "error": {
                "code": self.code,
                "message": self.public_message,
                "details": self.details,
                "retryable": self.retryable,
            }
        }
        return JSONResponse(status_code=self.status_code, content=payload)


def _error(
    name: str,
    code: str,
    message: str,
    http_status: int = 400,
    retryable: bool = False,
) -> type[APIErrors]:
    """Create a dynamically generated domain-specific error subclass."""

    return type(
        name,
        (APIErrors,),
        {
            "__module__": __name__,
            "code": code,
            "message": message,
            "http_status": http_status,
            "retryable": retryable,
        },
    )


ConfigurationError = _error("ConfigurationError", "configuration_error", "Service configuration is invalid.", http_status=503)
InvalidTokenError = _error("InvalidTokenError", "invalid_token", "Invalid token", http_status=401)
UserNotFoundError = _error("UserNotFoundError", "user_not_found", "User not found", http_status=404)
InvalidCredentialsError = _error("InvalidCredentialsError", "invalid_credentials", "Invalid login credentials", http_status=401)
AccountNotVerifiedError = _error("AccountNotVerifiedError", "account_not_verified", "User account is not verified", http_status=403)
DuplicateEmailError = _error("DuplicateEmailError", "duplicate_email", "Email already exists", http_status=409)
DuplicateUsernameError = _error("DuplicateUsernameError", "duplicate_username", "Username already exists", http_status=409)
UnauthorizedAccessError = _error("UnauthorizedAccessError", "unauthorized_access", "You are not authorized to access this resource.", http_status=401)
InvalidOTPError = _error("InvalidOTPError", "invalid_otp", "Invalid OTP code", http_status=400)
NoActiveOTPChallengeError = _error("NoActiveOTPChallengeError", "no_active_otp_challenge", "No active OTP challenge", http_status=400)
OTPExpiredError = _error("OTPExpiredError", "otp_expired", "OTP has expired", http_status=400)
OtpAttemptsExceededError = _error("OtpAttemptsExceededError", "otp_attempts_exceeded", "OTP attempts exceeded", http_status=429)
NoActiveResetChallengeError = _error("NoActiveResetChallengeError", "no_active_reset_challenge", "No active reset challenge", http_status=400)
ResetTokenExpiredError = _error("ResetTokenExpiredError", "reset_token_expired", "Reset token has expired", http_status=400)
InvalidResetTokenError = _error("InvalidResetTokenError", "invalid_reset_token", "Invalid reset token", http_status=400)
GoogleTokenExchangeError = _error("GoogleTokenExchangeError", "google_token_exchange_failed", "Google token exchange failed", http_status=400)
MicrosoftTokenExchangeError = _error("MicrosoftTokenExchangeError", "microsoft_token_exchange_failed", "Microsoft token exchange failed", http_status=400)
InvalidRelativePathError = _error("InvalidRelativePathError", "invalid_relative_path", "relative_path is invalid", http_status=400)
FolderNotFoundError = _error("FolderNotFoundError", "folder_not_found", "Folder not found", http_status=404)
DatasetNotFoundError = _error("DatasetNotFoundError", "dataset_not_found", "Dataset not found.", http_status=404)
DatasetAccessError = _error("DatasetAccessError", "dataset_access_denied", "Dataset does not belong to the current user.", http_status=403)
DuplicateDatasetError = _error("DuplicateDatasetError", "duplicate_dataset", "A dataset with this name already exists.", http_status=409)
InvalidDatasetStateError = _error("InvalidDatasetStateError", "invalid_dataset_state", "Invalid dataset state transition.", http_status=400)
UploadSessionNotFoundError = _error("UploadSessionNotFoundError", "upload_session_not_found", "Upload session not found", http_status=404)
UploadOwnershipError = _error("UploadOwnershipError", "upload_ownership_error", "Upload does not belong to the current user", http_status=403)
ChunkValidationError = _error("ChunkValidationError", "chunk_validation_error", "Chunk validation failed", http_status=400)
UploadIncompleteError = _error("UploadIncompleteError", "upload_incomplete", "Upload is incomplete", http_status=409)
MissingChunkError = _error("MissingChunkError", "missing_chunk", "Missing required chunk", http_status=409)
UploadNotFoundError = _error("UploadNotFoundError", "upload_not_found", "Upload not found", http_status=404)
FileNotFoundError = _error("FileNotFoundError", "file_not_found", "File not found.", http_status=404)
FileOwnershipError = _error("FileOwnershipError", "file_ownership_error", "File does not belong to the user.", http_status=403)
IntegrationConnectionError = _error("IntegrationConnectionError", "integration_connection_error", "Cloud integration is not connected.", http_status=401)
IntegrationRequestError = _error("IntegrationRequestError", "integration_request_error", "Cloud integration request failed.", http_status=400)
IngestionJobNotFoundError = _error("IngestionJobNotFoundError", "ingestion_job_not_found", "Ingestion job not found", http_status=404)

# --- External FTP Pull Ingestion (v3.3) ---
FtpConnectionError = _error("FtpConnectionError", "ftp_connection_error", "Failed to connect to external FTP server.", http_status=504)
FtpTlsRejectedError = _error("FtpTlsRejectedError", "ftp_tls_rejected", "Server rejected TLS and insecure mode is not enabled.", http_status=400)
FtpAuthError = _error("FtpAuthError", "ftp_auth_error", "Invalid FTP credentials.", http_status=401)
FtpRateLimitError = _error("FtpRateLimitError", "ftp_rate_limit", "Too many FTP connection attempts. Try again later.", http_status=429)
FtpSessionNotFoundError = _error("FtpSessionNotFoundError", "ftp_session_not_found", "No active FTP session found.", http_status=404)
FtpSessionExpiredError = _error("FtpSessionExpiredError", "ftp_session_expired", "FTP session credentials have expired.", http_status=410)
FtpSessionDrainingError = _error("FtpSessionDrainingError", "ftp_session_draining", "FTP session is currently draining.", http_status=409)
FtpRemotePathNotFoundError = _error("FtpRemotePathNotFoundError", "ftp_remote_path_not_found", "Remote FTP directory path does not exist.", http_status=404)

# --- Virtual Deduplication (Phase 0) ---
FilenameCollisionError = _error("FilenameCollisionError", "filename_collision", "Filename collision detected", http_status=409)

