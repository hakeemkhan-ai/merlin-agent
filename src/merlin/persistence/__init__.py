"""Transactional task persistence and execution fencing."""

from merlin.persistence.store import SQLTaskStore

__all__ = ["SQLTaskStore"]
