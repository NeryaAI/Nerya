from __future__ import annotations

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal, list_proposals
from nerya.tools.native.strategy_runtime import (
    strategy_draft_proposal_handler,
    strategy_validate_handler,
    strategy_submit_proposal_handler,
)
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


# A main.py that misuses the StrategyContext facade (ctx.account_id), which the
# validator flags as a hard blocker. Used to prove submit refuses to leave draft.
_INVALID_ACCOUNT_ID_MAIN = (
    "from nerya.strategies import StrategyContext, StrategyResult\n\n\n"
    "def run(ctx: StrategyContext) -> StrategyResult:\n"
    "    account = ctx.account_id\n"
    "    return ctx.result.hold(reason=\"no_signal\")\n"
)

# A minimal-but-real promoted package used to seed an iteration draft.
_PROMOTED_MAIN = (
    "from nerya.strategies import StrategyContext, StrategyResult\n\n\n"
    "def run(ctx: StrategyContext) -> StrategyResult:\n"
    "    candles = ctx.market.candles(ctx.config.markets[0], timeframe=\"1d\", limit=20)\n"
    "    if not candles:\n"
    "        return ctx.result.skip(reason=\"no_data\")\n"
    "    return ctx.result.hold(reason=\"no_signal\")\n"
)
_PROMOTED_YML = (
    "id: btc_live\n"
    "title: BTC live\n"
    "class: trend\n"
    "mode: paper\n"
    "markets:\n"
    "  - mock:BTC/USDT\n"
    "accounts:\n"
    "  - paper_main\n"
    "schedule:\n"
    "  type: cron\n"
    "  cron: \"*/5 * * * *\"\n"
)


def _cfg(tmp_path) -> tuple[WorkspacePaths, Config]:
    paths = WorkspacePaths(root=tmp_path)
    return paths, Config(paths=paths, data={})


def _draft_new(cfg, strategy_id="btc_intraday", **overrides):
    args = {
        "strategy_id": strategy_id,
        "strategy_class": "trend",
        "markets": ["mock:BTC/USDT"],
        "accounts": ["paper_main"],
    }
    args.update(overrides)
    return strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments=args), config=cfg
    )


def _submit(cfg, proposal_id):
    return strategy_submit_proposal_handler(
        ToolCall(
            name="strategy_submit_proposal",
            arguments={"proposal_id": proposal_id},
        ),
        config=cfg,
    )


def _validate(cfg, proposal_id):
    return strategy_validate_handler(
        ToolCall(
            name="strategy_validate",
            arguments={"proposal_id": proposal_id},
        ),
        config=cfg,
    )


# ---------------------------------------------------------------------------
# strategy_draft_proposal (scaffold)
# ---------------------------------------------------------------------------


def test_draft_scaffolds_draft_proposal(tmp_path):
    paths, cfg = _cfg(tmp_path)

    result = _draft_new(cfg)

    assert result.is_error is False
    data = result.content[0].data
    assert data["action"] == "strategy_draft_proposal"
    assert data["state"] == "draft"
    assert data["seeded_from"] == "template"
    assert data["proposal_id"].startswith("prp_")

    # The proposal exists and is parked as a draft, NOT in the review queue.
    proposals = {p.id: p for p in list_proposals(paths)}
    assert data["proposal_id"] in proposals
    assert proposals[data["proposal_id"]].state == "draft"

    # Staged files actually exist under the after/strategies tree.
    paths_map = data["proposal_paths"]
    main_file = paths.root / paths_map["main_path"]
    assert main_file.exists()
    assert (paths.root / paths_map["strategy_yml_path"]).exists()

    # next_steps must point the agent at edit -> validate -> submit.
    steps = " ".join(data["next_steps"]).lower()
    assert "edit_file" in steps or "write_file" in steps
    assert "strategy_submit_proposal" in steps


def test_validate_normalizes_bybit_perpetual_manifest_market(tmp_path):
    paths, cfg = _cfg(tmp_path)

    result = _draft_new(
        cfg,
        strategy_id="sol_ema_crossover",
        title="Bybit SOLUSDT linear perpetual EMA",
        description="Paper Bybit linear perpetual contract strategy.",
        markets=["BYBIT_PERPETUAL:SOLUSDT"],
        accounts=["bybit_paper"],
    )
    assert result.is_error is False
    proposal_id = result.content[0].data["proposal_id"]
    manifest = (
        paths.evolution
        / "proposals"
        / proposal_id
        / "after"
        / "strategies"
        / "sol_ema_crossover"
        / "strategy.yml"
    )
    data = yaml_io.load(manifest, default={})
    data["description"] = "Simple EMA on Bybit SOLUSDT linear perpetual."
    data["markets"] = ["BYBIT:SOLUSDT"]
    yaml_io.dump(manifest, data)

    validation = _validate(cfg, proposal_id)

    assert validation.is_error is False
    repaired = yaml_io.load(manifest, default={})
    assert repaired["markets"] == ["BYBIT_PERPETUAL:SOLUSDT"]


