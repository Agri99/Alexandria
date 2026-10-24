"""Consistent backup and snapshot export utilities."""

import sqlite3
import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Any, List
from .models import MemoryExport
from .migrations import CURRENT_SCHEMA_VERSION


def create_sqlite_backup(src_conn: sqlite3.Connection, dest_path: Path) -> Path:
    """
    Creates a consistent SQLite backup using the SQLite Online Backup API.
    Works safely while WAL writes may be active.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest_path.with_suffix(".tmp")
    dest_conn = sqlite3.connect(str(temp_dest))
    try:
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        temp_dest.replace(dest_path)
    finally:
        if temp_dest.exists():
            temp_dest.unlink()
    return dest_path


def restore_sqlite_backup(backup_path: Path, target_db_path: Path) -> Path:
    """
    Restores an Alexandria SQLite database from a verified backup file.
    Validates integrity check, schema version, and offline target state before replacing target.
    Fails closed if the target database is locked, actively open, or corrupted.
    Preserves rollback copy of target if target exists until restore verification succeeds.
    """
    backup_path = Path(backup_path).resolve()
    target_db_path = Path(target_db_path).resolve()

    if backup_path == target_db_path:
        raise ValueError("Source backup path and target database path cannot be identical.")

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found at {backup_path}")

    if backup_path.stat().st_size == 0:
        raise ValueError("Corrupted SQLite backup file: file is empty.")

    # 1. Integrity check and schema verification on source backup
    try:
        chk_conn = sqlite3.connect(str(backup_path))
        try:
            cur = chk_conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            res = cur.fetchone()
            if not res or res[0] != "ok":
                raise ValueError(f"Corrupted SQLite backup file: {res[0] if res else 'Unknown error'}")

            # Check schema_meta
            try:
                cur.execute("SELECT MAX(version) FROM schema_meta;")
                meta_row = cur.fetchone()
            except sqlite3.OperationalError:
                meta_row = None

            if not meta_row or meta_row[0] is None or meta_row[0] < 1:
                raise ValueError("Invalid backup: schema_meta missing or invalid schema version.")

            if meta_row[0] > CURRENT_SCHEMA_VERSION:
                raise ValueError(
                    f"Invalid backup: schema version {meta_row[0]} is newer than supported version {CURRENT_SCHEMA_VERSION}."
                )

            # Check required tables exist
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row[0] for row in cur.fetchall()}
            required_tables = {"schema_meta", "memories"}
            if not required_tables.issubset(tables):
                raise ValueError(f"Invalid backup: missing required tables {required_tables - tables}.")
        finally:
            chk_conn.close()
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        raise ValueError(f"Corrupted SQLite backup file: {exc}") from exc

    # 2. Offline lock probe on target database
    if target_db_path.exists():
        try:
            probe_conn = sqlite3.connect(str(target_db_path), timeout=0.1)
            try:
                probe_conn.execute("PRAGMA locking_mode = EXCLUSIVE;")
                probe_conn.execute("BEGIN EXCLUSIVE;")
                probe_conn.execute("COMMIT;")
            finally:
                probe_conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            raise RuntimeError(
                f"Cannot restore SQLite backup: target database '{target_db_path}' is actively in use or locked. "
                "Offline restore policy requires stopping all active gateway/application connections before restoring."
            ) from exc

    # 3. Create rollback copy of target if target exists
    target_db_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_file = target_db_path.with_suffix(".rollback_bak") if target_db_path.exists() else None
    if rollback_file and target_db_path.exists():
        with open(target_db_path, "rb") as fsrc, open(rollback_file, "wb") as fdst:
            while chunk := fsrc.read(65536):
                fdst.write(chunk)

    temp_target = target_db_path.with_suffix(".restore_tmp")
    src_conn = sqlite3.connect(str(backup_path))
    dest_conn = sqlite3.connect(str(temp_target))
    try:
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()

        # Check and remove active WAL/SHM sidecars for target if present
        wal_file = target_db_path.with_name(target_db_path.name + "-wal")
        shm_file = target_db_path.with_name(target_db_path.name + "-shm")
        if wal_file.exists():
            try:
                wal_file.unlink()
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot restore SQLite backup: unable to remove active WAL sidecar '{wal_file}'. "
                    "Another process may still have the database open. Ensure offline state before restoring."
                ) from exc
        if shm_file.exists():
            try:
                shm_file.unlink()
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot restore SQLite backup: unable to remove active SHM sidecar '{shm_file}'. "
                    "Another process may still have the database open. Ensure offline state before restoring."
                ) from exc

        temp_target.replace(target_db_path)

        # 4. Post-restore verification on replaced target
        post_conn = sqlite3.connect(str(target_db_path))
        try:
            pcur = post_conn.cursor()
            pcur.execute("PRAGMA integrity_check;")
            pres = pcur.fetchone()
            if not pres or pres[0] != "ok":
                raise RuntimeError(f"Post-restore integrity check failed: {pres[0] if pres else 'Unknown error'}")
        finally:
            post_conn.close()

        # Clean up rollback copy on successful verification
        if rollback_file and rollback_file.exists():
            rollback_file.unlink()

    except Exception:
        # Restore rollback copy if target was partially touched
        if rollback_file and rollback_file.exists():
            try:
                rollback_file.replace(target_db_path)
            except Exception:
                pass
        raise
    finally:
        if temp_target.exists():
            temp_target.unlink()

    return target_db_path


def compute_file_sha256(file_path: Path) -> str:
    """Computes SHA-256 checksum of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def export_json_snapshot(conn: sqlite3.Connection) -> MemoryExport:
    """
    Exports approved records into a versioned human-readable memory snapshot.
    Strictly read-only; does not mutate database state.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash "
        "FROM memories WHERE status = 'approved' ORDER BY is_core DESC, updated_at DESC"
    )
    records = []
    for row in cur.fetchall():
        records.append({
            "id": row[0],
            "body": row[1],
            "is_core": bool(row[2]),
            "status": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "deleted_at": row[6],
            "client_request_id": row[7],
            "body_hash": row[8],
        })

    return MemoryExport(
        version=1,
        exported_at=int(time.time()),
        record_count=len(records),
        records=records,
    )


def write_json_snapshot_file(conn: sqlite3.Connection, output_path: Path) -> Path:
    """Writes human-readable JSON export snapshot to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = export_json_snapshot(conn)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "version": snapshot.version,
            "exported_at": snapshot.exported_at,
            "record_count": snapshot.record_count,
            "records": snapshot.records,
        }, f, indent=2, ensure_ascii=False)
    return output_path
