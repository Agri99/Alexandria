"""MemoryProvider protocol specification for Alexandria."""

from typing import Protocol, Sequence, Optional
from .models import MemoryRecord, MemoryDraft, MemoryContext, MemoryExport


class MemoryProvider(Protocol):
    """The stable provider interface between HTTP/UI and storage."""

    def list_approved(self, *, limit: int = 200) -> Sequence[MemoryRecord]:
        """Lists approved memory records ordered by is_core DESC, updated_at DESC."""
        ...

    def get_approved(self, memory_id: str) -> Optional[MemoryRecord]:
        """Retrieves a single approved memory record by ID."""
        ...

    def create_explicit(self, draft: MemoryDraft, *, request_id: str) -> MemoryRecord:
        """
        Creates an explicitly user-saved approved memory record.
        Validates content, checks idempotency on request_id, and checks body_hash for duplicates.
        """
        ...

    def update_explicit(self, memory_id: str, draft: MemoryDraft) -> MemoryRecord:
        """Edits an approved memory record in place."""
        ...

    def soft_delete(self, memory_id: str) -> None:
        """Soft-deletes a record, immediately excluding it from context."""
        ...

    def restore_recent_delete(self, memory_id: str) -> MemoryRecord:
        """Restores a soft-deleted record within the configured undo window."""
        ...

    def load_context(self, *, max_memory_chars: int) -> MemoryContext:
        """
        Loads approved records for prompt assembly, prioritizing core profile records,
        fitting within max_memory_chars.
        """
        ...

    def export_snapshot(self) -> MemoryExport:
        """Exports a snapshot of all non-deleted records for backup/portability."""
        ...

