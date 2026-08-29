"""Tests for PageResolver path normalization and GA4 page matching."""

import pytest
from app.services.integrations.matching import _normalise_path, PageResolver


def test_normalise_path_handles_trailing_slash_and_query_strings():
    assert _normalise_path("/shirdi-yatra/") == "/shirdi-yatra"
    assert _normalise_path("/shirdi-yatra?utm_source=google") == "/shirdi-yatra"
    assert _normalise_path("shirdi-yatra") == "/shirdi-yatra"
    assert _normalise_path("/SHIRDI-YATRA") == "/shirdi-yatra"
    assert _normalise_path("/path%2Fsubpath") == "/path/subpath"
