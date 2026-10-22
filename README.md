# Alexandria

Alexandria is the source of truth for the small set of shared, durable memory records used by Raphael and F.R.I.D.A.Y. It is not a transcript store and does not generate answers.

In accordance with Revision 3.2 FINAL of the Alexandria Architecture Specification, Alexandria starts as a Python module behind a stable `MemoryProvider` protocol with a dedicated SQLite database file, rather than being deployed as a standalone microservice in v1.

## Key Design Principles

1. In-Process Provider Module: Alexandria operates directly inside the restricted gateway process. It shares the gateway lifecycle and authentication model without introducing a separate network hop, port, or TLS stack.
2. Explicit User Memory Save: Deliberate phone saves create approved records immediately upon preview and confirmation. Model-suggested memories (deferred to future milestones) remain candidate records requiring trusted owner review.
3. No Raw Transcripts: Conversation history remains local to the client (Room database on Android). Raw prompts and multi-turn chat dialogues are strictly rejected by the validator.
4. Exact Duplicate Detection and Idempotency: Duplicate prevention uses SHA-256 hashes of Unicode NFKC-normalized text. Idempotent retries are enforced using a client-supplied unique `client_request_id`.
5. Soft-Delete and Undo Window: Soft-deleted memories are immediately excluded from prompt context assembly and can only be restored within a configured undo window (default: 24 hours).
6. Online Backups and Zero-Data-Loss Migrations: SQLite Online Backup API provides live consistent snapshots during active WAL writes. Ordered schema migrations verify integrity and fail closed without silently destroying existing data.

## Module Structure

```
src/alexandria/
    __init__.py          # Module exports
    models.py            # MemoryRecord, MemoryDraft, MemoryContext, MemoryExport
    provider.py          # MemoryProvider Protocol definition
    sqlite_provider.py   # SQLite implementation with WAL and transaction isolation
    validation.py        # Input sanitation, secret scanning, and whitespace normalization
    lifecycle.py         # Domain error definitions and status enforcement
    migrations.py        # Ordered schema migrations with schema_meta tracking
    backup.py            # SQLite online backup API and snapshot export tools
tests/
    test_provider.py     # Provider CRUD, idempotency, duplicate detection, and context tests
    test_validation.py   # Secret scanning, transcript rejection, and normalization tests
    test_migrations.py   # Schema versioning and migration idempotency tests
```

## Database Schema (v1)

```sql
CREATE TABLE schema_meta (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    is_core INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('approved', 'deleted')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted_at INTEGER,
    client_request_id TEXT NOT NULL UNIQUE,
    body_hash TEXT NOT NULL
);

CREATE INDEX idx_memories_status_core ON memories(status, is_core, updated_at DESC);
CREATE INDEX idx_memories_body_hash ON memories(body_hash);
CREATE INDEX idx_memories_client_request_id ON memories(client_request_id);
```

## Usage Example

```python
from pathlib import Path
from alexandria import SqliteMemoryProvider, MemoryDraft

# Initialize provider (creates database and applies migrations automatically)
provider = SqliteMemoryProvider(db_path=Path("data/alexandria.sqlite3"))

# Explicit user save (approved immediately)
draft = MemoryDraft(
    body="User prefers concise technical explanations with concrete steps.",
    is_core=True
)
record = provider.create_explicit(draft, request_id="client-generated-uuid-1")

# Assemble context within prompt character budget
context = provider.load_context(max_memory_chars=3000)
for memory in context.records:
    print(f"[{'CORE' if memory.is_core else 'FACT'}] {memory.body}")
```

## Running Tests

Install test dependencies and run pytest:

```bash
pip install -r requirements.txt
pytest
```

