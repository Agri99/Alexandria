"""Ordered schema migrations and version metadata management."""

import sqlite3
import time
from typing import List, Callable, Tuple
import logging

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1


def _migration_001_initial_schema(conn: sqlite3.Connection) -> None:
    """Initial schema for Revision 3.2 FINAL."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS schema_meta (
        version INTEGER PRIMARY KEY,
        applied_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        is_core INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK (status IN ('approved', 'deleted')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        deleted_at INTEGER,
        client_request_id TEXT NOT NULL UNIQUE,
        body_hash TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_memories_status_core ON memories(status, is_core, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_memories_body_hash ON memories(body_hash);
    CREATE INDEX IF NOT EXISTS idx_memories_client_request_id ON memories(client_request_id);
    """)


MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_001_initial_schema),
]


def get_current_version(conn: sqlite3.Connection) -> int:
    """Returns the current applied schema version, or 0 if uninitialized."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(version) FROM schema_meta")
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.OperationalError:
        # schema_meta does not exist yet
        return 0
    return 0


def run_migrations(conn: sqlite3.Connection) -> int:
    """
    Applies any outstanding migrations in order within transactions.
    Fails closed with a clear exception if any migration fails.
    """
    current = get_current_version(conn)
    for ver, migration_func in MIGRATIONS:
        if ver > current:
            logger.info("Applying schema migration to version %d", ver)
            try:
                with conn:
                    migration_func(conn)
                    now = int(time.time())
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta (version, applied_at) VALUES (?, ?)",
                        (ver, now),
                    )
                current = ver
            except Exception as exc:
                logger.error("Migration to version %d failed: %s", ver, exc)
                raise RuntimeError(f"Database migration to version {ver} failed: {exc}") from exc
    return current
