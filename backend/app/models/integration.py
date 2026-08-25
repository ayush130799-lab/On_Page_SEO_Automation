"""Third-party integration state and encrypted credential storage."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime
from .enums import IntegrationStatus

if TYPE_CHECKING:
    from .website import Website


class Integration(TimestampMixin, Base):
    """One connection between a website and a provider (gsc / ga4 / semrush / github).

    ``credentials_encrypted`` holds a Fernet-encrypted JSON blob. It is written only by
    ``app.services.integrations`` and is never serialised into an API response.
    """

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("website_id", "provider", name="uq_integration_website_provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default=IntegrationStatus.NOT_CONNECTED, nullable=False, index=True
    )

    #: Encrypted JSON credential blob (OAuth tokens, API keys, webhook secrets).
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)

    #: Non-sensitive provider configuration, safe to return to the client, e.g.
    #: ``{"site_url": "sc-domain:example.com"}`` or ``{"property_id": "123456789"}``.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    #: Display-only account identifier (email, property name, repo full name).
    account_label: Mapped[str | None] = mapped_column(String(255))

    connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    token_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_sync_status: Mapped[str | None] = mapped_column(String(30))
    last_error: Mapped[str | None] = mapped_column(Text)
    sync_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    website: Mapped["Website"] = relationship(back_populates="integrations")

    @property
    def is_connected(self) -> bool:
        return self.status in {IntegrationStatus.CONNECTED, IntegrationStatus.SYNCING}
