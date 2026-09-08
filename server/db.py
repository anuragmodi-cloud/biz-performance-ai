"""Shared Supabase (Postgres) connection pool -- query_log.py and
transcripts.py both use this ONE pool rather than each opening their own.

Replaces the previous local SQLite file (query_log.db), which turned out to
be unusable on Render's free tier: containers there have no persistent
disk, so any local file is wiped on every restart/redeploy, and even on
Render's own free-tier idle-then-cold-start behavior -- the admin dashboard
was reliably showing zero logged asks shortly after real usage.

Uses Supabase's "Transaction pooler" (typically port 6543, PgBouncer
transaction mode) -- prepared statements aren't safely reusable across
pooled backend connections in that mode, so server-side prepare is
disabled explicitly (prepare_threshold=None) on every connection this pool
hands out; psycopg falls back to always sending the full query text, which
transaction-pooling mode supports fine.
"""
from __future__ import annotations

import os

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not set -- query_log/transcript persistence needs a Supabase "
                "(or any Postgres) connection string. See server/.env.example."
            )
        _pool = ConnectionPool(
            database_url,
            min_size=1,
            max_size=5,
            kwargs={"prepare_threshold": None, "autocommit": True},
            open=True,
        )
    return _pool
