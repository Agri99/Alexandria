"""Comprehensive unit tests for Alexandria SQLite backup, integrity verification, and recovery."""

import tempfile
import sqlite3
from pathlib import Path
import pytest

from alexandria.sqlite_provider import SqliteMemoryProvider
from alexandria.models import MemoryDraft
from alexandria.backup import restore_sqlite_backup, create_sqlite_backup, write_json_snapshot_file
from alexandria.migrations import CURRENT_SCHEMA_VERSION


def test_sqlite_backup_and_restore_cycle():
    """Verifies full backup, offline restoration, and post-restore data consistency."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        live_db = tmp_path / "live.sqlite3"
        backup_db = tmp_path / "backup.sqlite3"
        restored_db = tmp_path / "restored.sqlite3"

        provider = SqliteMemoryProvider(db_path=live_db)
        draft1 = MemoryDraft(body="Fact to survive backup and restore.", is_core=True)
        draft2 = MemoryDraft(body="Second fact.", is_core=False)
        provider.create_explicit(draft1, request_id="b-1")
        provider.create_explicit(draft2, request_id="b-2")

        # 1. Create online backup
        provider.backup_to_file(backup_db)
        assert backup_db.exists()

        # 2. Restore to new database path
        restore_sqlite_backup(backup_db, restored_db)
        assert restored_db.exists()

        # 3. Open restored database with a new provider and verify records
        restored_provider = SqliteMemoryProvider(db_path=restored_db)
        approved = restored_provider.list_approved()
        assert len(approved) == 2
        bodies = {r.body for r in approved}
        assert "Fact to survive backup and restore." in bodies
        assert "Second fact." in bodies

        # 4. Smoke test: provider can read and write to restored database
        new_rec = restored_provider.create_explicit(MemoryDraft(body="Post restore fact."), request_id="b-3")
        assert new_rec.id is not None
        assert len(restored_provider.list_approved()) == 3


def test_restore_rejects_missing_source():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        non_existent = tmp_path / "does_not_exist.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        with pytest.raises(FileNotFoundError, match="Backup file not found"):
            restore_sqlite_backup(non_existent, target_db)


def test_restore_rejects_identical_source_and_target():
    with tempfile.TemporaryDirectory() as tmp_dir:
        same_path = Path(tmp_dir) / "database.sqlite3"
        with pytest.raises(ValueError, match="Source backup path and target database path cannot be identical"):
            restore_sqlite_backup(same_path, same_path)


def test_restore_rejects_empty_backup():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        empty_file = tmp_path / "empty.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        empty_file.touch()
        with pytest.raises(ValueError, match="file is empty"):
            restore_sqlite_backup(empty_file, target_db)


def test_restore_rejects_corrupted_backup():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        corrupted_file = tmp_path / "corrupted.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        with open(corrupted_file, "wb") as f:
            f.write(b"NOT_A_VALID_SQLITE_DATABASE_HEADER_DATA")

        with pytest.raises(ValueError, match="Corrupted SQLite backup file"):
            restore_sqlite_backup(corrupted_file, target_db)

        assert not target_db.exists()


def test_restore_rejects_missing_schema_meta():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        src_file = tmp_path / "no_meta.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        conn = sqlite3.connect(str(src_file))
        conn.execute("CREATE TABLE dummy (id INT);")
        conn.close()

        with pytest.raises(ValueError, match="schema_meta missing or invalid schema version"):
            restore_sqlite_backup(src_file, target_db)


def test_restore_rejects_unsupported_future_version():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        src_file = tmp_path / "future.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        conn = sqlite3.connect(str(src_file))
        conn.execute("CREATE TABLE schema_meta (version INT PRIMARY KEY, applied_at INT);")
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY);")
        conn.execute("INSERT INTO schema_meta VALUES (999, 1700000000);")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="newer than supported version"):
            restore_sqlite_backup(src_file, target_db)


def test_restore_rejects_missing_required_tables():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        src_file = tmp_path / "missing_tables.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        conn = sqlite3.connect(str(src_file))
        conn.execute("CREATE TABLE schema_meta (version INT PRIMARY KEY, applied_at INT);")
        conn.execute("INSERT INTO schema_meta VALUES (1, 1700000000);")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="missing required tables"):
            restore_sqlite_backup(src_file, target_db)


def test_restore_rejects_locked_target():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        live_db = tmp_path / "live.sqlite3"
        backup_db = tmp_path / "backup.sqlite3"
        target_db = tmp_path / "locked_target.sqlite3"

        provider = SqliteMemoryProvider(db_path=live_db)
        provider.create_explicit(MemoryDraft(body="Fact."), request_id="l-1")
        provider.backup_to_file(backup_db)

        # Initialize target database
        target_provider = SqliteMemoryProvider(db_path=target_db)
        target_provider.create_explicit(MemoryDraft(body="Existing fact."), request_id="ex-1")

        # Open exclusive lock on target database
        conn = sqlite3.connect(str(target_db))
        conn.execute("BEGIN EXCLUSIVE")

        try:
            with pytest.raises(RuntimeError, match="actively in use or locked"):
                restore_sqlite_backup(backup_db, target_db)
        finally:
            conn.rollback()
            conn.close()

        # Verify existing target database was unchanged
        check_provider = SqliteMemoryProvider(db_path=target_db)
        records = check_provider.list_approved()
        assert len(records) == 1
        assert records[0].body == "Existing fact."


def test_restore_over_existing_target_preserves_rollback_on_failure(monkeypatch):
    """Verifies that an existing target is preserved via rollback if an error occurs during replacement."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        live_db = tmp_path / "live.sqlite3"
        backup_db = tmp_path / "backup.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        live_provider = SqliteMemoryProvider(db_path=live_db)
        live_provider.create_explicit(MemoryDraft(body="Live fact."), request_id="lv-1")
        live_provider.backup_to_file(backup_db)

        target_provider = SqliteMemoryProvider(db_path=target_db)
        target_provider.create_explicit(MemoryDraft(body="Important target fact."), request_id="tg-1")

        orig_connect = sqlite3.connect
        target_connect_count = [0]

        def faulty_connect(database, *args, **kwargs):
            conn = orig_connect(database, *args, **kwargs)
            if str(target_db) == str(database):
                target_connect_count[0] += 1
                # Probe connection is call 1; post-restore connection is call 2
                if target_connect_count[0] >= 2:
                    conn.close()
                    raise sqlite3.DatabaseError("Simulated post-restore verification failure")
            return conn

        monkeypatch.setattr(sqlite3, "connect", faulty_connect)

        with pytest.raises(Exception, match="Simulated post-restore verification failure"):
            restore_sqlite_backup(backup_db, target_db)

        # Confirm target still exists and has original data
        monkeypatch.undo()
        check_provider = SqliteMemoryProvider(db_path=target_db)
        records = check_provider.list_approved()
        assert len(records) == 1
        assert records[0].body == "Important target fact."


def test_json_export_is_non_mutating():
    """Verifies that JSON snapshot export is strictly read-only and does not mutate database."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "export_test.sqlite3"
        json_path = tmp_path / "export.json"

        provider = SqliteMemoryProvider(db_path=db_path)
        provider.create_explicit(MemoryDraft(body="Readable exported fact.", is_core=True), request_id="exp-1")

        # Export snapshot
        snapshot = provider.export_snapshot()
        assert snapshot.record_count == 1
        assert snapshot.records[0]["body"] == "Readable exported fact."
        assert snapshot.records[0]["is_core"] is True

        # Write to JSON file
        conn = sqlite3.connect(str(db_path))
        write_json_snapshot_file(conn, json_path)
        conn.close()

        assert json_path.exists()

        # Confirm database is untouched
        approved = provider.list_approved()
        assert len(approved) == 1
        assert approved[0].body == "Readable exported fact."
