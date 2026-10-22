"""Comprehensive unit tests for Alexandria backup, integrity check, and restore."""

import tempfile
import sqlite3
from pathlib import Path
import pytest

from alexandria.sqlite_provider import SqliteMemoryProvider
from alexandria.models import MemoryDraft
from alexandria.backup import restore_sqlite_backup, restore_json_snapshot


def test_sqlite_backup_and_restore_cycle():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        live_db = tmp_path / "live.sqlite3"
        backup_db = tmp_path / "backup.sqlite3"
        restored_db = tmp_path / "restored.sqlite3"

        provider = SqliteMemoryProvider(db_path=live_db)
        draft1 = MemoryDraft(body="Fact to survive backup and restore.", is_core=True)
        draft2 = MemoryDraft(body="Second fact.", is_core=False)
        rec1 = provider.create_explicit(draft1, request_id="b-1")
        rec2 = provider.create_explicit(draft2, request_id="b-2")

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


def test_restore_rejects_corrupted_backup():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        corrupted_file = tmp_path / "corrupted.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        # Write invalid garbage bytes
        with open(corrupted_file, "wb") as f:
            f.write(b"NOT_A_VALID_SQLITE_DATABASE_HEADER_DATA")

        with pytest.raises(Exception):
            restore_sqlite_backup(corrupted_file, target_db)

        # Ensure target database was not created
        assert not target_db.exists()


def test_json_snapshot_restore():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        src_db = tmp_path / "src.sqlite3"
        dest_db = tmp_path / "dest.sqlite3"

        src_provider = SqliteMemoryProvider(db_path=src_db)
        src_provider.create_explicit(MemoryDraft(body="Fact for snapshot.", is_core=True), request_id="s-1")
        snapshot = src_provider.export_snapshot()

        dest_provider = SqliteMemoryProvider(db_path=dest_db)
        restored_count = dest_provider.restore_snapshot(snapshot)
        assert restored_count == 1

        approved = dest_provider.list_approved()
        assert len(approved) == 1
        assert approved[0].body == "Fact for snapshot."

