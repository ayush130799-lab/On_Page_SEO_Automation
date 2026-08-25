"""Shared model primitives: the declarative base, timestamps and portable column types."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

__all__ = [
    "Base",
    "JSONColumn",
    "LongText",
    "TimestampMixin",
    "UTCDateTime",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now — used as the default for every timestamp column."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC on the way in and out.

    SQLite has no timezone type, so a value written as aware UTC comes back naive. Serialised
    without an offset, the browser then parses it as *local* time and a crawl that finished
    seconds ago renders as hours ago. Normalising here fixes it for every column at once, and
    keeps PostgreSQL and SQLite behaving identically.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


#: JSON on SQLite (tests/dev), JSONB on PostgreSQL (indexable, production).
JSONColumn = JSON().with_variant(JSONB, "postgresql")

#: Unbounded text, portable across both backends.
LongText = Text()


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` maintained by the ORM."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
