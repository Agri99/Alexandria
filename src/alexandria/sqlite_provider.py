"""SQLite implementation of the Alexandria MemoryProvider protocol."""

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence, Optional, List

from .models import MemoryRecord, MemoryDraft, MemoryContext, MemoryExport
from .lifecycle import (
    MemoryNotFoundError,
    RequestIdConflictError,
    UndoWindowExpiredError,
    DuplicateMemoryError,
)
from .validation import validate_draft
from .migrations import run_migrations
from .backup import (
    export_json_snapshot,
    create_sqlite_backup,
    restore_sqlite_backup,
)


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    """Helper to convert an SQLite row into an immutable MemoryRecord."""
    return MemoryRecord(
        id=row["id"],
        body=row["body"],
        is_core=bool(row["is_core"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        client_request_id=row["client_request_id"],
        body_hash=row["body_hash"],
    )


class SqliteMemoryProvider:
    """Production SQLite-backed implementation of MemoryProvider."""

    def __init__(
        self,
        db_path: Path | str,
        undo_window_seconds: int = 86400,  # 24 hours
        busy_timeout_ms: int = 5000,
    ):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.undo_window_seconds = undo_window_seconds
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.Lock()

        # Run ordered migrations on startup
        conn = self._get_connection()
        try:
            run_migrations(conn)
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Creates and configures an isolated SQLite connection."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON;")
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms};")
        cur.close()
        return conn

    def list_approved(self, *, limit: int = 200) -> Sequence[MemoryRecord]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                FROM memories
                WHERE status = 'approved'
                ORDER BY is_core DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def get_approved(self, memory_id: str) -> Optional[MemoryRecord]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                FROM memories
                WHERE id = ? AND status = 'approved'
                """,
                (memory_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_record(row)
        finally:
            conn.close()

    def create_explicit(self, draft: MemoryDraft, *, request_id: str) -> MemoryRecord:
        normalized_body, body_hash = validate_draft(draft)
        now = int(time.time())

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    # 1. Check idempotency key
                    cur.execute(
                        """
                        SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                        FROM memories
                        WHERE client_request_id = ?
                        """,
                        (request_id,),
                    )
                    existing_by_req = cur.fetchone()
                    if existing_by_req:
                        if existing_by_req["body_hash"] == body_hash:
                            return _row_to_record(existing_by_req)
                        else:
                            raise RequestIdConflictError(
                                f"Request ID {request_id} already exists with different body content."
                            )

                    # 2. Check exact duplicate among active approved memories
                    cur.execute(
                        """
                        SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                        FROM memories
                        WHERE body_hash = ? AND status = 'approved'
                        """,
                        (body_hash,),
                    )
                    existing_by_hash = cur.fetchone()
                    if existing_by_hash:
                        return _row_to_record(existing_by_hash)

                    # 3. Insert new approved memory record (saving normalized_body consistently)
                    new_id = str(uuid.uuid4())
                    try:
                        cur.execute(
                            """
                            INSERT INTO memories (
                                id, body, is_core, status, created_at, updated_at, deleted_at,
                                client_request_id, body_hash
                            ) VALUES (?, ?, ?, 'approved', ?, ?, NULL, ?, ?)
                            """,
                            (
                                new_id,
                                normalized_body,
                                1 if draft.is_core else 0,
                                now,
                                now,
                                request_id,
                                body_hash,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        if "uq_memories_active_body_hash" in str(exc) or "body_hash" in str(exc):
                            cur.execute(
                                """
                                SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                                FROM memories
                                WHERE body_hash = ? AND status = 'approved'
                                """,
                                (body_hash,),
                            )
                            existing_row = cur.fetchone()
                            if existing_row:
                                return _row_to_record(existing_row)
                            raise DuplicateMemoryError("An approved memory with this exact content already exists.") from exc
                        elif "client_request_id" in str(exc):
                            cur.execute(
                                """
                                SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                                FROM memories
                                WHERE client_request_id = ?
                                """,
                                (request_id,),
                            )
                            existing_row = cur.fetchone()
                            if existing_row and existing_row["body_hash"] == body_hash:
                                return _row_to_record(existing_row)
                            raise RequestIdConflictError(f"Request ID {request_id} already exists with different body content.") from exc
                        raise

                    return MemoryRecord(
                        id=new_id,
                        body=normalized_body,
                        is_core=draft.is_core,
                        status="approved",
                        created_at=now,
                        updated_at=now,
                        deleted_at=None,
                        client_request_id=request_id,
                        body_hash=body_hash,
                    )
            finally:
                conn.close()

    def update_explicit(self, memory_id: str, draft: MemoryDraft) -> MemoryRecord:
        normalized_body, body_hash = validate_draft(draft)
        now = int(time.time())

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT created_at, client_request_id, body_hash FROM memories WHERE id = ? AND status = 'approved'",
                        (memory_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise MemoryNotFoundError(f"Approved memory {memory_id} not found.")

                    # Check if another approved memory already exists with this exact content
                    cur.execute(
                        "SELECT id FROM memories WHERE body_hash = ? AND status = 'approved' AND id != ?",
                        (body_hash, memory_id),
                    )
                    if cur.fetchone():
                        raise DuplicateMemoryError(
                            "Cannot update: an approved memory with this exact content already exists."
                        )

                    try:
                        cur.execute(
                            """
                            UPDATE memories
                            SET body = ?, is_core = ?, body_hash = ?, updated_at = ?
                            WHERE id = ? AND status = 'approved'
                            """,
                            (normalized_body, 1 if draft.is_core else 0, body_hash, now, memory_id),
                        )
                    except sqlite3.IntegrityError as exc:
                        if "uq_memories_active_body_hash" in str(exc) or "body_hash" in str(exc):
                            raise DuplicateMemoryError(
                                "Cannot update: an approved memory with this exact content already exists."
                            ) from exc
                        raise

                    return MemoryRecord(
                        id=memory_id,
                        body=normalized_body,
                        is_core=draft.is_core,
                        status="approved",
                        created_at=row["created_at"],
                        updated_at=now,
                        deleted_at=None,
                        client_request_id=row["client_request_id"],
                        body_hash=body_hash,
                    )
            finally:
                conn.close()

    def soft_delete(self, memory_id: str) -> None:
        now = int(time.time())
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id FROM memories WHERE id = ? AND status = 'approved'",
                        (memory_id,),
                    )
                    if not cur.fetchone():
                        raise MemoryNotFoundError(f"Approved memory {memory_id} not found.")

                    cur.execute(
                        "UPDATE memories SET status = 'deleted', deleted_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, memory_id),
                    )
            finally:
                conn.close()

    def restore_recent_delete(self, memory_id: str) -> MemoryRecord:
        now = int(time.time())
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                        FROM memories
                        WHERE id = ? AND status = 'deleted'
                        """,
                        (memory_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise MemoryNotFoundError(f"Deleted memory {memory_id} not found.")

                    deleted_at = row["deleted_at"]
                    if deleted_at is not None and (now - deleted_at) >= self.undo_window_seconds:
                        raise UndoWindowExpiredError(
                            f"Undo window expired ({now - deleted_at}s >= {self.undo_window_seconds}s)."
                        )

                    # Check if an identical approved record was created while this one was deleted
                    cur.execute(
                        "SELECT id FROM memories WHERE body_hash = ? AND status = 'approved' AND id != ?",
                        (row["body_hash"], memory_id),
                    )
                    if cur.fetchone():
                        raise DuplicateMemoryError(
                            "Cannot restore: an identical approved memory already exists."
                        )

                    try:
                        cur.execute(
                            "UPDATE memories SET status = 'approved', deleted_at = NULL, updated_at = ? WHERE id = ?",
                            (now, memory_id),
                        )
                    except sqlite3.IntegrityError as exc:
                        if "uq_memories_active_body_hash" in str(exc) or "body_hash" in str(exc):
                            raise DuplicateMemoryError(
                                "Cannot restore: an identical approved memory already exists."
                            ) from exc
                        raise

                    return MemoryRecord(
                        id=row["id"],
                        body=row["body"],
                        is_core=bool(row["is_core"]),
                        status="approved",
                        created_at=row["created_at"],
                        updated_at=now,
                        deleted_at=None,
                        client_request_id=row["client_request_id"],
                        body_hash=row["body_hash"],
                    )
            finally:
                conn.close()

    def load_context(self, *, max_memory_chars: int) -> MemoryContext:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, body, is_core, status, created_at, updated_at, deleted_at, client_request_id, body_hash
                FROM memories
                WHERE status = 'approved'
                ORDER BY is_core DESC, updated_at DESC
                """
            )
            rows = cur.fetchall()

            selected: List[MemoryRecord] = []
            current_chars = 0
            for row in rows:
                body_len = len(row["body"])
                # Keep whole records only; do not truncate mid-record
                if current_chars + body_len <= max_memory_chars:
                    selected.append(_row_to_record(row))
                    current_chars += body_len

            # Conservative token estimation: ~4 chars per token
            estimated_tokens = max(1, current_chars // 4) if current_chars > 0 else 0
            return MemoryContext(
                records=selected,
                total_chars=current_chars,
                estimated_tokens=estimated_tokens,
            )
        finally:
            conn.close()

    def export_snapshot(self) -> MemoryExport:
        conn = self._get_connection()
        try:
            return export_json_snapshot(conn)
        finally:
            conn.close()

    def backup_to_file(self, dest_path: Path | str) -> Path:
        conn = self._get_connection()
        try:
            return create_sqlite_backup(conn, Path(dest_path))
        finally:
            conn.close()

    def restore_from_backup(self, backup_path: Path | str) -> Path:
        """Restores live database from a verified SQLite backup."""
        with self._lock:
            return restore_sqlite_backup(Path(backup_path), self.db_path)
