from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api.routes_evolution import routes
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import (
    create_proposal,
    delete_proposal,
    list_proposals,
    set_state,
)
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from nerya.tools.native.strategy_runtime import (
    strategy_delete_proposal_handler,
    strategy_generate_proposal_handler,
)
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


# A custom main.py that is otherwise a normal script strategy but imports a
# forbidden module, which the validator always flags as a blocker.
_FORBIDDEN_IMPORT_MAIN = (
    "import requests\n"
    "from nerya.strategies import StrategyContext, StrategyResult\n\n\n"
    "def run(ctx: StrategyContext) -> StrategyResult:\n"
    "    candles = ctx.market.candles(ctx.config.markets[0], timeframe=\"1d\", limit=20)\n"
    "    if not candles:\n"
    "        return ctx.result.skip(reason=\"no_data\")\n"
    "    return ctx.result.hold(reason=\"no_signal\")\n"
)


def _invalid_request(strategy_id: str = "btc_forbidden_import") -> StrategyGenerationRequest:
    return StrategyGenerationRequest(
        strategy_id=strategy_id,
        strategy_class="trend",
        execution_mode="script",
        markets=("mock:BTC/USDT",),
        accounts=("paper_main",),
        files={"main.py": _FORBIDDEN_IMPORT_MAIN},
    )


# ---------------------------------------------------------------------------
# delete_proposal (backend)
# ---------------------------------------------------------------------------


def test_delete_proposal_removes_pending(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="BTC scalper",
        initial_state="pending_review",
    )
    pdir = proposal.path
    assert pdir.exists()

    result = delete_proposal(paths, proposal.id)

    assert result["ok"] is True
    assert result["deleted"] is True
    assert result["prev_state"] == "pending_review"
    assert not pdir.exists()
    assert all(p.id != proposal.id for p in list_proposals(paths))


def test_delete_proposal_not_found(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    result = delete_proposal(paths, "prp_does_not_exist")
    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_delete_proposal_applied_requires_force(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="BTC applied",
        initial_state="pending_review",
    )
    set_state(paths, proposal.id, "applied")

    refused = delete_proposal(paths, proposal.id)
    assert refused["ok"] is False
    assert refused["reason"] == "applied_requires_force"
    assert proposal.path.exists()

    forced = delete_proposal(paths, proposal.id, force=True)
    assert forced["ok"] is True
    assert not proposal.path.exists()


# ---------------------------------------------------------------------------
# delete route (HTTP control plane)
# ---------------------------------------------------------------------------


def _delete_route():
    return next(
        handler
        for method, path, handler in routes()
        if method == "POST" and path == "/evolution/proposals/delete"
    )


def test_delete_proposal_route_status_codes(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))
    route = _delete_route()

    missing = route(client, {})
    assert missing["_status"] == 400

    not_found = route(client, {"proposal_id": "prp_missing"})
    assert not_found["_status"] == 404

    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="route delete",
        initial_state="pending_review",
    )
    ok = route(client, {"proposal_id": proposal.id})
    assert ok["ok"] is True
    assert ok["deleted"] is True
    assert not proposal.path.exists()


def test_delete_proposal_route_static_path_wins_over_param_route(tmp_path):
    """``/proposals/delete`` must hit the delete route, not be captured by
    the ``/proposals/{proposal_id}`` detail route."""

    all_routes = routes()
    post_paths = [path for method, path, _ in all_routes if method == "POST"]
    delete_idx = post_paths.index("/evolution/proposals/delete")
    param_idx = post_paths.index("/evolution/proposals/{proposal_id}")
    assert delete_idx < param_idx


# ---------------------------------------------------------------------------
# generator validation gating (require_valid)
# ---------------------------------------------------------------------------


def test_generate_require_valid_blocks_invalid_proposal(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    generator = StrategyCodeGenerator(paths)

    out = generator.generate(
        _invalid_request(),
        validate=True,
        create_proposal_record=True,
        require_valid=True,
    )

    assert out.validation is not None
    assert out.validation.ok is False
    assert out.proposal is None
    # Nothing should have been persisted to the pending-review queue.
    assert list_proposals(paths) == []


def test_generate_without_require_valid_keeps_legacy_behavior(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    generator = StrategyCodeGenerator(paths)

    out = generator.generate(
        _invalid_request("btc_forbidden_legacy"),
        validate=True,
        create_proposal_record=True,
        require_valid=False,
    )

    assert out.validation is not None
    assert out.validation.ok is False
    assert out.proposal is not None


def test_generate_require_valid_allows_valid_proposal(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    generator = StrategyCodeGenerator(paths)

    out = generator.generate(
        StrategyGenerationRequest(
            strategy_id="btc_valid_trend",
            strategy_class="trend",
            markets=("mock:BTC/USDT",),
            accounts=("paper_main",),
        ),
        validate=True,
        create_proposal_record=True,
        require_valid=True,
    )

    assert out.validation is not None
    assert out.validation.ok is True
    assert out.proposal is not None


# ---------------------------------------------------------------------------
# native tool handlers
# ---------------------------------------------------------------------------


def test_generate_proposal_handler_returns_blockers_without_pending(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_forbidden_handler",
                "strategy_class": "trend",
                "execution_mode": "script",
                "markets": ["mock:BTC/USDT"],
                "accounts": ["paper_main"],
                "files": {"main.py": _FORBIDDEN_IMPORT_MAIN},
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    hint = result.error.recovery_hint
    assert hint["action"] == "fix_validation_blockers_and_retry"
    assert hint["tool_name"] == "strategy_generate_proposal"
    assert hint["blockers"], "expected at least one blocker in the recovery hint"
    # No pending proposal should have been written.
    assert list_proposals(paths) == []


def test_strategy_delete_proposal_handler_deletes_pending(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="handler delete",
        initial_state="pending_review",
    )

    result = strategy_delete_proposal_handler(
        ToolCall(
            name="strategy_delete_proposal",
            arguments={"proposal_id": proposal.id},
        ),
        config=cfg,
    )

    assert result.is_error is False
    assert result.content[0].data["ok"] is True
    assert result.content[0].data["deleted"] is True
    assert not proposal.path.exists()


def test_strategy_delete_proposal_handler_not_found(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})

    result = strategy_delete_proposal_handler(
        ToolCall(
            name="strategy_delete_proposal",
            arguments={"proposal_id": "prp_nope"},
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION


def test_strategy_delete_proposal_handler_applied_requires_force(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="handler applied",
        initial_state="pending_review",
    )
    set_state(paths, proposal.id, "applied")

    refused = strategy_delete_proposal_handler(
        ToolCall(
            name="strategy_delete_proposal",
            arguments={"proposal_id": proposal.id},
        ),
        config=cfg,
    )
    assert refused.is_error is True
    assert proposal.path.exists()

    forced = strategy_delete_proposal_handler(
        ToolCall(
            name="strategy_delete_proposal",
            arguments={"proposal_id": proposal.id, "force": True},
        ),
        config=cfg,
    )
    assert forced.is_error is False
    assert forced.content[0].data["ok"] is True
    assert not proposal.path.exists()
