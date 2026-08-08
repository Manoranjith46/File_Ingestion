"""Micro-benchmarks for chunk hashing and chunk-file merging."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from services import file_services


@pytest.fixture(scope="module")
def chunk_payloads() -> tuple[bytes, list[bytes]]:
    payload = b"benchmark-chunk-payload-" * 1024
    chunks = [payload[i : i + 256] for i in range(0, len(payload), 256)]
    return payload, chunks


def test_hash_bytes_benchmark(benchmark, chunk_payloads):
    """Benchmark hashing a chunk payload without any I/O."""
    _, chunks = chunk_payloads
    chunk = chunks[0]
    benchmark(file_services._hash_bytes, chunk)


def test_validate_relative_path_benchmark(benchmark):
    """Benchmark validation of a nested relative path."""
    relative_path = "folder/subfolder/file.txt"
    benchmark(file_services._validate_relative_path, relative_path)


def test_merge_chunk_files_benchmark(benchmark, tmp_path, chunk_payloads):
    """Benchmark merging several chunk files into one output file."""
    _, chunks = chunk_payloads
    temp_dir = tmp_path / "merge"
    temp_dir.mkdir(parents=True, exist_ok=True)

    chunk_paths = []
    for index, chunk in enumerate(chunks[:16]):
        path = temp_dir / f"{index:06d}.part"
        path.write_bytes(chunk)
        chunk_paths.append(path)

    destination = temp_dir / "merged.bin"
    benchmark(file_services._merge_chunk_files, chunk_paths, destination)


if __name__ == "__main__":
    # Simple script-mode micro-benchmarks so running the file directly shows output.
    import time
    import tempfile

    print("Running micro-benchmarks (script mode)")

    payload = b"benchmark-chunk-payload-" * 1024
    chunks = [payload[i : i + 256] for i in range(0, len(payload), 256)]

    # hash_bytes micro-benchmark
    iters = 1000
    print(f"hash_bytes: running {iters} iterations...")
    t0 = time.perf_counter()
    for _ in range(iters):
        file_services._hash_bytes(chunks[0])
    t1 = time.perf_counter()
    print(f"hash_bytes: total {t1 - t0:.6f}s, avg {(t1 - t0) / iters:.6f}s")

    # validate_relative_path micro-benchmark
    iters = 10000
    rel = "folder/subfolder/file.txt"
    print(f"validate_relative_path: running {iters} iterations...")
    t0 = time.perf_counter()
    for _ in range(iters):
        file_services._validate_relative_path(rel)
    t1 = time.perf_counter()
    print(f"validate_relative_path: total {t1 - t0:.6f}s, avg {(t1 - t0) / iters:.6f}s")

    # merge_chunk_files micro-benchmark (writes to a tempdir)
    print("merge_chunk_files: preparing temp files and running 10 iterations...")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        chunk_paths = []
        for index, chunk in enumerate(chunks[:16]):
            path = td_path / f"{index:06d}.part"
            path.write_bytes(chunk)
            chunk_paths.append(path)
        destination = td_path / "merged.bin"
        iters = 10
        t0 = time.perf_counter()
        for _ in range(iters):
            file_services._merge_chunk_files(chunk_paths, destination)
        t1 = time.perf_counter()
        print(f"merge_chunk_files: {iters} iterations total {t1 - t0:.6f}s, avg {(t1 - t0) / iters:.6f}s")
