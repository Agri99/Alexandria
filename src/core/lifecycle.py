"""Lifecycle domain exceptions and status definitions for C.O.R.E. (Central Operational Repository Engine)."""


class CoreError(Exception):
    """Base exception for all C.O.R.E. errors."""
    pass


# Backward-compatibility alias
AlexandriaError = CoreError


class InvalidMemoryError(CoreError):
    """Raised when memory content fails validation (empty, oversized, sensitive)."""
    pass


class DuplicateMemoryError(CoreError):
    """Raised or used to signal an exact duplicate memory already exists."""
    pass


class RequestIdConflictError(CoreError):
    """Raised when a client_request_id is reused with different content."""
    pass


class MemoryNotFoundError(CoreError):
    """Raised when the requested memory ID does not exist."""
    pass


class InvalidMemoryStateError(CoreError):
    """Raised when an operation is invalid for current memory status."""
    pass


class UndoWindowExpiredError(CoreError):
    """Raised when attempting to restore a record past the configured undo window."""
    pass


class MemoryUnavailableError(CoreError):
    """Raised when storage cannot be reached or initialized."""
    pass

