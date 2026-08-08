"""Locust load tests for FastAPI chunked upload workflows.

Run locally with:
    .\\.venv\\Scripts\\python.exe -m locust -f tests/load_tests/locustfile.py --headless --users 20 --spawn-rate 2 --run-time 60s --host http://127.0.0.1:8000

This test uses a full authenticated user lifecycle, creates a dataset, then performs an upload
workflow consisting of init, five sequential chunk uploads, and finalize.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import time
from locust import FastHttpUser, between, task, LoadTestShape


class FileIngestionUser(FastHttpUser):
    wait_time = between(0.1, 1)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.dataset_id: str | None = None
        self.upload_id: str | None = None
        self.upload_master_hash: str | None = None
        self.total_chunks: int = 0
        self.next_chunk_index: int = 0
        self.chunk_size = 1 * 1024 * 1024
        self.total_file_size = self.chunk_size * 5

    def on_start(self) -> None:
        self._signup_verify_login()
        self._create_dataset()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    def _generate_user_credentials(self) -> tuple[str, str, str]:
        suffix = uuid.uuid4().hex[:8]
        return (
            f"loaduser-{suffix}",
            f"loaduser-{suffix}@example.com",
            "StrongPass123!",
        )

    def _parse_tokens(self, response) -> None:
        auth_header = response.headers.get("Authorization", "")
        refresh_header = response.headers.get("X-Refresh-Token", "")
        if auth_header.startswith("Bearer "):
            self.access_token = auth_header.removeprefix("Bearer ").strip()
        if refresh_header:
            self.refresh_token = refresh_header.strip()

    def _signup_verify_login(self) -> None:
        username, email, password = self._generate_user_credentials()
        self.client.post(
            "/auth/signup/init",
            json={
                "email": email,
                "username": username,
                "password": password,
                "full_name": "Load Test User",
            },
            name="auth/signup/init",
            timeout=20,
        )
        self.client.post(
            "/auth/signup/verify",
            json={"email": email, "otp_code": "123456"},
            name="auth/signup/verify",
            timeout=20,
        )
        login_resp = self.client.post(
            "/auth/login",
            json={"identifier": email, "password": password},
            name="auth/login",
            timeout=20,
        )
        self._parse_tokens(login_resp)

    def _create_dataset(self) -> None:
        if not self.access_token:
            return
        response = self.client.post(
            "/v1/datasets",
            json={"name": f"load-dataset-{uuid.uuid4().hex[:8]}", "language": "English"},
            headers=self._auth_headers(),
            name="v1/datasets",
            timeout=20,
        )
        if response.status_code == 201:
            self.dataset_id = response.json().get("id")

    def _init_upload(self) -> None:
        if not self.dataset_id:
            self._create_dataset()
        if not self.dataset_id:
            return

        self.upload_master_hash = hashlib.sha256(b"x" * self.total_file_size).hexdigest()
        response = self.client.post(
            "/v1/upload/init",
            json={
                "dataset_id": self.dataset_id,
                "filename": f"load-{uuid.uuid4().hex}.bin",
                "filesize": self.total_file_size,
                "master_hash": self.upload_master_hash,
                "relative_path": "load-tests",
            },
            headers=self._auth_headers(),
            name="v1/upload/init",
            timeout=30,
        )
        if response.status_code != 200:
            self.upload_id = None
            return

        payload = response.json()
        self.upload_id = payload.get("upload_id")
        self.total_chunks = int(payload.get("total_chunks", 0))
        self.next_chunk_index = 0

    def _upload_chunk(self) -> None:
        if not self.upload_id or self.next_chunk_index >= self.total_chunks:
            self._init_upload()
            if not self.upload_id:
                return

        chunk_bytes = b"x" * self.chunk_size
        chunk_hash = hashlib.sha256(chunk_bytes).hexdigest()
        response = self.client.post(
            "/v1/upload/chunk",
            data={
                "upload_id": self.upload_id,
                "chunk_index": str(self.next_chunk_index),
                "chunk_hash": chunk_hash,
            },
            files={"chunk_file": ("chunk.bin", chunk_bytes, "application/octet-stream")},
            headers=self._auth_headers(),
            name="v1/upload/chunk",
            timeout=30,
        )
        if response.status_code == 200:
            self.next_chunk_index += 1

    def _finalize_upload(self) -> None:
        if not self.upload_id or self.next_chunk_index < self.total_chunks:
            return

        response = self.client.post(
            "/v1/upload/finalize",
            json={"upload_id": self.upload_id, "master_hash": self.upload_master_hash},
            headers=self._auth_headers(),
            name="v1/upload/finalize",
            timeout=30,
        )
        if response.status_code == 200:
            self.upload_id = None
            self.upload_master_hash = None
            self.total_chunks = 0
            self.next_chunk_index = 0

    @task(3)
    def upload_init_task(self) -> None:
        self._init_upload()

    @task(5)
    def upload_chunk_task(self) -> None:
        self._upload_chunk()

    @task(2)
    def finalize_upload_task(self) -> None:
        self._finalize_upload()


class InfiniteRampShape(LoadTestShape):
    """Ramp shape that adds 50 users every 10 seconds indefinitely.

    tick() returns a (user_count, spawn_rate) tuple. Returning None would stop the test,
    so we always return increasing user counts with a fixed spawn rate.
    """

    def __init__(self, users_per_step: int = 50, step_seconds: int = 10) -> None:
        self.users_per_step = users_per_step
        self.step_seconds = step_seconds
        self.start_time = time.time()

    def tick(self):
        run_time = int(time.time() - self.start_time)
        # How many full steps have elapsed
        steps = run_time // self.step_seconds
        user_count = max(1, steps * self.users_per_step)
        spawn_rate = self.users_per_step
        return (user_count, spawn_rate)
