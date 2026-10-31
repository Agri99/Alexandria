"""Ordered schema migrations and version metadata management with atomic transactions."""

import sqlite3
import time
from typing import List, Callable, Tuple
import logging

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2


def _migration_001_initial_schema(conn: sqlite3.Connection) -> None:
    """Initial schema baseline (v1)."""
    statements = [
        """CREATE TABLE IF NOT EXISTS schema_meta (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL
        );""",
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            is_core INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK (status IN ('approved', 'deleted')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            deleted_at INTEGER,
            client_request_id TEXT NOT NULL UNIQUE,
            body_hash TEXT NOT NULL
        );""",
        "CREATE INDEX IF NOT EXISTS idx_memories_status_core ON memories(status, is_core, updated_at DESC);",
        "CREATE INDEX IF NOT EXISTS idx_memories_body_hash ON memories(body_hash);",
        "CREATE INDEX IF NOT EXISTS idx_memories_client_request_id ON memories(client_request_id);",
    ]
    for stmt in statements:
        conn.execute(stmt)


def _migration_002_active_dedup_index(conn: sqlite3.Connection) -> None:
    """
    Enforces active deduplication in SQLite via partial unique index.
    Deterministically resolves legacy duplicate approved rows prior to index creation:
    For each body_hash with multiple approved records, the canonical record
    (prioritizing is_core DESC, updated_at DESC, created_at DESC, id ASC) is preserved,
    while duplicate records are transitioned to status='deleted' with deleted_at set.
    No user records are physically deleted from the database.
    """
    now = int(time.time())
    cur = conn.cursor()

    # Identify duplicate body_hash groups with status='approved'
    cur.execute(
        """
        SELECT body_hash
        FROM memories
        WHERE status = 'approved'
        GROUP BY body_hash
        HAVING COUNT(*) > 1
        """
    )
    dup_hashes = [row[0] for row in cur.fetchall()]

    for b_hash in dup_hashes:
        cur.execute(
            """
            SELECT id, is_core, updated_at, created_at
            FROM memories
            WHERE status = 'approved' AND body_hash = ?
            ORDER BY is_core DESC, updated_at DESC, created_at DESC, id ASC
            """,
            (b_hash,)
        )
        rows = cur.fetchall()
        if len(rows) > 1:
            canonical_id = rows[0][0]
            for dup_row in rows[1:]:
                dup_id = dup_row[0]
                logger.info(
                    "Resolving legacy duplicate approved memory %s in favor of canonical %s",
                    dup_id, canonical_id
                )
                conn.execute(
                    """
                    UPDATE memories
                    SET status = 'deleted', deleted_at = ?
                    WHERE id = ?
                    """,
                    (now, dup_id)
                )

    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_active_body_hash
        ON memories(body_hash)
        WHERE status = 'approved';"""
    )


MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_001_initial_schema),
    (2, _migration_002_active_dedup_index),
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
    Applies any outstanding migrations in order within explicit transactions.
    Fails closed with a clear exception and rollbacks if any migration fails.
    """
    current = get_current_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than supported version {CURRENT_SCHEMA_VERSION}."
        )
    for ver, migration_func in MIGRATIONS:
        if ver > current:
            logger.info("Applying schema migration to version %d", ver)
            # Ensure isolation
            try:
                conn.execute("BEGIN IMMEDIATE")
                migration_func(conn)
                now = int(time.time())
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (version, applied_at) VALUES (?, ?)",
                    (ver, now),
                )
                conn.execute("COMMIT")
                current = ver
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                logger.error("Migration to version %d failed: %s", ver, exc)
                raise RuntimeError(f"Database migration to version {ver} failed: {exc}") from exc
    return current
