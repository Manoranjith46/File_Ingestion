"""Micro-benchmark helper for auth token generation."""

from __future__ import annotations

import sys
import timeit
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from models.auth_model import User
from services import auth_services


class DummySession:
    """Minimal session stub for benchmarking token issuance."""

    def commit(self) -> None:
        return None

    def refresh(self, instance: object) -> None:
        return None


def benchmark_issue_token_pair() -> None:
    """Measure the cost of issuing a token pair for a synthetic user."""
    auth_services.active_session_limiter = lambda **_: None

    user = User(
        id="benchmark-user",
        email="benchmark@example.com",
        username="benchmark",
        full_name="Benchmark",
        password_hash="hash",
        role="user",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    session = DummySession()
    elapsed = timeit.timeit(lambda: auth_services.issue_token_pair(session, user), number=10)
    print(f"{elapsed:.6f} seconds")


if __name__ == "__main__":
    benchmark_issue_token_pair()
