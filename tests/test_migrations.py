"""Tests for Alexandria schema migrations and integrity."""

import tempfile
import sqlite3
from pathlib import Path
from alexandria.migrations import run_migrations, get_current_version, CURRENT_SCHEMA_VERSION


def test_migrations_create_schema_and_version():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_mig.sqlite3"
        conn = sqlite3.connect(str(db_path))
        try:
            assert get_current_version(conn) == 0

            applied = run_migrations(conn)
            assert applied == CURRENT_SCHEMA_VERSION
            assert get_current_version(conn) == CURRENT_SCHEMA_VERSION

            # Verify tables exist
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            assert "schema_meta" in tables
            assert "memories" in tables

            # Running migrations again is idempotent
            applied_again = run_migrations(conn)
            assert applied_again == CURRENT_SCHEMA_VERSION
        finally:
            conn.close()

