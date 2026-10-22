"""Lifecycle domain exceptions and status definitions for Alexandria."""


class AlexandriaError(Exception):
    """Base exception for all Alexandria errors."""
    pass


class InvalidMemoryError(AlexandriaError):
    """Raised when memory content fails validation (empty, oversized, sensitive)."""
    pass


class DuplicateMemoryError(AlexandriaError):
    """Raised or used to signal an exact duplicate memory already exists."""
    pass


class RequestIdConflictError(AlexandriaError):
    """Raised when a client_request_id is reused with different content."""
    pass


class MemoryNotFoundError(AlexandriaError):
    """Raised when the requested memory ID does not exist."""
    pass


class InvalidMemoryStateError(AlexandriaError):
    """Raised when an operation is invalid for current memory status."""
    pass


class UndoWindowExpiredError(AlexandriaError):
    """Raised when attempting to restore a record past the configured undo window."""
    pass


class MemoryUnavailableError(AlexandriaError):
    """Raised when storage cannot be reached or initialized."""
    pass

