#!/bin/sh
set -e

echo "Waiting for PostgreSQL..."
until python - <<'PY'
import os
import sys
from sqlalchemy import create_engine

conn = os.getenv("Connection_String") or os.getenv("DATABASE_URL") or "postgresql://postgres:Zenteiq@postgres:5432/Ingester-Database"
if "127.0.0.1" in conn or "localhost" in conn:
    conn = conn.replace("127.0.0.1", "postgres").replace("localhost", "postgres")

try:
    engine = create_engine(conn, pool_pre_ping=True)
    with engine.connect():
        sys.exit(0)
except Exception as e:
    sys.exit(1)
PY

do
  echo "PostgreSQL is not ready yet; waiting..."
  sleep 2
done

echo "Bootstrapping database..."
python bootstrap_db.py

echo "Starting FastAPI server..."
exec uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
