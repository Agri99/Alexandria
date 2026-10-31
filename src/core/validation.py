"""Input validation, body normalization, and sensitive pattern checking."""

import re
import unicodedata
import hashlib
from .models import MemoryDraft
from .lifecycle import InvalidMemoryError

MAX_MEMORY_CHARS = 1500

# Patterns for obviously sensitive credentials and transcript markers
SENSITIVE_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9-_]{10,}\.[A-Za-z0-9-_]{10,}\.[A-Za-z0-9-_]{10,}\b"),  # JWT token
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style API keys
    re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b"),  # Google API key
    re.compile(r"\bBearer\s+[A-Za-z0-9-_]{20,}\b", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*['\"][A-Za-z0-9-_]{12,}['\"]", re.IGNORECASE),
]

TRANSCRIPT_PATTERNS = [
    re.compile(r"^(?:User|Human|Assistant|System)\s*:\s*.+\n+(?:User|Human|Assistant|System)\s*:\s*.+", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
]


def normalize_body(body: str) -> str:
    """Consistently normalizes text using Unicode NFKC and internal whitespace collapsing."""
    if not body:
        return ""
    normalized = unicodedata.normalize("NFKC", body)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def compute_body_hash(normalized_body: str) -> str:
    """Calculates SHA-256 hash of the normalized body representation."""
    return hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()


def validate_draft(draft: MemoryDraft) -> tuple[str, str]:
    """
    Validates a memory draft.
    Returns tuple of (normalized_body, body_hash).
    Raises InvalidMemoryError if draft fails quality or security checks.
    """
    raw_body = draft.body if draft.body is not None else ""
    if not raw_body or not raw_body.strip():
        raise InvalidMemoryError("Memory body cannot be empty.")

    if len(raw_body) > MAX_MEMORY_CHARS:
        raise InvalidMemoryError(
            f"Memory body exceeds maximum limit of {MAX_MEMORY_CHARS} characters (received {len(raw_body)})."
        )

    # Check for secret patterns
    for pat in SENSITIVE_PATTERNS:
        if pat.search(raw_body):
            raise InvalidMemoryError("Memory body contains credential, token, or sensitive key pattern.")

    # Check for transcript-shaped text
    for pat in TRANSCRIPT_PATTERNS:
        if pat.search(raw_body):
            raise InvalidMemoryError("Memory body appears to be a multi-turn chat transcript rather than a concise fact.")

    normalized = normalize_body(raw_body)
    if not normalized:
        raise InvalidMemoryError("Normalized memory body is empty.")

    b_hash = compute_body_hash(normalized)
    return normalized, b_hash

