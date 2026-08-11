"""Action mapping for activity and audit log events."""

from __future__ import annotations

import re

from typing import Final


ACTION_MAP: Final[dict[tuple[str, str], str]] = {
    ("POST", "/auth/login"): "Login",
    ("POST", "/auth/signup/verify"): "Account Created",
    ("POST", "/auth/password-reset/verify"): "Password Changed",
    ("POST", "/v1/datasets"): "Dataset Created",
    ("POST", "/v1/upload/finalize"): "File Uploaded",
    ("POST", "/v1/uploads/delete"): "File Deleted",
    ("GET", "/auth/google/callback"): "Google Drive Connected",
    ("DELETE", "/auth/google/logout"): "Google Drive Disconnected",
    ("POST", "/v1/ingest/gdrive/init"): "Google Drive File Imported",
    ("POST", "/v1/ingest/gdrive/url"): "Google Drive URL Imported",
    ("GET", "/auth/microsoft/callback"): "SharePoint Connected",
    ("DELETE", "/auth/microsoft/logout"): "SharePoint Disconnected",
    ("POST", "/v1/ingest/sharepoint/init"): "SharePoint File Imported",
    ("POST", "/v1/ingest/sharepoint/url"): "SharePoint URL Imported",
    ("POST", "/v1/ingest/ftp/connect"): "FTP Connected",
    ("POST", "/v1/ingest/ftp/disconnect"): "FTP Disconnected",
    ("POST", "/v1/ingest/ftp/init"): "FTP File Imported",
}


PATTERN_ACTIONS: Final[list[tuple[str, re.Pattern[str], str]]] = [
    ("PATCH", re.compile(r"^/v1/datasets/[^/]+$", re.IGNORECASE), "Dataset Updated"),
    ("DELETE", re.compile(r"^/v1/datasets/[^/]+$", re.IGNORECASE), "Dataset Deleted"),
]

FAILURE_ACTION_MAP: Final[dict[str, str]] = {
    "Login": "Login Failed",
    "Account Created": "Account Creation Failed",
    "Password Changed": "Password Reset Failed",
    "Dataset Created": "Dataset Creation Failed",
    "File Uploaded": "File Upload Failed",
    "File Deleted": "File Deletion Failed",
    "Google Drive Connected": "Google Drive Connection Failed",
    "Google Drive Disconnected": "Google Drive Disconnect Failed",
    "Google Drive File Imported": "Google Drive Import Failed",
    "Google Drive URL Imported": "Google Drive URL Import Failed",
    "SharePoint Connected": "SharePoint Connection Failed",
    "SharePoint Disconnected": "SharePoint Disconnect Failed",
    "SharePoint File Imported": "SharePoint Import Failed",
    "SharePoint URL Imported": "SharePoint URL Import Failed",
    "FTP Connected": "FTP Connection Failed",
    "FTP Disconnected": "FTP Disconnect Failed",
    "FTP File Imported": "FTP Import Failed",
    "Dataset Updated": "Dataset Update Failed",
    "Dataset Deleted": "Dataset Deletion Failed",
}

VALID_AUDIT_ACTIONS: Final[set[str]] = (
    set(ACTION_MAP.values())
    | {label for _, _, label in PATTERN_ACTIONS}
    | set(FAILURE_ACTION_MAP.values())
    | {f"{act} Failed" for act in ACTION_MAP.values()}
)


def get_action_name(method: str, path: str, status_code: int = 200) -> str | None:
    """Return a human-friendly activity label for a request method, path, and HTTP status code."""
    normalized_method = method.strip().upper()
    normalized_path = path.rstrip("/")
    base_action = None

    if (normalized_method, normalized_path) in ACTION_MAP:
        base_action = ACTION_MAP[(normalized_method, normalized_path)]
    else:
        for expected_method, pattern, label in PATTERN_ACTIONS:
            if normalized_method == expected_method and pattern.match(normalized_path):
                base_action = label
                break

    if base_action is None:
        return None

    if status_code >= 400:
        return FAILURE_ACTION_MAP.get(base_action, f"{base_action} Failed")

    return base_action


def is_valid_audit_action(action: str | None) -> bool:
    """Return True when the resolved action is one of the allowed audit labels."""
    return action in VALID_AUDIT_ACTIONS

