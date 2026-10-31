"""Domain models and data transfer objects for C.O.R.E. shared memory."""

from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    body: str
    is_core: bool
    status: str  # 'approved' or 'deleted'
    created_at: int
    updated_at: int
    deleted_at: Optional[int]
    client_request_id: str
    body_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        return cls(
            id=str(data["id"]),
            body=str(data["body"]),
            is_core=bool(data.get("is_core", False)),
            status=str(data.get("status", "approved")),
            created_at=int(data["created_at"]),
            updated_at=int(data["updated_at"]),
            deleted_at=int(data["deleted_at"]) if data.get("deleted_at") is not None else None,
            client_request_id=str(data.get("client_request_id", "")),
            body_hash=str(data.get("body_hash", "")),
        )


@dataclass(frozen=True)
class MemoryDraft:
    body: str
    is_core: bool = False


@dataclass(frozen=True)
class MemoryContext:
    records: List[MemoryRecord]
    total_chars: int
    estimated_tokens: int


@dataclass(frozen=True)
class MemoryExport:
    version: int
    exported_at: int
    record_count: int
    records: List[Dict[str, Any]]

