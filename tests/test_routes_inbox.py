from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api import routes_inbox


pytestmark = pytest.mark.smoke


def test_collect_items_sorts_newest_first_before_severity(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_inbox,
        "_read_approvals",
        lambda _client: [
            {
                "id": "older-approval",
                "summary": "Older approval",
                "created_at": "2026-05-01T08:00:00",
            }
        ],
    )
    monkeypatch.setattr(
        routes_inbox,
        "_read_proposals",
        lambda _client: [
            {
                "id": "newer-proposal",
                "title": "Newer proposal",
                "summary": "Applied already",
                "state": "applied",
                "created_at": "2026-05-02T08:00:00",
            }
        ],
    )
    monkeypatch.setattr(routes_inbox, "_read_open_turns", lambda _client: [])
    monkeypatch.setattr(routes_inbox, "_read_messages", lambda _client: [])
    monkeypatch.setattr(routes_inbox, "_read_llm_tiers", lambda _client: [])

    items = routes_inbox._collect_items(SimpleNamespace())

    assert [item["id"] for item in items] == [
        "proposal:newer-proposal",
        "approval:older-approval",
    ]


def test_resolve_handler_accepts_batch_ids() -> None:
    result = routes_inbox._resolve_handler(
        SimpleNamespace(),
        {
            "ids": ["notification:first", "notification:second"],
            "decision": "dismiss",
        },
    )

    assert result["ok"] is True
    assert result["summary"] == "resolved 2 inbox item(s)"
    assert result["data"]["resolved_count"] == 2
    assert result["data"]["failed_count"] == 0
    assert [row["id"] for row in result["data"]["results"]] == [
        "notification:first",
        "notification:second",
    ]


def test_resolve_handler_reports_partial_batch_failures() -> None:
    result = routes_inbox._resolve_handler(
        SimpleNamespace(),
        {
            "ids": ["notification:first", "mystery:bad"],
            "decision": "dismiss",
        },
    )

    assert result["ok"] is True
    assert result["status"] == "warn"
    assert result["data"]["resolved_count"] == 1
    assert result["data"]["failed_count"] == 1
    assert result["data"]["results"][1]["ok"] is False
