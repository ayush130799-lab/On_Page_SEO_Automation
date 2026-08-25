"""Website onboarding and management schemas."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .common import ORMModel

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RENDER_MODES = {"auto", "always", "never"}


class WebsiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: HttpUrl
    github_repo: str | None = Field(
        default=None, description='GitHub repository as "owner/repo".', max_length=255
    )
    github_branch: str | None = Field(default="main", max_length=255)
    github_framework: str | None = Field(
        default=None, description='Routing hint: "next", "nuxt", "astro", "hugo", …', max_length=50
    )
    github_path_map: dict[str, str] | None = Field(
        default=None, description="Explicit source-file → URL-path overrides."
    )
    max_pages: int | None = Field(default=None, ge=1, le=200_000)
    render_mode: str = "auto"
    crawl_delay: float | None = Field(default=None, ge=0, le=60)
    respect_robots_txt: bool = True
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None

    @field_validator("github_repo")
    @classmethod
    def validate_repo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if value.startswith("http"):
            # Accept a pasted GitHub URL and reduce it to owner/repo.
            value = value.rstrip("/").removesuffix(".git").split("github.com/")[-1]
        if not GITHUB_REPO_RE.match(value):
            raise ValueError('github_repo must be in "owner/repo" form.')
        return value

    @field_validator("render_mode")
    @classmethod
    def validate_render_mode(cls, value: str) -> str:
        if value not in RENDER_MODES:
            raise ValueError(f"render_mode must be one of {sorted(RENDER_MODES)}.")
        return value


class WebsiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: HttpUrl | None = None
    is_active: bool | None = None
    github_repo: str | None = None
    github_branch: str | None = None
    github_framework: str | None = None
    github_path_map: dict[str, str] | None = None
    max_pages: int | None = Field(default=None, ge=1, le=200_000)
    render_mode: str | None = None
    crawl_delay: float | None = Field(default=None, ge=0, le=60)
    respect_robots_txt: bool | None = None
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None

    _validate_repo = field_validator("github_repo")(WebsiteCreate.validate_repo.__func__)

    @field_validator("render_mode")
    @classmethod
    def validate_render_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in RENDER_MODES:
            raise ValueError(f"render_mode must be one of {sorted(RENDER_MODES)}.")
        return value


class IntegrationStatusSummary(BaseModel):
    provider: str
    status: str
    account_label: str | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None


class WebsiteResponse(ORMModel):
    id: int
    name: str
    url: str
    domain: str
    is_active: bool
    github_repo: str | None
    github_branch: str | None
    github_framework: str | None
    max_pages: int | None
    render_mode: str
    respect_robots_txt: bool
    include_patterns: list[str] | None
    exclude_patterns: list[str] | None
    total_pages: int
    average_seo_score: float | None
    critical_issue_count: int
    high_priority_page_count: int
    last_crawled_at: datetime | None
    last_synced_at: datetime | None
    last_scored_at: datetime | None
    created_at: datetime


class WebsiteDetailResponse(WebsiteResponse):
    github_path_map: dict[str, str] | None = None
    integrations: list[IntegrationStatusSummary] = []
