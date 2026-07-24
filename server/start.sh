#!/bin/sh
set -e

echo "Waiting for PostgreSQL..."
until python - <<'PY'
import os
import sys
from sqlalchemy import create_engine

conn = os.getenv("Connection_String")
if not conn:
    sys.exit(1)
engine = create_engine(conn, pool_pre_ping=True)
try:
    with engine.connect():
        pass
except Exception:
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
