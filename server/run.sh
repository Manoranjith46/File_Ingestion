#!/usr/bin/env bash
set -euo pipefail

# Small helper to build and run via docker compose
cd "$(dirname "$0")"

echo "Building and starting containers..."
docker compose down
docker compose build
docker compose up -d --build