def test_validate_normalizes_byreal_generic_pool_market(tmp_path):
    paths, cfg = _cfg(tmp_path)

    result = _draft_new(
        cfg,
        strategy_id="sol_meme_pool_watcher",
        title="Byreal Solana meme pool watcher",
        description="Watch new Byreal Solana meme pools.",
        markets=["BYREAL_ONCHAIN:solana"],
        accounts=["paper_main"],
    )
    assert result.is_error is False
    proposal_id = result.content[0].data["proposal_id"]
    manifest = (
        paths.evolution
        / "proposals"
        / proposal_id
        / "after"
        / "strategies"
        / "sol_meme_pool_watcher"
        / "strategy.yml"
    )
    data = yaml_io.load(manifest, default={})
    data["markets"] = ["byreal:SOL_MEME_POOL"]
    yaml_io.dump(manifest, data)

    validation = _validate(cfg, proposal_id)

    assert validation.is_error is False
    repaired = yaml_io.load(manifest, default={})
    assert repaired["markets"] == ["BYREAL_ONCHAIN:solana"]


def test_submit_onchain_meme_strategy_uses_paper_replay_next_action(tmp_path):
    _, cfg = _cfg(tmp_path)

    result = _draft_new(
        cfg,
        strategy_id="sol_meme_pool_watcher",
        title="Byreal meme pool watcher",
        description="Watch new Byreal Solana meme pools.",
        markets=["BYREAL_ONCHAIN:solana"],
        accounts=["paper_main"],
    )
    assert result.is_error is False

    submitted = _submit(cfg, result.content[0].data["proposal_id"])

    assert submitted.is_error is False
    data = submitted.content[0].data
    assert data["backtest_required"] is False
    assert data["next_required_action"]["type"] == "paper_replay_or_custom_evidence"


def test_draft_requires_market_and_account(tmp_path):
    _, cfg = _cfg(tmp_path)

    no_markets = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={"strategy_id": "x", "accounts": ["paper_main"]},
        ),
        config=cfg,
    )
    assert no_markets.is_error is True
    assert no_markets.error.kind == ToolErrorKind.SCHEMA_VALIDATION

    no_accounts = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={"strategy_id": "x", "markets": ["mock:BTC/USDT"]},
        ),
        config=cfg,
    )
    assert no_accounts.is_error is True
    assert no_accounts.error.kind == ToolErrorKind.SCHEMA_VALIDATION


def _write_accounts(cfg, rows: list[dict]) -> None:
    yaml_io.dump(cfg.paths.accounts_file, {"accounts": rows})


def test_draft_auto_binds_matching_paper_account_when_omitted(tmp_path):
    """Omitting accounts auto-binds the matching active paper account so the

    agent does not need a separate account_list round-trip."""
    _, cfg = _cfg(tmp_path)
    _write_accounts(
        cfg,
        [
            {
                "id": "paper_main",
                "venue": "mock",
                "exchange": "mock",
                "mode": "paper",
                "status": "active",
                "initial_balance_usd": 10_000,
            }
        ],
    )

    result = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={
                "strategy_id": "auto_bind",
                "strategy_class": "trend",
                "markets": ["mock:BTC/USDT"],
            },
        ),
        config=cfg,
    )

    assert result.is_error is False
    data = result.content[0].data
    assert data["state"] == "draft"
    assert data["auto_selected_accounts"] == ["paper_main"]


def test_draft_omitted_account_errors_with_available_list(tmp_path):
    """When no safe account matches, the error lists available accounts so the

    agent can pick in one step (real-money accounts are never auto-bound)."""
    _, cfg = _cfg(tmp_path)
    _write_accounts(
        cfg,
        [
            {
                "id": "binance_live",
                "venue": "binance",
                "exchange": "binance",
                "mode": "live",
                "status": "active",
                "live_trading_enabled": True,
                "initial_balance_usd": 5_000,
            }
        ],
    )

    result = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={"strategy_id": "x", "markets": ["mock:BTC/USDT"]},
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    hint = result.error.recovery_hint or {}
    available_ids = {row["id"] for row in hint.get("available_accounts", [])}
    assert "binance_live" in available_ids


def test_draft_requires_strategy_id(tmp_path):
    _, cfg = _cfg(tmp_path)
    result = strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments={}), config=cfg
    )
    assert result.is_error is True
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION


