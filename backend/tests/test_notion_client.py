"""Tests for Notion API client helpers."""

from services.notion_client import normalize_notion_uuid


def test_normalize_notion_uuid_adds_hyphens() -> None:
    compact = "a1b2c3d4e5f6789012345678901234ab"
    assert normalize_notion_uuid(compact) == "a1b2c3d4-e5f6-7890-1234-5678901234ab"


def test_normalize_notion_uuid_preserves_dashed_form() -> None:
    dashed = "a1b2c3d4-e5f6-7890-1234-5678901234ab"
    assert normalize_notion_uuid(dashed) == dashed


def test_normalize_notion_uuid_strips_whitespace() -> None:
    compact = "  a1b2c3d4e5f6789012345678901234ab  "
    assert normalize_notion_uuid(compact) == "a1b2c3d4-e5f6-7890-1234-5678901234ab"


def test_normalize_notion_uuid_invalid_length_unchanged() -> None:
    assert normalize_notion_uuid("not-a-uuid") == "not-a-uuid"
