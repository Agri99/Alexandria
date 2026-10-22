"""Consistent backup and snapshot export/restore utilities."""

import sqlite3
import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Any, Union
from .models import MemoryExport, MemoryRecord


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
    Validates integrity check and schema version before replacing target.
    """
    backup_path = Path(backup_path).resolve()
    target_db_path = Path(target_db_path).resolve()

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found at {backup_path}")

    # 1. Integrity check on source backup
    chk_conn = sqlite3.connect(str(backup_path))
    try:
        cur = chk_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchone()
        if not res or res[0] != "ok":
            raise ValueError(f"Corrupted SQLite backup file: {res[0] if res else 'Unknown error'}")

        # Check schema_meta
        cur.execute("SELECT MAX(version) FROM schema_meta;")
        meta_row = cur.fetchone()
        if not meta_row or meta_row[0] is None or meta_row[0] < 1:
            raise ValueError("Invalid backup: schema_meta missing or invalid schema version.")
    finally:
        chk_conn.close()

    # 2. Safely restore into target via temporary file
    target_db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target_db_path.with_suffix(".restore_tmp")

    src_conn = sqlite3.connect(str(backup_path))
    dest_conn = sqlite3.connect(str(temp_target))
    try:
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()

        # Remove active WAL/SHM files for target if present before replacing
        wal_file = target_db_path.with_name(target_db_path.name + "-wal")
        shm_file = target_db_path.with_name(target_db_path.name + "-shm")
        if wal_file.exists():
            try: wal_file.unlink()
            except OSError: pass
        if shm_file.exists():
            try: shm_file.unlink()
            except OSError: pass

        temp_target.replace(target_db_path)
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
    """Exports approved records into a versioned memory snapshot."""
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


def restore_json_snapshot(snapshot: Union[MemoryExport, Dict[str, Any]], conn: sqlite3.Connection) -> int:
    """Restores memory records from an export snapshot into the target connection."""
    records = snapshot.records if isinstance(snapshot, MemoryExport) else snapshot.get("records", [])
    restored_count = 0

    with conn:
        for rec in records:
            conn.execute(
                """
                INSERT OR REPLACE INTO memories (
                    id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["id"],
                    rec["body"],
                    1 if rec.get("is_core") else 0,
                    rec.get("status", "approved"),
                    rec["created_at"],
                    rec["updated_at"],
                    rec.get("deleted_at"),
                    rec.get("client_request_id", rec["id"]),
                    rec["body_hash"],
                ),
            )
            restored_count += 1
    return restored_count


def write_json_snapshot_file(conn: sqlite3.Connection, output_path: Path) -> Path:
    """Writes JSON export snapshot to disk."""
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
