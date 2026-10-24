"""Consistent backup and snapshot export/restore utilities."""

import sqlite3
import json
import hashlib
import time
from pathlib import Path
from typing import Dict, Any, Union, Set
from .models import MemoryExport, MemoryRecord
from .validation import normalize_body, compute_body_hash


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
    Fails closed if the target database is locked or actively open.
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

    # 3. Safely restore into target via temporary file
    target_db_path.parent.mkdir(parents=True, exist_ok=True)
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


def validate_json_snapshot_data(snapshot: Union[MemoryExport, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pre-validates snapshot metadata and every individual record before modifying storage.
    Raises ValueError with descriptive reason if snapshot is invalid, incompatible, or corrupt.
    """
    if isinstance(snapshot, MemoryExport):
        version = snapshot.version
        record_count = snapshot.record_count
        records = snapshot.records
    elif isinstance(snapshot, dict):
        if "version" not in snapshot:
            raise ValueError("Malformed snapshot: missing 'version' field.")
        version = snapshot["version"]
        if "record_count" not in snapshot:
            raise ValueError("Malformed snapshot: missing 'record_count' field.")
        record_count = snapshot["record_count"]
        if "records" not in snapshot:
            raise ValueError("Malformed snapshot: missing 'records' field.")
        records = snapshot["records"]
    else:
        raise ValueError("Invalid snapshot: expected MemoryExport instance or dictionary.")

    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError(f"Unsupported snapshot version: {version}. Alexandria only supports version 1.")

    if not isinstance(records, list):
        raise ValueError("Malformed snapshot: 'records' must be a list.")

    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count != len(records):
        raise ValueError(
            f"Snapshot record_count mismatch: header declares {record_count}, but found {len(records)} records."
        )

    seen_ids: Set[str] = set()
    seen_req_ids: Set[str] = set()
    validated_records: List[Dict[str, Any]] = []

    required_fields = (
        "id",
        "body",
        "is_core",
        "status",
        "created_at",
        "updated_at",
        "client_request_id",
        "body_hash",
    )

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ValueError(f"Record at index {idx} is not a valid object.")

        missing = [f for f in required_fields if f not in rec]
        if missing:
            raise ValueError(f"Record at index {idx} missing required fields: {missing}")

        rec_id = rec["id"]
        if not isinstance(rec_id, str) or not rec_id.strip():
            raise ValueError(f"Record at index {idx} has invalid id: must be non-empty string.")
        if rec_id in seen_ids:
            raise ValueError(f"Duplicate record ID '{rec_id}' in snapshot.")
        seen_ids.add(rec_id)

        req_id = rec["client_request_id"]
        if not isinstance(req_id, str) or not req_id.strip():
            raise ValueError(f"Record '{rec_id}' has invalid client_request_id: must be non-empty string.")
        if req_id in seen_req_ids:
            raise ValueError(f"Duplicate client_request_id '{req_id}' in snapshot.")
        seen_req_ids.add(req_id)

        body = rec["body"]
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"Record '{rec_id}' has empty or invalid body.")

        is_core = rec["is_core"]
        if not isinstance(is_core, (bool, int)) or (isinstance(is_core, int) and is_core not in (0, 1)):
            raise ValueError(f"Record '{rec_id}' has invalid is_core: expected boolean or 0/1.")

        status = rec["status"]
        if status not in ("approved", "deleted"):
            raise ValueError(f"Record '{rec_id}' has invalid status: '{status}'. Expected 'approved' or 'deleted'.")

        created_at = rec["created_at"]
        if not isinstance(created_at, int) or isinstance(created_at, bool) or created_at <= 0:
            raise ValueError(f"Record '{rec_id}' has invalid created_at timestamp.")

        updated_at = rec["updated_at"]
        if not isinstance(updated_at, int) or isinstance(updated_at, bool) or updated_at < created_at:
            raise ValueError(f"Record '{rec_id}' has updated_at earlier than created_at.")

        deleted_at = rec.get("deleted_at")
        if deleted_at is not None:
            if not isinstance(deleted_at, int) or isinstance(deleted_at, bool) or deleted_at < created_at:
                raise ValueError(f"Record '{rec_id}' has invalid deleted_at timestamp.")
        if status == "deleted" and deleted_at is None:
            raise ValueError(f"Record '{rec_id}' marked as deleted but missing deleted_at timestamp.")

        # Hash and normalization consistency
        body_hash = rec["body_hash"]
        if not isinstance(body_hash, str) or len(body_hash) != 64:
            raise ValueError(f"Record '{rec_id}' has invalid body_hash format (expected 64 hex characters).")

        normalized = normalize_body(body)
        expected_hash = compute_body_hash(normalized)
        if body_hash.lower() != expected_hash.lower():
            raise ValueError(
                f"Record '{rec_id}' body_hash mismatch: declared '{body_hash}', expected '{expected_hash}'."
            )

        validated_records.append({
            "id": rec_id,
            "body": normalized,
            "is_core": 1 if is_core else 0,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "deleted_at": deleted_at,
            "client_request_id": req_id,
            "body_hash": expected_hash,
        })

    return validated_records


def restore_json_snapshot(snapshot: Union[MemoryExport, Dict[str, Any]], conn: sqlite3.Connection) -> int:
    """
    Restores memory records from an export snapshot into the target connection.
    Validates snapshot version, record count, and per-record consistency BEFORE modifying database.
    Performs atomic insertion within an explicit transaction to ensure rollback if any write fails.
    """
    validated_records = validate_json_snapshot_data(snapshot)
    restored_count = 0

    with conn:
        for rec in validated_records:
            conn.execute(
                """
                INSERT OR REPLACE INTO memories (
                    id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["id"],
                    rec["body"],
                    rec["is_core"],
                    rec["status"],
                    rec["created_at"],
                    rec["updated_at"],
                    rec["deleted_at"],
                    rec["client_request_id"],
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
