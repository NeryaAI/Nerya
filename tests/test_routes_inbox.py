from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api import routes_inbox


pytestmark = pytest.mark.smoke


def test_items_skip_deleted_session_history_and_preserve_other_errors(tmp_path, monkeypatch):
    from nerya.agent import workbench
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.command_store import CommandError
    from test_conversation_commands import config, payload

    cfg = config(tmp_path)
    runtime = CommandRuntime(cfg, lambda *_: {})
    for sid in ("deleted-session", "live-session"):
        request = payload(cid="command-" + sid)
        request["session_id"] = sid
        runtime.submit(request, start=False)
    with runtime.store.transaction() as con:
        con.execute("INSERT INTO agent_deleted_sessions(session_id,deleted_at) VALUES (?,1)",
                    ("deleted-session",))
        con.executemany("""INSERT INTO agent_interactions
            (interaction_id,session_id,turn_id,kind,state,payload_json,created_at,updated_at)
            VALUES (?,?, 'historical-turn','question','pending','{}',?,1)""",
            [("deleted-question", "deleted-session", 2), ("live-question", "live-session", 1)])
        before = {table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
                  for table in ("agent_commands", "agent_interactions", "agent_deleted_sessions")}
    client = SimpleNamespace(config=cfg)
    monkeypatch.setattr(routes_inbox, "_read_proposals", lambda _: [
        {"id": "panel-proposal", "state": "pending_review"}])
    monkeypatch.setattr(routes_inbox, "_read_messages", lambda _: [])
    query = {"type": "notification,proposal"}
    result = routes_inbox._items_handler(client, query)
    assert result["ok"] is True
    items = {item["id"]: item for item in result["data"]["items"]}
    assert set(items) == {"interaction:live-question", "proposal:panel-proposal"}
    assert any(a["id"] == "approve" for a in items["proposal:panel-proposal"]["actions"])
    with runtime.store.transaction() as con:
        assert before == {table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
                          for table in before}
    original = workbench.session_view
    for error in (CommandError("session_not_found", 404), RuntimeError("projection failed")):
        def fail_live(config, sid):
            if sid == "live-session":
                raise error
            return original(config, sid)
        monkeypatch.setattr(workbench, "session_view", fail_live)
        with pytest.raises(type(error)) as caught:
            routes_inbox._items_handler(client, query)
        assert caught.value is error


def test_proposal_actions_and_resolve_preserve_approval_and_producer_outcomes(tmp_path, monkeypatch):
    from nerya.core.paths import WorkspacePaths
    from nerya.evolution import promotion, rollback
    from nerya.evolution.patch_proposal import create_proposal, list_proposals

    paths = WorkspacePaths(root=tmp_path)
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))
    proposal = create_proposal(paths, kind="strategy_config_patch",
        summary="Explicit operator review", initial_state="pending_review")

    def actions(state):
        item = routes_inbox._proposal_item({"id": proposal.id, "state": state})
        return {a["id"]: a for a in item["actions"]}

    for state in ("draft", "proposed", "pending_review"):
        assert set(actions(state)) == {"approve", "reject", "open"}
    assert actions("pending_review")["approve"]["href"] == f"/evolution/proposals/{proposal.id}/approve"
    assert set(actions("approved")) == {"apply", "open"}
    assert set(actions("applied")) == {"rollback", "open"}
    for state in ("rejected", "rolled_back", "superseded"):
        assert set(actions(state)) == {"open"}
    assert routes_inbox._proposal_item({"state": "approved"})["requires_action"] is True

    def resolve(decision):
        return routes_inbox._resolve_handler(client, {"id": "proposal:" + proposal.id, "decision": decision})

    for decision in ("apply", "rollback"):
        result = resolve(decision)
        assert result["ok"] is False and result["status"] in {"blocked", "error"}
        assert result["data"]["outcome"]["reason"] == "state_pending_review"
        assert "state_pending_review" in result["summary"]
    for decision in ("", "dismiss", "unknown"):
        assert resolve(decision)["ok"] is False
    assert list_proposals(paths)[0].state == "pending_review"
    approved = resolve("approve")
    assert approved["ok"] is True
    assert approved["data"]["outcome"]["state"] == "approved"
    assert list_proposals(paths)[0].state == "approved"
    rejected = resolve("reject")
    assert rejected["ok"] is True and list_proposals(paths)[0].state == "rejected"
    refused = resolve("approve")
    assert refused["ok"] is False
    assert refused["data"]["outcome"]["error"] == "proposal_not_open"
    for module, name, decision in ((promotion, "apply_proposal", "apply"),
                                    (rollback, "rollback_proposal", "rollback")):
        for outcome in ({"ok": False, "reason": "candidate_bundle_conflict"}, {"ok": True}):
            monkeypatch.setattr(module, name, lambda *_, result=outcome: result)
            result = resolve(decision)
            assert result["ok"] is outcome["ok"]
            assert result["data"]["outcome"] == outcome


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
