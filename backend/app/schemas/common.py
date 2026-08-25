"""Shared response primitives."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for schemas read directly from SQLAlchemy instances."""

    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """Envelope for offset-paginated list endpoints."""

    total: int = Field(description="Total rows matching the filter, ignoring pagination.")
    limit: int
    offset: int
    items: list[T]


class MessageResponse(BaseModel):
    message: str


class IdResponse(BaseModel):
    id: int
