"""Consistent backup and snapshot export/restore utilities."""

import sqlite3
import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Any
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
