"""Tests for Alexandria schema migrations and integrity."""

import tempfile
import sqlite3
from pathlib import Path
import pytest
from alexandria.migrations import run_migrations, get_current_version, CURRENT_SCHEMA_VERSION
import alexandria.migrations as mig_mod


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

            # Verify partial unique index exists
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_memories_active_body_hash'")
            assert cur.fetchone() is not None

            # Running migrations again is idempotent
            applied_again = run_migrations(conn)
            assert applied_again == CURRENT_SCHEMA_VERSION
        finally:
            conn.close()


def test_migration_v1_to_v2_upgrade_on_populated_database():
    """Verifies seamless upgrade from schema version 1 to schema version 2 on populated data."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "v1_to_v2.sqlite3"
        conn = sqlite3.connect(str(db_path))

        # Manually apply only migration 1
        mig_mod._migration_001_initial_schema(conn)
        conn.execute("INSERT INTO schema_meta (version, applied_at) VALUES (1, 1000);")
        conn.execute(
            """
            INSERT INTO memories (
                id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
            ) VALUES ('m-v1', 'V1 memory body', 1, 'approved', 1000, 1000, NULL, 'req-v1', 'hash-v1')
            """
        )
        conn.commit()
        assert get_current_version(conn) == 1

        # Now run full migrations
        applied = run_migrations(conn)
        assert applied == 2
        assert get_current_version(conn) == 2

        # Verify data is preserved and new partial index is active
        cur = conn.cursor()
        cur.execute("SELECT body FROM memories WHERE id = 'm-v1'")
        assert cur.fetchone()[0] == "V1 memory body"

        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_memories_active_body_hash'")
        assert cur.fetchone() is not None
        conn.close()


def test_migrations_reject_future_version():
    """Verifies that database declaring a schema version newer than running code fails closed."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "future_mig.sqlite3"
        conn = sqlite3.connect(str(db_path))

        conn.execute("CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at INTEGER);")
        conn.execute(f"INSERT INTO schema_meta VALUES ({CURRENT_SCHEMA_VERSION + 1}, 1000);")
        conn.commit()

        with pytest.raises(RuntimeError, match="newer than supported version"):
            run_migrations(conn)
        conn.close()


def test_migration_failure_rolls_back_and_preserves_records():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "rollback_test.sqlite3"
        conn = sqlite3.connect(str(db_path))

        # 1. Initialize schema to current version 2
        run_migrations(conn)
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION

        # 2. Insert valid memory record
        conn.execute(
            """
            INSERT INTO memories (
                id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
            ) VALUES ('m-init', 'Data before failed migration', 1, 'approved', 1000, 1000, NULL, 'req-init', 'hash-init')
            """
        )
        conn.commit()

        # 3. Define a migration 3 that creates a table and then fails
        def failing_migration_003(c: sqlite3.Connection) -> None:
            c.execute("CREATE TABLE partial_migration_test (id INTEGER PRIMARY KEY);")
            c.execute("INSERT INTO non_existent_table VALUES (999);")  # Fails!

        original_migrations = list(mig_mod.MIGRATIONS)
        mig_mod.MIGRATIONS = list(original_migrations) + [(CURRENT_SCHEMA_VERSION + 1, failing_migration_003)]

        try:
            with pytest.raises(RuntimeError, match="Database migration to version 3 failed"):
                run_migrations(conn)

            # 4. Assert rollback in existing connection
            assert get_current_version(conn) == CURRENT_SCHEMA_VERSION

            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM schema_meta WHERE version = {CURRENT_SCHEMA_VERSION + 1}")
            assert cur.fetchone()[0] == 0

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='partial_migration_test'")
            assert cur.fetchone() is None

            cur.execute("SELECT body FROM memories WHERE id = 'm-init'")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "Data before failed migration"
        finally:
            mig_mod.MIGRATIONS = original_migrations
            conn.close()

        # 5. Reopen database and verify no partial schema was persisted
        reopened = sqlite3.connect(str(db_path))
        try:
            assert get_current_version(reopened) == CURRENT_SCHEMA_VERSION
            cur = reopened.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='partial_migration_test'")
            assert cur.fetchone() is None
            cur.execute("SELECT COUNT(*) FROM memories WHERE id = 'm-init'")
            assert cur.fetchone()[0] == 1
        finally:
            reopened.close()
