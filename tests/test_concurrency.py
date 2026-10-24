"""Targeted tests for Alexandria concurrency, storage contention, and lock recovery."""

import tempfile
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from alexandria.sqlite_provider import SqliteMemoryProvider
from alexandria.models import MemoryDraft


def test_concurrent_creates_across_threads():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_concurrent.sqlite3"
        provider = SqliteMemoryProvider(db_path=db_path)

        def worker(idx: int):
            draft = MemoryDraft(body=f"Fact number {idx} created concurrently.")
            return provider.create_explicit(draft, request_id=f"concurrent-req-{idx}")

        count = 12
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(worker, i) for i in range(count)]
            records = [f.result() for f in futures]

        assert len(records) == count
        approved = provider.list_approved(limit=100)
        assert len(approved) == count

        # Verify DB integrity
        raw_conn = sqlite3.connect(str(db_path))
        try:
            cur = raw_conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            assert cur.fetchone()[0] == "ok"
        finally:
            raw_conn.close()


def test_concurrent_duplicate_saves_deduplicate_safely():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_dup_threads.sqlite3"
        provider = SqliteMemoryProvider(db_path=db_path)

        def worker(idx: int):
            draft = MemoryDraft(body="Shared immutable preference across threads.")
            return provider.create_explicit(draft, request_id=f"dup-thread-req-{idx}")

        count = 10
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, i) for i in range(count)]
            records = [f.result() for f in futures]

        assert len(records) == count
        # All returned records must share the identical unique id
        first_id = records[0].id
        for r in records:
            assert r.id == first_id

        # Exactly 1 row in the approved database
        approved = provider.list_approved()
        assert len(approved) == 1
        assert approved[0].id == first_id


def test_concurrent_idempotent_retries():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_idemp_threads.sqlite3"
        provider = SqliteMemoryProvider(db_path=db_path)

        def worker():
            draft = MemoryDraft(body="Idempotent payload under identical request key.")
            return provider.create_explicit(draft, request_id="idempotent-shared-key")

        count = 8
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker) for _ in range(count)]
            records = [f.result() for f in futures]

        first_id = records[0].id
        assert all(r.id == first_id for r in records)
        assert len(provider.list_approved()) == 1


def test_lock_contention_and_controlled_error_recovery():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_lock.sqlite3"

        # Initialize provider with short busy timeout (50ms)
        provider = SqliteMemoryProvider(db_path=db_path, busy_timeout_ms=50)

        # 1. External connection locks database exclusively
        external_conn = sqlite3.connect(str(db_path))
        external_conn.execute("BEGIN EXCLUSIVE;")

        try:
            # 2. Attempt operation during lock - must fail with controlled OperationalError
            draft = MemoryDraft(body="Memory during lock contention.")
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                provider.create_explicit(draft, request_id="locked-req")
        finally:
            # 3. Release external lock
            external_conn.execute("COMMIT;")
            external_conn.close()

        # 4. Subsequent operation must recover and succeed
        recovered_rec = provider.create_explicit(
            MemoryDraft(body="Memory after lock released."), request_id="recovered-req"
        )
        assert recovered_rec is not None
        assert recovered_rec.id is not None

        # 5. Database remains completely healthy
        approved = provider.list_approved()
        assert len(approved) == 1
        assert approved[0].body == "Memory after lock released."


def test_independent_provider_instances_active_dedup():
    """Verifies that SQLite partial unique index enforces uniqueness across independent provider instances."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_independent.sqlite3"

        # Initialize provider 1 and provider 2 with separate connections
        provider1 = SqliteMemoryProvider(db_path=db_path)
        provider2 = SqliteMemoryProvider(db_path=db_path)

        def worker(provider_inst, req_id):
            draft = MemoryDraft(body="Exact fact created across independent providers.")
            return provider_inst.create_explicit(draft, request_id=req_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(worker, provider1, "indep-1")
            fut2 = executor.submit(worker, provider2, "indep-2")
            rec1 = fut1.result()
            rec2 = fut2.result()

        # Both providers return the exact same approved memory record ID
        assert rec1.id == rec2.id
        assert rec1.body == rec2.body

        # Confirm exactly 1 record in database
        approved = provider1.list_approved()
        assert len(approved) == 1


def test_deleted_and_active_coexist_and_restore_conflict():
    """Verifies that a deleted record and an active approved record can coexist, but restoring conflicts."""
    from alexandria.lifecycle import DuplicateMemoryError

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "alexandria_coexist.sqlite3"
        provider = SqliteMemoryProvider(db_path=db_path)

        # 1. Create and delete initial fact
        rec1 = provider.create_explicit(MemoryDraft(body="Coexisting preference."), request_id="co-1")
        provider.soft_delete(rec1.id)
        assert len(provider.list_approved()) == 0

        # 2. Create another active fact with identical body
        rec2 = provider.create_explicit(MemoryDraft(body="Coexisting preference."), request_id="co-2")
        assert len(provider.list_approved()) == 1

        # 3. Direct DB inspection confirms both rows exist (one deleted, one approved)
        raw_conn = sqlite3.connect(str(db_path))
        cur = raw_conn.cursor()
        cur.execute("SELECT id, status FROM memories WHERE body_hash = ?", (rec1.body_hash,))
        rows = cur.fetchall()
        raw_conn.close()
        assert len(rows) == 2
        statuses = {r[1] for r in rows}
        assert statuses == {"approved", "deleted"}

        # 4. Attempting to restore the deleted record fails with DuplicateMemoryError
        with pytest.raises(DuplicateMemoryError):
            provider.restore_recent_delete(rec1.id)
