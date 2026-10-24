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


def test_json_snapshot_rejects_unsupported_version():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        bad_snapshot = {
            "version": 99,
            "exported_at": 1700000000,
            "record_count": 0,
            "records": [],
        }
        with pytest.raises(ValueError, match="Unsupported snapshot version"):
            dest_provider.restore_snapshot(bad_snapshot)


def test_json_snapshot_rejects_count_mismatch():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        bad_snapshot = {
            "version": 1,
            "exported_at": 1700000000,
            "record_count": 5,
            "records": [],
        }
        with pytest.raises(ValueError, match="Snapshot record_count mismatch"):
            dest_provider.restore_snapshot(bad_snapshot)


def test_json_snapshot_rejects_missing_fields():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        bad_snapshot = {
            "version": 1,
            "exported_at": 1700000000,
            "record_count": 1,
            "records": [
                {
                    "id": "rec-1",
                    "body": "Missing client request id and hash",
                    "is_core": True,
                    "status": "approved",
                    "created_at": 100,
                    "updated_at": 100,
                }
            ],
        }
        with pytest.raises(ValueError, match="missing required fields"):
            dest_provider.restore_snapshot(bad_snapshot)


def test_json_snapshot_rejects_body_hash_mismatch():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        bad_snapshot = {
            "version": 1,
            "exported_at": 1700000000,
            "record_count": 1,
            "records": [
                {
                    "id": "rec-1",
                    "body": "Valid body text",
                    "is_core": True,
                    "status": "approved",
                    "created_at": 100,
                    "updated_at": 100,
                    "deleted_at": None,
                    "client_request_id": "req-1",
                    "body_hash": "a" * 64,  # Incorrect hash
                }
            ],
        }
        with pytest.raises(ValueError, match="body_hash mismatch"):
            dest_provider.restore_snapshot(bad_snapshot)


def test_json_snapshot_rejects_duplicate_records():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        # Snapshot containing duplicate IDs
        from alexandria.validation import normalize_body, compute_body_hash

        body = "Test duplicate ID."
        h = compute_body_hash(normalize_body(body))
        bad_snapshot = {
            "version": 1,
            "exported_at": 1700000000,
            "record_count": 2,
            "records": [
                {
                    "id": "dup-id",
                    "body": body,
                    "is_core": False,
                    "status": "approved",
                    "created_at": 100,
                    "updated_at": 100,
                    "deleted_at": None,
                    "client_request_id": "req-1",
                    "body_hash": h,
                },
                {
                    "id": "dup-id",
                    "body": body,
                    "is_core": False,
                    "status": "approved",
                    "created_at": 100,
                    "updated_at": 100,
                    "deleted_at": None,
                    "client_request_id": "req-2",
                    "body_hash": h,
                },
            ],
        }
        with pytest.raises(ValueError, match="Duplicate record ID"):
            dest_provider.restore_snapshot(bad_snapshot)


def test_json_snapshot_failure_preserves_existing_data():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        # Insert live baseline data
        dest_provider.create_explicit(
            MemoryDraft(body="Existing live data to be preserved.", is_core=True),
            request_id="live-init",
        )
        assert len(dest_provider.list_approved()) == 1

        bad_snapshot = {
            "version": 1,
            "exported_at": 1700000000,
            "record_count": 1,
            "records": [
                {
                    "id": "bad-rec",
                    "body": "Malformed record",
                    "is_core": False,
                    "status": "approved",
                    "created_at": 100,
                    "updated_at": 100,
                    "deleted_at": None,
                    "client_request_id": "req-bad",
                    "body_hash": "invalid_hash_string",
                }
            ],
        }

        with pytest.raises(ValueError):
            dest_provider.restore_snapshot(bad_snapshot)

        # Confirm original data was completely preserved and untouched
        approved = dest_provider.list_approved()
        assert len(approved) == 1
        assert approved[0].body == "Existing live data to be preserved."


def test_restore_sqlite_backup_fails_when_target_locked():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        live_db = tmp_path / "live.sqlite3"
        backup_db = tmp_path / "backup.sqlite3"
        target_db = tmp_path / "target.sqlite3"

        # Create valid source and backup
        provider = SqliteMemoryProvider(db_path=live_db)
        provider.create_explicit(MemoryDraft(body="Backup content."), request_id="b-lock")
        provider.backup_to_file(backup_db)

        # Create target db and hold an exclusive lock
        target_provider = SqliteMemoryProvider(db_path=target_db)
        target_conn = sqlite3.connect(str(target_db))
        target_conn.execute("BEGIN EXCLUSIVE;")

        try:
            with pytest.raises(RuntimeError, match="actively in use or locked"):
                restore_sqlite_backup(backup_db, target_db)
        finally:
            target_conn.execute("ROLLBACK;")
            target_conn.close()


def test_json_snapshot_rejects_malformed_types_and_metadata():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_db = Path(tmp_dir) / "dest_malformed.sqlite3"
        dest_provider = SqliteMemoryProvider(db_path=dest_db)

        # Baseline record
        dest_provider.create_explicit(MemoryDraft(body="Baseline memory.", is_core=True), request_id="base-1")
        assert len(dest_provider.list_approved()) == 1

        malformed_cases = [
            # 1. Non-dict record item
            {"version": 1, "exported_at": 100, "record_count": 1, "records": ["not_a_dict"]},
            # 2. Invalid is_core type
            {"version": 1, "exported_at": 100, "record_count": 1, "records": [
                {"id": "r1", "body": "test", "is_core": "invalid", "status": "approved", "created_at": 10, "updated_at": 10, "deleted_at": None, "client_request_id": "c1", "body_hash": "a"*64}
            ]},
            # 3. Invalid status
            {"version": 1, "exported_at": 100, "record_count": 1, "records": [
                {"id": "r1", "body": "test", "is_core": True, "status": "pending_review", "created_at": 10, "updated_at": 10, "deleted_at": None, "client_request_id": "c1", "body_hash": "a"*64}
            ]},
            # 4. Negative timestamp
            {"version": 1, "exported_at": 100, "record_count": 1, "records": [
                {"id": "r1", "body": "test", "is_core": True, "status": "approved", "created_at": -1, "updated_at": 10, "deleted_at": None, "client_request_id": "c1", "body_hash": "a"*64}
            ]},
            # 5. String timestamp
            {"version": 1, "exported_at": 100, "record_count": 1, "records": [
                {"id": "r1", "body": "test", "is_core": True, "status": "approved", "created_at": "100", "updated_at": 100, "deleted_at": None, "client_request_id": "c1", "body_hash": "a"*64}
            ]},
            # 6. updated_at earlier than created_at
            {"version": 1, "exported_at": 100, "record_count": 1, "records": [
                {"id": "r1", "body": "test", "is_core": True, "status": "approved", "created_at": 200, "updated_at": 100, "deleted_at": None, "client_request_id": "c1", "body_hash": "a"*64}
            ]},
            # 7. status deleted but deleted_at is None
            {"version": 1, "exported_at": 100, "record_count": 1, "records": [
                {"id": "r1", "body": "test", "is_core": True, "status": "deleted", "created_at": 100, "updated_at": 100, "deleted_at": None, "client_request_id": "c1", "body_hash": "a"*64}
            ]},
        ]

        for idx, bad_snap in enumerate(malformed_cases):
            with pytest.raises(ValueError):
                dest_provider.restore_snapshot(bad_snap)
            # Guarantee zero writes occurred and baseline data remains intact
            approved = dest_provider.list_approved()
            assert len(approved) == 1, f"Failed preservation on malformed case {idx}"
            assert approved[0].body == "Baseline memory."



