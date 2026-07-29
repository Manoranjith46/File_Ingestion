"""Micro-benchmark helper for auth token generation."""

from __future__ import annotations

import sys
import timeit
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.models.auth_model import User
from src.services.auth_services import issue_token_pair


def benchmark_issue_token_pair() -> None:
    """Measure the cost of issuing a token pair for a synthetic user."""
    user = User(email="benchmark@example.com", username="benchmark", full_name="Benchmark", password_hash="hash", auth_provider="local", is_verified=True)
    session = None
    print(timeit.timeit(lambda: issue_token_pair(session, user), number=10))


if __name__ == "__main__":
    benchmark_issue_token_pair()
