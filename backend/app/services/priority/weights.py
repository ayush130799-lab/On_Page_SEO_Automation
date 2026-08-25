"""Weight resolution for the priority engine.

The mandated defaults (severity 40 / GA4 30 / GSC 20 / Semrush 10) are declared once in
``app.config`` and reach the engine only through this module. Resolution order is:

    environment defaults  →  global `settings` row  →  per-website `settings` row

so an operator can retune the platform, or one website, without a deploy — and no call site ever
embeds a number.

Missing integrations are handled by **redistribution**, not zero-filling. A website with no GA4
connection renormalises the remaining weights so its pages are ranked on the signals that do exist;
zero-filling would instead compress every page's score by the same 30 points and destroy the
ranking's resolution.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models import Setting

logger = logging.getLogger(__name__)

COMPONENTS = ("seo_severity", "ga4_activity", "gsc_search", "semrush_opportunity")

#: Component → the provider that must be present for it to contribute.
COMPONENT_SOURCE = {
    "seo_severity": "seo",
    "ga4_activity": "ga4",
    "gsc_search": "gsc",
    "semrush_opportunity": "semrush",
}


def default_weights() -> dict[str, float]:
    return dict(settings.default_priority_weights)


def _setting_value(db: Session, key: str, website_id: int | None) -> Any:
    row = (
        db.query(Setting)
        .filter(Setting.key == key, Setting.website_id.is_(None) if website_id is None
                else Setting.website_id == website_id)
        .first()
    )
    return row.value if row is not None else None


def resolve_weights(db: Session, website_id: int | None = None) -> dict[str, float]:
    """Return the configured weights, normalised to sum to 1.0.

    Applies the global override first, then the per-website one, so a website can adjust a single
    component without restating the others.
    """
    weights = default_weights()

    scopes: list[int | None] = [None]
    if website_id is not None:
        scopes.append(website_id)

    for scope in scopes:
        override = _setting_value(db, "priority_weights", scope)
        if not isinstance(override, dict):
            continue
        for component in COMPONENTS:
            if component not in override:
                continue
            try:
                weights[component] = max(0.0, float(override[component]))
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric priority weight for '%s'.", component)

    return normalise(weights)


def normalise(weights: dict[str, float]) -> dict[str, float]:
    """Scale weights so they sum to 1.0, falling back to the defaults if all are zero."""
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 0:
        return default_weights()
    return {key: round(max(0.0, value) / total, 6) for key, value in weights.items()}


def redistribute(weights: dict[str, float], available_sources: set[str]) -> dict[str, float]:
    """Drop components whose data source is unavailable and renormalise the rest.

    ``seo_severity`` is always available (the crawler is the platform's own data), so a site with
    no integrations at all falls back to pure technical severity — which is exactly the MVP's
    behaviour, reached by the general rule rather than a special case.
    """
    active = {
        component: weight
        for component, weight in weights.items()
        if COMPONENT_SOURCE.get(component, component) in available_sources
    }
    if not active:
        return {"seo_severity": 1.0, "ga4_activity": 0.0, "gsc_search": 0.0,
                "semrush_opportunity": 0.0}

    normalised = normalise(active)
    return {component: normalised.get(component, 0.0) for component in COMPONENTS}


def set_weights(
    db: Session, weights: dict[str, float], website_id: int | None = None
) -> dict[str, float]:
    """Persist a weight override at global or per-website scope."""
    cleaned = {
        component: max(0.0, float(weights[component]))
        for component in COMPONENTS
        if component in weights
    }
    if not cleaned:
        from ...core.errors import ValidationError

        raise ValidationError(
            f"Provide at least one of: {', '.join(COMPONENTS)}."
        )

    merged = {**default_weights(), **cleaned}
    normalised = normalise(merged)

    row = (
        db.query(Setting)
        .filter(
            Setting.key == "priority_weights",
            Setting.website_id.is_(None) if website_id is None
            else Setting.website_id == website_id,
        )
        .first()
    )
    if row is None:
        row = Setting(
            website_id=website_id,
            key="priority_weights",
            value=normalised,
            description="Weights for the priority engine components.",
        )
        db.add(row)
    else:
        row.value = normalised
    db.commit()

    logger.info(
        "Priority weights updated for %s: %s",
        f"website {website_id}" if website_id else "all websites",
        normalised,
    )
    return normalised


def sub_weights(kind: str) -> dict[str, float]:
    """Normalised sub-weights inside one component."""
    mapping = {
        "ga4": {
            "users": settings.ga4_weight_users,
            "sessions": settings.ga4_weight_sessions,
            "conversions": settings.ga4_weight_conversions,
            "revenue": settings.ga4_weight_revenue,
        },
        "gsc": {
            "clicks": settings.gsc_weight_clicks,
            "impressions": settings.gsc_weight_impressions,
            "position": settings.gsc_weight_position,
            "ctr_gap": settings.gsc_weight_ctr_gap,
        },
        "semrush": {
            "keywords": settings.semrush_weight_keywords,
            "traffic": settings.semrush_weight_traffic,
            "striking_distance": settings.semrush_weight_striking_distance,
            "backlinks": settings.semrush_weight_backlinks,
        },
    }[kind]
    return normalise(mapping)
