"""Comprehensive unit tests for Alexandria SqliteMemoryProvider."""

import tempfile
import time
from pathlib import Path
import pytest

from alexandria.sqlite_provider import SqliteMemoryProvider
from alexandria.models import MemoryDraft
from alexandria.lifecycle import (
    RequestIdConflictError,
    MemoryNotFoundError,
    UndoWindowExpiredError,
    InvalidMemoryError,
)


@pytest.fixture
def provider():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria.sqlite3"
        yield SqliteMemoryProvider(db_path=db_path, undo_window_seconds=10)


def test_create_explicit_approved_server_side(provider):
    draft = MemoryDraft(body="User prefers concise explanations.", is_core=True)
    rec = provider.create_explicit(draft, request_id="req-1")

    assert rec.id is not None
    assert rec.body == "User prefers concise explanations."
    assert rec.is_core is True
    assert rec.status == "approved"
    assert rec.created_at > 0
    assert rec.updated_at == rec.created_at
    assert rec.deleted_at is None
    assert rec.client_request_id == "req-1"
    assert rec.body_hash is not None

    # Verify retrieval
    fetched = provider.get_approved(rec.id)
    assert fetched is not None
    assert fetched.id == rec.id
    assert fetched.status == "approved"


def test_idempotent_retry_same_payload(provider):
    draft = MemoryDraft(body="Always format output as markdown.")
    rec1 = provider.create_explicit(draft, request_id="idemp-1")
    rec2 = provider.create_explicit(draft, request_id="idemp-1")

    assert rec1.id == rec2.id
    assert rec1.body == rec2.body
    assert rec1.client_request_id == rec2.client_request_id

    # Ensure only 1 row exists
    all_recs = provider.list_approved()
    assert len(all_recs) == 1


def test_idempotent_conflict_different_body(provider):
    draft1 = MemoryDraft(body="User language preference is English.")
    provider.create_explicit(draft1, request_id="conflict-key")

    draft2 = MemoryDraft(body="User language preference is Indonesian.")
    with pytest.raises(RequestIdConflictError):
        provider.create_explicit(draft2, request_id="conflict-key")


def test_exact_duplicate_detection_via_hash(provider):
    # Normalized whitespace should produce same hash
    draft1 = MemoryDraft(body="Project directory is C:\\Dev.")
    rec1 = provider.create_explicit(draft1, request_id="req-a")

    draft2 = MemoryDraft(body="  Project   directory is C:\\Dev.  ")
    rec2 = provider.create_explicit(draft2, request_id="req-b")

    assert rec1.id == rec2.id
    all_recs = provider.list_approved()
    assert len(all_recs) == 1


def test_update_explicit(provider):
    draft1 = MemoryDraft(body="Initial draft content.")
    rec = provider.create_explicit(draft1, request_id="edit-req")

    time.sleep(0.01)
    draft2 = MemoryDraft(body="Updated fact content.", is_core=True)
    updated = provider.update_explicit(rec.id, draft2)

    assert updated.id == rec.id
    assert updated.body == "Updated fact content."
    assert updated.is_core is True
    assert updated.updated_at >= rec.updated_at

    fetched = provider.get_approved(rec.id)
    assert fetched.body == "Updated fact content."


def test_soft_delete_and_context_exclusion(provider):
    draft = MemoryDraft(body="Fact to be deleted.")
    rec = provider.create_explicit(draft, request_id="del-req")

    # Visible before delete
    assert provider.get_approved(rec.id) is not None
    ctx_before = provider.load_context(max_memory_chars=1000)
    assert any(r.id == rec.id for r in ctx_before.records)

    provider.soft_delete(rec.id)

    # Immediately excluded after delete
    assert provider.get_approved(rec.id) is None
    ctx_after = provider.load_context(max_memory_chars=1000)
    assert not any(r.id == rec.id for r in ctx_after.records)


def test_restore_within_undo_window(provider):
    draft = MemoryDraft(body="Temporary deleted fact.")
    rec = provider.create_explicit(draft, request_id="restore-req")
    provider.soft_delete(rec.id)

    restored = provider.restore_recent_delete(rec.id)
    assert restored.status == "approved"
    assert restored.deleted_at is None
    assert provider.get_approved(rec.id) is not None


def test_restore_expired_undo_window():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_expired.sqlite3"
        # 1-second undo window
        provider = SqliteMemoryProvider(db_path=db_path, undo_window_seconds=1)
        draft = MemoryDraft(body="Quickly expiring delete.")
        rec = provider.create_explicit(draft, request_id="expire-req")
        provider.soft_delete(rec.id)

        time.sleep(1.5)
        with pytest.raises(UndoWindowExpiredError):
            provider.restore_recent_delete(rec.id)


def test_context_budget_prioritizes_core():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_budget.sqlite3"
        provider = SqliteMemoryProvider(db_path=db_path)

        # Core fact (50 chars)
        core_draft = MemoryDraft(body="Core: User is an experienced systems engineer.", is_core=True)
        provider.create_explicit(core_draft, request_id="core-1")

        # Ordinary fact (60 chars)
        norm_draft = MemoryDraft(body="Ordinary: User works on multiple Android projects.", is_core=False)
        provider.create_explicit(norm_draft, request_id="norm-1")

        # Set budget to fit only the core fact
        ctx = provider.load_context(max_memory_chars=55)
        assert len(ctx.records) == 1
        assert ctx.records[0].is_core is True
        assert ctx.total_chars <= 55


def test_backup_and_snapshot_export(provider):
    draft = MemoryDraft(body="Fact for backup test.", is_core=True)
    provider.create_explicit(draft, request_id="backup-req")

    with tempfile.TemporaryDirectory() as tmp_dir:
        backup_file = Path(tmp_dir) / "backup.sqlite3"
        provider.backup_to_file(backup_file)
        assert backup_file.exists()
        assert backup_file.stat().st_size > 0

        # Snapshot export
        snapshot = provider.export_snapshot()
        assert snapshot.record_count >= 1
        assert snapshot.version == 1
        assert any(r["body"] == "Fact for backup test." for r in snapshot.records)
