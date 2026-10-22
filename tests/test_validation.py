"""Unit tests for Alexandria input validation and secret detection."""

import pytest
from alexandria.models import MemoryDraft
from alexandria.validation import validate_draft, normalize_body
from alexandria.lifecycle import InvalidMemoryError


def test_empty_body_rejected():
    with pytest.raises(InvalidMemoryError, match="empty"):
        validate_draft(MemoryDraft(body=""))
    with pytest.raises(InvalidMemoryError, match="empty"):
        validate_draft(MemoryDraft(body="   \n\t  "))


def test_oversized_body_rejected():
    huge_body = "A" * 1501
    with pytest.raises(InvalidMemoryError, match="exceeds maximum limit"):
        validate_draft(MemoryDraft(body=huge_body))


def test_sensitive_bearer_token_rejected():
    bad_draft = MemoryDraft(body="The auth token is Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcLEO")
    with pytest.raises(InvalidMemoryError, match="credential"):
        validate_draft(bad_draft)


def test_sensitive_private_key_rejected():
    bad_draft = MemoryDraft(body="-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...")
    with pytest.raises(InvalidMemoryError, match="credential"):
        validate_draft(bad_draft)


def test_sensitive_openai_key_rejected():
    bad_draft = MemoryDraft(body="api_key = 'sk-abcdef12345678901234567890'")
    with pytest.raises(InvalidMemoryError, match="credential"):
        validate_draft(bad_draft)


def test_multi_turn_transcript_rejected():
    transcript_draft = MemoryDraft(
        body="User: Hello, how are you?\nAssistant: I am doing well, how can I help you today?"
    )
    with pytest.raises(InvalidMemoryError, match="multi-turn chat transcript"):
        validate_draft(transcript_draft)


def test_normalize_body_whitespace():
    raw = "  Hello \t  world \n\n this is   a   test  "
    norm = normalize_body(raw)
    assert norm == "Hello world this is a test"
