#!/bin/sh
# Starts the server -- no migration step needed (query_log.py's _init_db()
# self-initializes its SQLite schema on import, unlike costguard's
# Alembic-managed Postgres).
set -e

echo "Starting Munshi on port ${PORT:-8010}..."
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8010}"
