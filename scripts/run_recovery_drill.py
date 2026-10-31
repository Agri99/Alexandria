"""Automated SQLite Online Backup and Offline Recovery Drill for C.O.R.E."""

import hashlib
from pathlib import Path
import sqlite3
import sys
import time

# Ensure C.O.R.E. is on sys.path
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from core.backup import restore_sqlite_backup  # noqa: E402
from core.models import MemoryDraft  # noqa: E402
from core.sqlite_provider import SqliteMemoryProvider  # noqa: E402


def run_drill():
    repo_root = Path(__file__).resolve().parent.parent
    drill_dir = repo_root / "data" / "drill"
    drill_dir.mkdir(parents=True, exist_ok=True)

    source_db = drill_dir / "drill_live.sqlite3"
    backup_file = drill_dir / "drill_backup.sqlite3"
    restore_db = drill_dir / "drill_restored.sqlite3"

    for p in [source_db, backup_file, restore_db]:
        if p.exists():
            p.unlink()

    print("=== 1. POPULATING LIVE SOURCE DATABASE ===")
    provider = SqliteMemoryProvider(source_db)
    provider.create_explicit(MemoryDraft(body="User prefers concise technical responses with bullet points.", is_core=True), request_id="req-drill-1")
    provider.create_explicit(MemoryDraft(body="Primary stack is Kotlin and Python 3.12.", is_core=True), request_id="req-drill-2")
    provider.create_explicit(MemoryDraft(body="Active development workspace root is C:/Dev.", is_core=False), request_id="req-drill-3")
    provider.create_explicit(MemoryDraft(body="Local inference provider is Ollama running qwen2.5:3b.", is_core=False), request_id="req-drill-4")
    r5 = provider.create_explicit(MemoryDraft(body="Temporary note to be deleted.", is_core=False), request_id="req-drill-5")
    provider.soft_delete(r5.id)

    approved = provider.list_approved()
    print(f"Populated {len(approved)} approved records, 1 soft-deleted record.")

    print("\n=== 2. CREATING LIVE SQLITE BACKUP ===")
    start_backup = time.perf_counter()
    provider.backup_to_file(backup_file)
    backup_duration = time.perf_counter() - start_backup

    backup_bytes = backup_file.stat().st_size
    backup_sha256 = hashlib.sha256(backup_file.read_bytes()).hexdigest()
    print(f"Backup created in {backup_duration*1000:.2f}ms: size={backup_bytes} bytes")
    print(f"Backup SHA-256: {backup_sha256}")

    print("\n=== 3. VERIFYING BACKUP INTEGRITY & SCHEMA ===")
    chk = sqlite3.connect(str(backup_file))
    cur = chk.cursor()
    cur.execute("PRAGMA integrity_check;")
    integrity = cur.fetchone()[0]
    assert integrity == "ok", f"Integrity check failed: {integrity}"
    cur.execute("SELECT MAX(version) FROM schema_meta;")
    schema_ver = cur.fetchone()[0]
    assert schema_ver == 2, f"Expected schema v2, got {schema_ver}"
    cur.execute("SELECT COUNT(*) FROM memories WHERE status = 'approved';")
    app_count = cur.fetchone()[0]
    assert app_count == 4, f"Expected 4 approved records, got {app_count}"
    cur.execute("SELECT COUNT(*) FROM memories WHERE status = 'deleted';")
    del_count = cur.fetchone()[0]
    assert del_count == 1, f"Expected 1 deleted record, got {del_count}"
    chk.close()
    print("Backup passes PRAGMA integrity_check (ok), schema version (2), and record counts (4 approved, 1 deleted).")

    print("\n=== 4. EXECUTING OFFLINE RESTORE ===")
    start_restore = time.perf_counter()
    restore_sqlite_backup(backup_file, restore_db)
    restore_duration = time.perf_counter() - start_restore
    print(f"Offline restore completed in {restore_duration*1000:.2f}ms to {restore_db}")

    print("\n=== 5. POST-RESTORE VERIFICATION & READ/WRITE SMOKE TESTS ===")
    restored_provider = SqliteMemoryProvider(restore_db)
    restored_approved = restored_provider.list_approved()
    assert len(restored_approved) == 4, f"Expected 4 approved records in restored DB, got {len(restored_approved)}"
    print(f"Verified {len(restored_approved)} approved records match source state.")

    # Verify deleted record is excluded from context
    ctx = restored_provider.load_context(max_memory_chars=3000)
    assert len(ctx.records) == 4
    assert not any("Temporary note" in r.body for r in ctx.records)
    print("Verified soft-deleted records are strictly excluded from context assembly.")

    # Smoke test 1: Create a new memory
    smoke_new = restored_provider.create_explicit(MemoryDraft(body="Post-restore verification fact.", is_core=False), request_id="req-smoke-1")
    assert smoke_new.id is not None
    print("Smoke test 1 passed: Created new memory in restored database.")

    # Smoke test 2: Edit a memory
    restored_provider.update_explicit(smoke_new.id, MemoryDraft(body="Updated post-restore verification fact.", is_core=False))
    updated_rec = restored_provider.get_approved(smoke_new.id)
    assert updated_rec.body == "Updated post-restore verification fact."
    print("Smoke test 2 passed: Successfully updated memory in restored database.")

    # Smoke test 3: Verify active deduplication is enforced in restored DB
    dup_res = restored_provider.create_explicit(MemoryDraft(body="Updated post-restore verification fact.", is_core=False), request_id="req-smoke-2")
    assert dup_res.id == smoke_new.id, "Expected deduplication to return existing record ID"
    print("Smoke test 3a passed: create_explicit dedup returned existing record.")

    # Smoke test 3b: Verify partial unique index enforces uniqueness at SQLite level
    conn = restored_provider._get_connection()
    try:
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO memories (
                id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
            ) VALUES ('direct-dup', 'Updated post-restore verification fact.', 0, 'approved', ?, ?, NULL, 'req-direct', ?)
            """,
            (now, now, updated_rec.body_hash)
        )
        assert False, "Expected sqlite3.IntegrityError on partial unique index"
    except sqlite3.IntegrityError:
        print("Smoke test 3b passed: Partial unique index rejected duplicate approved row.")
    finally:
        conn.close()

    # Smoke test 4: Soft delete
    restored_provider.soft_delete(smoke_new.id)
    assert len(restored_provider.list_approved()) == 4
    print("Smoke test 4 passed: Soft-delete verified in restored database.")

    del provider
    del restored_provider
    import gc
    gc.collect()
    time.sleep(0.1)

    # Clean up drill files
    for p in [source_db, backup_file, restore_db]:
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                pass
    shm_wal = list(drill_dir.glob("*.sqlite3*"))
    for f in shm_wal:
        try:
            f.unlink()
        except PermissionError:
            pass

    print("\n=== RECOVERY DRILL RESULT: SUCCESS ===")
    return {
        "status": "PASS",
        "backup_duration_ms": round(backup_duration * 1000, 2),
        "restore_duration_ms": round(restore_duration * 1000, 2),
        "backup_size_bytes": backup_bytes,
        "backup_sha256": backup_sha256,
        "schema_version": schema_ver,
        "records_approved": app_count,
        "records_deleted": del_count,
    }


if __name__ == "__main__":
    result = run_drill()
    print("Drill metadata:", result)
