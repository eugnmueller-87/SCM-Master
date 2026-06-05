"""The adapter port: the one method every upstream connector implements.

An adapter turns a raw feed (bytes / text just received from an upstream system)
into a :class:`~app.integrations.schemas.FeedBatch`. It does parsing and field
mapping ONLY — no database access, no business rules. That keeps each upstream's
quirks (Coupa's CSV columns, SAP's IDoc segments) isolated to one file and lets
the sync engine stay upstream-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.integrations.schemas import FeedBatch


class FeedParseError(ValueError):
    """Raised when a feed cannot be parsed/mapped (bad columns, missing keys).

    The API layer maps this to a 422 so the caller sees *why* their file was
    rejected rather than a generic 500.
    """


class Adapter(ABC):
    """Base class for every upstream connector."""

    #: Stable identifier persisted as ``source_system`` on every synced row.
    source_system: str

    @abstractmethod
    def parse(self, raw: str) -> FeedBatch:
        """Map a raw feed (decoded text) onto canonical records."""
        raise NotImplementedError