# ---------------------------------------------------------------------------
# strategy_submit_proposal (validate gate -> pending)
# ---------------------------------------------------------------------------


def test_draft_then_submit_clean_moves_to_pending(tmp_path):
    paths, cfg = _cfg(tmp_path)

    draft = _draft_new(cfg, strategy_id="btc_submit_ok")
    proposal_id = draft.content[0].data["proposal_id"]

    submitted = _submit(cfg, proposal_id)

    assert submitted.is_error is False
    data = submitted.content[0].data
    assert data["action"] == "strategy_submit_proposal"
    assert data["state"] == "pending_review"
    assert data["validation"]["ok"] is True
    assert data["backtest_required"] is True

    proposals = {p.id: p for p in list_proposals(paths)}
    assert proposals[proposal_id].state == "pending_review"


def test_submit_with_blockers_keeps_draft(tmp_path):
    paths, cfg = _cfg(tmp_path)

    draft = _draft_new(cfg, strategy_id="btc_submit_blocked")
    payload = draft.content[0].data
    proposal_id = payload["proposal_id"]

    # Simulate the agent editing the staged main.py into an invalid state.
    main_file = paths.root / payload["proposal_paths"]["main_path"]
    main_file.write_text(_INVALID_ACCOUNT_ID_MAIN, encoding="utf-8")

    submitted = _submit(cfg, proposal_id)

    assert submitted.is_error is True
    assert submitted.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    hint = submitted.error.recovery_hint
    assert hint["action"] == "fix_validation_blockers_and_resubmit"
    assert hint["tool_name"] == "strategy_submit_proposal"
    assert hint["proposal_id"] == proposal_id
    assert hint["blockers"], "expected at least one blocker in the recovery hint"
    assert any("account_id" in str(b.get("message", "")) for b in hint["blockers"])

    # The proposal must remain a draft — it never enters pending review dirty.
    proposals = {p.id: p for p in list_proposals(paths)}
    assert proposals[proposal_id].state == "draft"


def test_submit_unknown_proposal(tmp_path):
    _, cfg = _cfg(tmp_path)
    result = _submit(cfg, "prp_missing")
    assert result.is_error is True
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION


def test_submit_requires_proposal_id(tmp_path):
    _, cfg = _cfg(tmp_path)
    result = strategy_submit_proposal_handler(
        ToolCall(name="strategy_submit_proposal", arguments={}), config=cfg
    )
    assert result.is_error is True
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION


def test_submit_rejects_non_strategy_proposal(tmp_path):
    paths, cfg = _cfg(tmp_path)
    other = create_proposal(
        paths,
        kind="provider_proposal",
        summary="not a strategy",
        initial_state="draft",
    )
    result = _submit(cfg, other.id)
    assert result.is_error is True
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert "strategy package" in result.error.message.lower()


# ---------------------------------------------------------------------------
# iteration: strategy_draft_proposal(from_strategy_id=...)
# ---------------------------------------------------------------------------


def _seed_promoted(paths, strategy_id="btc_live"):
    root = paths.strategies / strategy_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text(_PROMOTED_MAIN, encoding="utf-8")
    (root / "strategy.yml").write_text(_PROMOTED_YML, encoding="utf-8")
    (root / "strategy.md").write_text("# BTC live\n", encoding="utf-8")
    # Runtime artifacts that must NOT be copied into the seeded draft.
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "tick.json").write_text("{}", encoding="utf-8")
    return root


def test_draft_from_promoted_seeds_files(tmp_path):
    paths, cfg = _cfg(tmp_path)
    _seed_promoted(paths, "btc_live")

    result = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={
                "strategy_id": "btc_live_v2",
                "from_strategy_id": "btc_live",
            },
        ),
        config=cfg,
    )

    assert result.is_error is False
    data = result.content[0].data
    assert data["seeded_from"] == "promoted"
    assert data["iterated_from"] == "btc_live"
    assert data["state"] == "draft"

    # Authored files are seeded into the new draft; runtime artifacts are not.
    assert "main.py" in data["files"]
    assert "strategy.yml" in data["files"]
    assert not any(f.startswith("runs/") for f in data["files"])

    paths_map = data["proposal_paths"]
    staged_main = (paths.root / paths_map["main_path"]).read_text(encoding="utf-8")
    assert staged_main == _PROMOTED_MAIN

    proposals = {p.id: p for p in list_proposals(paths)}
    assert proposals[data["proposal_id"]].state == "draft"


def test_draft_from_promoted_missing_source_errors(tmp_path):
    _, cfg = _cfg(tmp_path)
    result = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={
                "strategy_id": "btc_live_v2",
                "from_strategy_id": "does_not_exist",
            },
        ),
        config=cfg,
    )
    assert result.is_error is True
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
