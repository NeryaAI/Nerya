"""Explicit producer timeframe regression; isolated files, no services or DB."""
from copy import deepcopy

import pytest

from nerya.api.routes_strategies_runtime import _request_from_payload
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator, StrategyGenerationInputError, StrategyGenerationRequest,
)
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
from nerya.skills.builtin.backtest.scripts.engine import _views_from_strategy_config
from nerya.skills.builtin.backtest.scripts.mock_ctx import MockCtx, MockState
from nerya.strategies.config_view import StrategyConfig
from nerya.strategies.validator import validate_proposal_files
from nerya.tools.native import strategy_runtime
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def isolated_producer(monkeypatch):
    # Keep real file staging and static validation; exclude event DB and execution.
    monkeypatch.setattr("nerya.evolution.event_store.record_event", lambda *a, **kw: None)
    def static_validation(**kwargs):
        return validate_proposal_files(**{**kwargs, "smoke_test": False, "run_tests": False})
    monkeypatch.setattr("nerya.evolution.strategy_code_generator.validate_proposal_files", static_validation)
    monkeypatch.setattr(strategy_runtime, "validate_proposal_files", static_validation)
    def blocked(*args, **kwargs):
        pytest.fail("timeframe producer tests must not access network or DB")
    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("sqlite3.connect", blocked)


def args(**overrides):
    return {"strategy_id": "explicit_timeframe", "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"], "create_tuning": False, **overrides}


def draft(tmp_path, payload):
    return strategy_runtime._strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments=payload),
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
    )


def staged_manifest(tmp_path, result):
    assert not result.is_error, result.error
    return yaml_io.load(tmp_path / result.content[0].data["proposal_paths"]["strategy_yml_path"])


@pytest.mark.parametrize("timeframe", ["1h", "15m"])
def test_draft_timeframe_reaches_manifest_and_mock_context(tmp_path, timeframe):
    manifest = staged_manifest(tmp_path, draft(tmp_path, args(timeframe=timeframe, schedule_every_seconds=60)))
    assert manifest["timeframe"] == timeframe
    assert manifest["schedule"]["every_seconds"] == 60
    config = BacktestConfig.from_raw({"markets": manifest["markets"], "tf": timeframe})
    view, policy = _views_from_strategy_config(manifest["strategy_id"], config, manifest)
    runtime = StrategyConfig(strategy_id=manifest["strategy_id"], extras=manifest)
    bar = {"ts": 1700006400, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
    market = manifest["markets"][0]
    ctx = MockCtx(strategy_id=manifest["strategy_id"], market_name=market,
                  bars_by_market={market: [bar]}, current_bar=bar, pending_orders=[],
                  config_obj=config, state=MockState(), config=view, policy_obj=policy)
    assert ctx.config.timeframe == runtime.timeframe == timeframe
    assert ctx.config.get("timeframe") == timeframe
    assert len(ctx.market.candles(market, timeframe=ctx.config.timeframe, limit=1)) == 1


def test_omitted_timeframe_is_not_inferred(tmp_path):
    manifest = staged_manifest(tmp_path, draft(tmp_path, args(
        prompt="Use 1h candles", schedule_every_seconds=3600)))
    assert "timeframe" not in manifest


@pytest.mark.parametrize("value", ["", "  ", 60, False, [], {}, None])
def test_draft_invalid_timeframe_rejected_before_writes(tmp_path, value):
    result = draft(tmp_path, args(timeframe=value))
    assert result.is_error
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert result.error.recovery_hint["field"] == ["timeframe"]
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("value", ["", "  ", 60, False, [], {}])
def test_direct_generator_invalid_timeframe_rejected_before_writes(tmp_path, value):
    with pytest.raises(StrategyGenerationInputError, match="timeframe"):
        StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
            StrategyGenerationRequest(**args(timeframe=value)))
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("request_tf", [None, "4h", "1h"])
def test_authored_manifest_preserved_or_conflict_rejected(tmp_path, request_tf):
    payload = args(files={"strategy.yml": yaml_io.dumps({"timeframe": "4h"})})
    if request_tf is not None:
        payload["timeframe"] = request_tf
    before = deepcopy(payload)
    result = draft(tmp_path, payload)
    assert payload == before
    if request_tf == "1h":
        assert result.is_error
        assert result.error.recovery_hint["field"] == ["files", "strategy.yml", "timeframe"]
        assert list(tmp_path.rglob("*")) == []
    else:
        assert staged_manifest(tmp_path, result)["timeframe"] == "4h"


def test_clone_can_change_timeframe_without_mutating_its_source(tmp_path):
    paths = WorkspacePaths(tmp_path)
    source = paths.strategy("original")
    source.mkdir(parents=True)
    original = "strategy_id: original\ntimeframe: 4h\n"
    (source / "strategy.yml").write_text(original)
    payload = {"strategy_id": "cloned", "from_strategy_id": "original"}
    changed = draft(tmp_path, {**payload, "strategy_id": "one_hour_clone", "timeframe": "1h"})
    assert staged_manifest(tmp_path, changed)["timeframe"] == "1h"
    assert staged_manifest(tmp_path, draft(tmp_path, payload))["timeframe"] == "4h"
    assert (source / "strategy.yml").read_text() == original


@pytest.mark.parametrize("timeframe", [None, "30m"])
def test_api_request_maps_timeframe_through_shared_generator(tmp_path, timeframe):
    payload = args()
    if timeframe is not None:
        payload["timeframe"] = timeframe
    request = _request_from_payload(payload)
    assert request.timeframe == timeframe
    generated = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, validate=False, create_proposal_record=False)
    assert yaml_io.loads(generated.files["strategy.yml"]).get("timeframe") == timeframe
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("value", ["", "  ", 60, False, [], {}, None])
def test_api_invalid_timeframe_rejected_before_writes(tmp_path, value):
    with pytest.raises(StrategyGenerationInputError, match="timeframe"):
        request = _request_from_payload(args(timeframe=value))
        StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(request)
    assert list(tmp_path.rglob("*")) == []
