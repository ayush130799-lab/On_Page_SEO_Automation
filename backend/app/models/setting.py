"""Runtime-configurable settings.

Resolution order is env defaults (``app.config.Settings``) → global row (``website_id IS NULL``) →
per-website row. This is what keeps the priority weights out of the application code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin

if TYPE_CHECKING:
    from .website import Website


class Setting(TimestampMixin, Base):
    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("website_id", "key", name="uq_setting_scope_key"),
        Index("ix_settings_key", "key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: NULL = global default; set = per-website override.
    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Any] = mapped_column(JSONColumn, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    website: Mapped["Website | None"] = relationship()


#: Settings keys the API is allowed to write. Anything else is rejected, so a typo cannot silently
#: create a setting that nothing reads.
SETTING_KEYS: dict[str, str] = {
    "priority_weights": "Weights for the priority engine components (must sum to 1.0).",
    "seo_weights": "Per-check weights used by the SEO health score.",
    "ai_max_pages": "Maximum number of pages sent to the LLM per run.",
    "ai_seo_score_threshold": "Pages scoring above this are skipped by the AI stage.",
    "priority_metric_window_days": "Lookback window (days) for GA4/GSC/Semrush aggregation.",
    "max_pages": "Maximum pages crawled per run.",
    "render_mode": "JavaScript rendering policy: auto | always | never.",
}
