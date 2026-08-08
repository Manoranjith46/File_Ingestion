# Benchmarking notes

## Micro-benchmarks
Run:

```bash
uv run pytest tests/benchmark/benchmark_chunking.py --benchmark-only -q
```

## Load test
Install Locust if needed:

```bash
uv pip install locust
```

Then run the server locally and execute:

```bash
uv run locust -f tests/load/locustfile.py --headless --users 50 --spawn-rate 5 --run-time 60s --host http://127.0.0.1:8000
```
