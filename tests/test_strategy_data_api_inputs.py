"""Offline contracts for lazy, host-bounded strategy Data API inputs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.data_api.registry import DataApiRegistry, compact_data_result
from nerya.data_api.types import DataActionSpec, DataApiError
from nerya.strategies.agent_execution import validate_agent_configuration
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.strategies.context import build_strategy_context, StrategyRuntimeError
from nerya.strategies.input_context import StrategyInputContext, collect_task_context, context_prompt
from nerya.strategies.package import load_package

pytestmark = pytest.mark.smoke


def source(**overrides):
    return {"id": "rates", "provider": "data_api", "provider_id": "fixture",
            "action": "public_rates", "args": {"pool_id": "pool-one"}, "limit": 1, **overrides}


def table():
    return {"rows": [{"rate": 0, "reward": None, "confirmed": False}, {"rate": 3.2}],
            "source_url": "https://fixture.invalid/rates",
            "_envelope": {"provider": "fixture", "mode": "public_read_only", "mock": True,
                "sources": [{"url": "https://fixture.invalid/rates", "sha256": "a" * 64}],
                "rows_sha256": "b" * 64, "returned_rows_sha256": "c" * 64,
                "provider_truncated": True, "provider_limit": 2,
                "pool": {"pool_id": "pool-one", "chain": "fixture"},
                "requested_window": {"start": "2026-10-20", "end": "2026-10-22"},
                "coverage": {"completeness": "not_asserted", "missing_utc_days": ["2026-10-21"]}}}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("strategy input tests must not use network")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)


@pytest.fixture
def host(monkeypatch):
    calls, builds = [], []
    raw = table()
    def handler(ctx, args):
        calls.append((ctx, deepcopy(args)))
        return raw
    registry = DataApiRegistry()
    schema = {"type": "object", "required": ["pool_id"], "additionalProperties": False,
              "properties": {"pool_id": {"type": "string", "enum": ["pool-one"]}}}
    for action, audited in (("public_rates", True), ("private_rates", False)):
        registry.register_action(DataActionSpec(provider="fixture", action=action, title="fixture",
            description="Synthetic offline input", input_schema=schema, handler=handler, replay_input=audited))
    def factory():
        builds.append(True)
        return registry
    monkeypatch.setattr("nerya.data_api.build_data_api_registry", factory)
    return SimpleNamespace(registry=registry, calls=calls, builds=builds, raw=raw)


def runtime(tmp_path, sources=None):
    config = Config(paths=WorkspacePaths(tmp_path))
    root = config.paths.strategy("input_fixture")
    yaml_io.dump(root / "strategy.yml", {"version": 1, "strategy_id": "input_fixture",
        "title": "Aave rates from prose must not select a source", "mode": "paper",
        "entrypoint": "main.py:run", "schedule": {"type": "none"},
        "markets": ["mock:BTC"], "accounts": [], "policy": {"allow_direct_order": False},
        "data_sources": [source()] if sources is None else sources})
    (root / "main.py").write_text("def run(ctx): return ctx.result.hold(reason='fixture')\n")
    package = load_package(config.paths, "input_fixture")
    clock = SimpleNamespace(now_iso=lambda: "2026-10-28T00:00:00Z")
    return build_strategy_context(config=config, package=package, run_id="fixture-run", clock=clock), package


def test_runtime_is_lazy_bounded_once_per_source_and_run(tmp_path, host):
    ctx, package = runtime(tmp_path)
    assert host.builds == host.calls == []
    assert ctx.inputs.snapshot() == {}
    collect_task_context(StrategyAgentTask.skip("no signal"), ctx, {"sources": ["rates"]})
    assert host.builds == []
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: ctx.inputs.source("rates"), range(3)))
    expected = compact_data_result("fixture", "public_rates", table(), limit=1)
    assert results == [expected] * 3
    assert len(host.calls) == len(host.builds) == 1
    call_ctx, args = host.calls[0]
    assert call_ctx.config_like is None and call_ctx.workspace is None and call_ctx.config_data == {}
    assert args == source()["args"]
    results[0]["rows"][0]["rate"] = 999
    host.raw["_envelope"]["coverage"]["completeness"] = "changed"
    assert ctx.inputs.source("rates") == expected
    receipt = ctx.inputs.snapshot()["source:rates"]
    assert receipt["request"] == source()
    assert receipt["source"] == "data_source:rates" and receipt["run_id"] == ctx.run_id
    assert receipt["captured_at"] == "2026-10-28T00:00:00Z"
    assert "data_as_of" not in receipt
    task = StrategyAgentTask.dispatch(prompt="Review fixture", sources=["rates"])
    collect_task_context(task, ctx, {})
    context_prompt(task, package, "fixture-task")
    artifact = json.loads((package.root / task.metadata["input_context"]["path"]).read_text())
    assert artifact["published"]["source:rates"]["value"] == expected
    other, _ = runtime(tmp_path / "other-run")
    other.inputs.source("rates")
    assert len(host.calls) == 2


def test_mapping_and_explicit_source_only(tmp_path, host):
    declaration = source()
    declaration.pop("id")
    ctx, _ = runtime(tmp_path, {"rates": declaration})
    assert ctx.inputs.source("rates")["rows"][0]["rate"] == 0
    empty, _ = runtime(tmp_path / "empty", [])
    with pytest.raises(ValueError, match="unknown data source"):
        empty.inputs.source("rates")
    assert len(host.calls) == 1


def test_defillama_adapter_envelope_survives_sdk_with_fake_http(tmp_path, host):
    from nerya.data_api.defillama import defillama_specs, ORIGIN
    pool_id = "aa70268e-4b52-42bf-a116-608b370f9501"
    token_address = "0x" + "a" * 40
    calls = []
    class FakeHttp:
        def request(self, method, url, **kwargs):
            calls.append(url)
            assert method == "GET" and kwargs["timeout"] <= 15
            if url == ORIGIN + "/pools":
                rows = [{"pool": pool_id, "project": "aave-v3", "chain": "Ethereum",
                         "symbol": "USDC", "underlyingTokens": [token_address]}]
            else:
                assert url == ORIGIN + "/chart/" + pool_id
                rows = [{"timestamp": day + "T00:00:00Z", "apyBase": 0, "apyReward": None}
                        for day in ("2026-10-20", "2026-10-22")]
            return 200, {"status": "success", "data": rows}
    produced = []
    from dataclasses import replace
    for spec in defillama_specs(FakeHttp()):
        def capture(ctx, args, handler=spec.handler):
            value = handler(ctx, args)
            produced.append(deepcopy(value))
            return value
        host.registry.register_action(replace(spec, handler=capture))
    declaration = source(provider_id="defillama", action="yield_history",
        args={"pool_id": pool_id, "start": "2026-10-20", "end": "2026-10-22", "limit": 2})
    ctx, package = runtime(tmp_path, [declaration])
    assert calls == []
    result = ctx.inputs.source("rates")
    assert len(calls) == 2
    assert result["_envelope"] == produced[0]["_envelope"]
    assert result["row_count"] == 2 and len(result["rows"]) == 1 and result["truncated"]
    assert result["_envelope"]["coverage"]["missing_utc_days"] == ["2026-10-21"]
    assert result["rows"][0]["deposit_base_apy_percent"] == 0
    assert result["rows"][0]["deposit_reward_apy_percent"] is None
    task = StrategyAgentTask.dispatch(prompt="Offline evidence", sources=["rates"])
    collect_task_context(task, ctx, {})
    context_prompt(task, package, "defi-fixture")
    saved = json.loads((package.root / task.metadata["input_context"]["path"]).read_text())
    assert saved["published"]["source:rates"]["value"] == result
    assert len(calls) == 2


@pytest.mark.parametrize("patch", [
    {"provider_id": ""}, {"provider_id": "https://fixture.invalid"}, {"action": None},
    {"action": "python reader.py"}, {"args": []}, {"args": None},
    {"args": {"rate": float("nan")}}, {"args": {"rate": object()}},
    {"args": {"pool_id": "x" * 32_000}}, {"limit": 0}, {"limit": 501},
    {"limit": True}, {"limit": 1.2}, {"market": "mock:BTC"}, {"timeframe": "1h"},
    {"url": "https://fixture.invalid"}, {"script": "reader.py"}, {"account": "private"},
    {"provider": "runtime.market"}, {"provider": None}, {"provider": []},
])
def test_malformed_config_never_dispatches(host, patch):
    declaration = source(**patch)
    with pytest.raises(ValueError):
        validate_agent_configuration({"data_sources": [declaration]})
    inputs = StrategyInputContext([declaration], None, None, (), "run",
                                  _data_api_reader=host.registry.call_replay_input)
    with pytest.raises(ValueError):
        inputs.source("rates")
    assert host.calls == []


@pytest.mark.parametrize("field", ["id", "provider", "provider_id", "action", "args"])
def test_required_declaration_fields(host, field):
    declaration = source()
    declaration.pop(field)
    with pytest.raises(ValueError):
        validate_agent_configuration({"data_sources": [declaration]})
    assert host.calls == []


@pytest.mark.parametrize("args", [{}, {"pool_id": True}, {"pool_id": "other-pool"},
    {"pool_id": "pool-one", "url": "https://fixture.invalid"},
    {"pool_id": "pool-one", "account_id": "private"},
    {"pool_id": "pool-one", "script": "reader.py"}])
def test_action_schema_rejected_before_handler_and_cached(tmp_path, host, args):
    ctx, _ = runtime(tmp_path, [source(args=args)])
    with pytest.raises(DataApiError) as error:
        ctx.inputs.source("rates")
    assert error.value.kind == "schema_validation"
    with pytest.raises(ValueError):
        ctx.inputs.source("rates")
    assert len(host.builds) == 1 and not host.calls


@pytest.mark.parametrize("action", ["private_rates", "unknown"])
def test_unaudited_actions_are_refused(tmp_path, host, action):
    ctx, _ = runtime(tmp_path, [source(action=action)])
    with pytest.raises(DataApiError) as error:
        ctx.inputs.source("rates")
    assert error.value.kind == "permission_denied"
    assert not host.calls


def test_missing_producer_requires_publication(host):
    inputs = StrategyInputContext([source()], None, None, (), "run")
    with pytest.raises(ValueError, match="no host read-only producer"):
        inputs.source("rates")
    prepared = compact_data_result("fixture", "public_rates", table(), limit=1)
    inputs.publish("source:rates", prepared, source="host_fixture")
    assert inputs.source("rates") == prepared
    assert host.builds == host.calls == []


def test_historical_mock_ctx_never_builds_registry_or_fetches(host):
    from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
    from nerya.skills.builtin.backtest.scripts.mock_ctx import MockCtx, MockState, SimpleConfigView
    cfg = BacktestConfig.from_raw({"markets": ["mock:BTC"], "tf": "1h"})
    ctx = MockCtx(strategy_id="fixture", market_name="mock:BTC", bars_by_market={},
        current_bar={"ts": 1792454400, "close": 1}, pending_orders=[], config_obj=cfg, state=MockState(),
        config=SimpleConfigView(strategy_id="fixture", markets=("mock:BTC",),
                                extras={"data_sources": [source()]}))
    with pytest.raises(ValueError, match="historical"):
        ctx.inputs.source("rates")
    prepared = table()
    ctx.inputs.publish("source:rates", prepared, source="historical_fixture", data_as_of="2026-10-20T00:00:00Z")
    assert ctx.inputs.source("rates") == prepared
    assert ctx.inputs.snapshot()["source:rates"]["data_as_of"] == "2026-10-20T00:00:00Z"
    assert host.builds == host.calls == []


@pytest.mark.parametrize("when", ["before", "during"])
def test_run_deadline_refuses_fetch_or_late_publication(tmp_path, host, monkeypatch, when):
    ctx, _ = runtime(tmp_path)
    if when == "before":
        ctx.run_deadline.trigger("fixture timeout")
    else:
        original = host.registry.call_replay_input
        def interrupted(*args):
            result = original(*args)
            ctx.run_deadline.trigger("fixture timeout")
            return result
        monkeypatch.setattr(host.registry, "call_replay_input", interrupted)
    with pytest.raises(StrategyRuntimeError, match="timed out"):
        ctx.inputs.source("rates")
    with pytest.raises(ValueError, match="timed out"):
        ctx.inputs.source("rates")
    assert len(host.calls) == (0 if when == "before" else 1)
    assert ctx.inputs.snapshot()["source:rates"]["value"] is None


@pytest.mark.parametrize("damage", ["object", "rows", "envelope", "hash", "bytes", "nan", "hidden_nan"])
def test_invalid_producer_output_is_bounded_and_not_retried(tmp_path, host, damage):
    if damage == "object":
        host.raw.pop("rows")
    elif damage == "rows":
        host.raw["rows"] = [{}] * 10_001
    elif damage == "envelope":
        host.raw.pop("_envelope")
    elif damage == "hash":
        host.raw["_envelope"]["sources"][0]["sha256"] = "missing"
    elif damage == "bytes":
        host.raw["rows"][0]["note"] = "long note " * 410_000
    elif damage == "hidden_nan":
        host.raw["rows"][1]["rate"] = float("nan")
    else:
        host.raw["rows"][0]["rate"] = float("nan")
    ctx, _ = runtime(tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            ctx.inputs.source("rates")
    assert len(host.calls) == 1
    assert ctx.inputs.snapshot()["source:rates"]["errors"]


def test_empty_provider_table_is_preserved_without_inventing_coverage(tmp_path, host):
    host.raw["rows"] = []
    ctx, _ = runtime(tmp_path)
    value = ctx.inputs.source("rates")
    assert value["rows"] == [] and value["row_count"] == 0
    assert value["_envelope"] == host.raw["_envelope"]


def test_secrets_stay_redacted_while_public_hashes_survive(tmp_path, host):
    host.raw["rows"][0]["api_key"] = "synthetic-not-a-real-key"
    ctx, _ = runtime(tmp_path)
    value = ctx.inputs.source("rates")
    assert value["rows"][0]["api_key"]["__redacted__"] is True
    assert value["_envelope"]["sources"][0]["sha256"] == "a" * 64


def test_invalid_selection_does_not_fetch(tmp_path, host):
    ctx, _ = runtime(tmp_path)
    with pytest.raises(ValueError, match="declared args"):
        ctx.inputs.source("rates", market="mock:BTC")
    assert host.calls == host.builds == []


def test_legacy_market_news_and_custom_publication_are_unchanged():
    calls = []
    market = SimpleNamespace(candles=lambda *a, **kw: calls.append((a, kw)) or [{"close": 0}])
    news = SimpleNamespace(fetch=lambda **kw: calls.append(kw) or [])
    inputs = StrategyInputContext([
        {"id": "bars", "timeframe": "1h"},
        {"id": "news", "provider": "runtime.news", "sources": ["fixture"]},
        {"id": "custom", "provider": "custom.fixture"},
    ], market, news, ("mock:BTC",), "run")
    for _ in range(2):
        assert inputs.source("bars") == [{"close": 0}]
        assert inputs.source("news") == []
    assert len(calls) == 2
    with pytest.raises(ValueError, match="publish its actual provider output"):
        inputs.source("custom")
    inputs.publish("source:custom", {"zero": 0, "false": False})
    assert inputs.source("custom") == {"zero": 0, "false": False}
