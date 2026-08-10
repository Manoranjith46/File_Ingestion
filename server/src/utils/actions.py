"""Action mapping for activity and audit log events."""

from __future__ import annotations

import re

from typing import Final


ACTION_MAP: Final[dict[tuple[str, str], str]] = {
    # Authentication
    ("POST", "/auth/login"): "User Login",
    ("POST", "/auth/logout"): "User Logged out",
    ("POST", "/auth/signup/verify"): "Account Created",
    ("POST", "/auth/password-reset/verify"): "Password Changed",
    # Dataset
    ("POST", "/v1/datasets"): "Dataset Created",
    # Local Upload
    ("POST", "/v1/upload/init"): "File Upload Initialized",
    ("POST", "/v1/upload/finalize"): "File Uploaded",
    ("POST", "/v1/uploads/delete"): "File Deleted",
    # Google Drive
    ("GET", "/auth/google/callback"): "Google Drive Connected",
    ("DELETE", "/auth/google/logout"): "Google Drive Disconnected",
    ("POST", "/v1/ingest/gdrive/init"): "Google Drive File Imported",
    ("POST", "/v1/ingest/gdrive/url"): "Google Drive URL Imported",
    # SharePoint
    ("GET", "/auth/microsoft/callback"): "SharePoint Connected",
    ("DELETE", "/auth/microsoft/logout"): "SharePoint Disconnected",
    ("POST", "/v1/ingest/sharepoint/init"): "SharePoint File Imported",
    ("POST", "/v1/ingest/sharepoint/url"): "SharePoint URL Imported",
}

PATTERN_ACTIONS: Final[list[tuple[str, re.Pattern[str], str]]] = [
    ("PATCH", re.compile(r"^/v1/datasets/[^/]+$", re.IGNORECASE), "Dataset Updated"),
    ("DELETE", re.compile(r"^/v1/datasets/[^/]+$", re.IGNORECASE), "Dataset Deleted"),
]


def get_action_name(method: str, path: str) -> str:
    """Return a human-friendly activity label for a request method and path."""
    normalized_method = method.strip().upper()
    normalized_path = path.rstrip("/")
    if (normalized_method, normalized_path) in ACTION_MAP:
        return ACTION_MAP[(normalized_method, normalized_path)]

    for expected_method, pattern, label in PATTERN_ACTIONS:
        if normalized_method == expected_method and pattern.match(normalized_path):
            return label

    return f"{normalized_method} {path}"

