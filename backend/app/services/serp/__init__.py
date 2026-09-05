"""Live SERP competitor analysis — roadmap §4.2 / §7.4.

Word counts and heading structure for the top-N ranking pages for a keyword, plus Google's
"People Also Ask" box and related searches, via SerpApi (a licensed intermediary — see
``client.py`` for why raw Google scraping is not used). On-demand only: never wired into the
automatic crawl or nightly rescore pipelines, since every call costs money.
"""

from .analyzer import CompetitorAnalysisOutcome, analyse_competitors, latest_analysis
from .client import OrganicResult, SerpApiError, SerpResult, is_configured, search
from .competitor_analyzer import CompetitorFetch, fetch_competitors

__all__ = [
    "CompetitorAnalysisOutcome",
    "CompetitorFetch",
    "OrganicResult",
    "SerpApiError",
    "SerpResult",
    "analyse_competitors",
    "fetch_competitors",
    "is_configured",
    "latest_analysis",
    "search",
]
