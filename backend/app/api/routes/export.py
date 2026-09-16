"""Export the current SEO audit state of a website as a downloadable .xlsx workbook."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response

from ...core.deps import DbSession, ReadableWebsite
from ...core.ratelimit import default_rate_limit
from ...services.excel_export import generate_seo_excel_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["export"])


@router.post("/websites/{website_id}/export/excel", dependencies=[Depends(default_rate_limit)])
def export_excel(website: ReadableWebsite, db: DbSession):
    """Generate an Excel workbook of this website's current SEO audit data.

    ``ReadableWebsite`` both validates the website exists and checks the caller's authorization
    to see it (returning 404 for either — see core/deps.py), so both happen before any workbook
    work starts. Every figure in the file is read from data already collected by the crawler,
    the rule engine, the AI recommendation stage and the connected integrations — nothing here
    recomputes a score with different logic than the dashboard uses.
    """
    report = generate_seo_excel_report(db, website)
    logger.info(
        "Generated Excel export for website %s (%d bytes) requested by user.",
        website.id, len(report.content),
    )
    return Response(
        content=report.content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{report.filename}"',
            "Content-Length": str(len(report.content)),
        },
    )
