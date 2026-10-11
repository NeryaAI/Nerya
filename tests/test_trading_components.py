from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.financial.store import FinancialStore
from nerya.trading.components import TradingComponentRegistry, TradingComponentSpec, trading_components

pytestmark = pytest.mark.smoke
ADDRESS = "0x" + "1" * 40
CID = "user:venue:funds"


def config(root):
    data = deepcopy(DEFAULT_CONFIG)
    data["financial"] = {"enabled": True}
    data["runtime"]["live_trading_enabled"] = True
    data["wallet"] = {"providers": {"wallet": {"provider": "self_custody", "config": {"address": ADDRESS, "signer_ref": "vault://test-ref"}}}}
    return Config(WorkspacePaths(root), data)


def plugin(root, version="1", *, timeout=False, action="wallet_transfer"):
    directory = root / "plugins" / "venue"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "codec.py").write_text("VERSION = " + repr(version) + "\n")
    (directory / "trading.py").write_text(f'''
import time
from .codec import VERSION
from nerya.trading.components import TradingComponentSpec
class Adapter:
    def quote(self, request):
        return {{"risk_usd": request["amount"], "spend_usd": request["amount"], "fee_usd": "0",
                "asset_amounts": {{"USDC": request["amount"]}}, "available_asset_amounts": {{"USDC": "1000"}},
                "expires_at": time.time() + 60, "version": VERSION}}
    def validate(self, request, quote): pass
    def execute(self, request, quote, submitted):
        submitted({{"transaction_hash": "0x" + "a" * 64}})
        {"raise TimeoutError()" if timeout else "return {\"state\": \"confirmed\", \"version\": VERSION}"}
    def status(self, request, quote, submission):
        return {{"state": "confirmed", "version": VERSION, "observed": True}}
TRADING_COMPONENTS = (TradingComponentSpec("funds", VERSION, ({action!r},), ("transfer",), lambda c, r: Adapter()),)
''')
    return directory


def request():
    return {"kind": "wallet_transfer", "component_id": CID, "wallet_id": "wallet", "asset": "USDC",
            "chain": "base", "amount": "10", "recipient": ADDRESS}


def authorized(cfg):
    issuer = FinancialContext("operator", frozenset({"api:all"}))
    ctx = replace(issuer, task_kind="scheduled_agent", task_id="funds", security_revision="v1")
    store = FinancialStore(cfg)
    grant = store.create_grant(issuer, task_kind=ctx.task_kind, task_id=ctx.task_id, security_revision="v1", policy={
        "actions": ["wallet_transfer"], "resources": {"wallets": ["wallet"], "assets": ["USDC"], "chains": ["base"],
        "recipients": [ADDRESS], "components": [CID]}, "limits": {"single_usd": "100", "rolling_24h_usd": "100",
        "total_usd": "100", "fee_usd": "10", "asset_amounts": {"USDC": "100"}}})
    store.approve_grant(grant["grant_id"], issuer, expected_revision=1)
    return ctx


def test_workspace_isolation_and_helper_hot_reload(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    plugin(first, "1"); plugin(second, "2")
    one, two = trading_components(config(first)), trading_components(config(second))
    old = one.resolve(request())
    assert old.build(config(first), request()).quote(request())["version"] == "1"
    assert two.resolve(request()).build(config(second), request()).quote(request())["version"] == "2"
    plugin(first, "3")
    trading_components(config(first))
    assert one.resolve(request()).revision != old.revision
    assert old.build(config(first), request()).quote(request())["version"] == "1"
    assert two.resolve(request()).build(config(second), request()).quote(request())["version"] == "2"


def test_stale_quote_cannot_execute_after_reload(tmp_path):
    plugin(tmp_path); cfg = config(tmp_path); ctx = authorized(cfg)
    gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx, request(), action_key="first")
    assert action["quote"]["component_binding"]["id"] == CID
    plugin(tmp_path, "2")
    with pytest.raises(FinancialError, match="component_revision_changed"):
        gateway.execute(ctx, action["action_id"], quote_hash=action["quote_hash"])
    assert gateway.store.get_action(action["action_id"], ctx)["state"] == "prepared"


def test_uncertain_submission_uses_original_revision_after_restart_and_removal(tmp_path):
    plugin(tmp_path, timeout=True); cfg = config(tmp_path); ctx = authorized(cfg)
    gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx, request(), action_key="once")
    result = gateway.execute(ctx, action["action_id"], quote_hash=action["quote_hash"])
    assert result["state"] == "unconfirmed"
    binding = result["quote"]["component_binding"]
    (tmp_path / "plugins" / "venue" / "trading.py").unlink()
    fresh = TradingComponentRegistry(tmp_path)
    fresh.reload(cfg)
    original = fresh.resolve(request(), binding=binding, recovery=True)
    assert original.build(cfg, request()).status(request(), result["quote"], result["submission"])["version"] == "1"
    with pytest.raises(FinancialError, match="revision_changed"):
        fresh.resolve(request(), binding=binding)
    reconciled = gateway.reconcile(ctx, action["action_id"])
    assert reconciled["state"] == "confirmed" and reconciled["receipt"]["version"] == "1"
    assert gateway.execute(ctx, action["action_id"], quote_hash=action["quote_hash"])["duplicate"]


def test_failed_load_keeps_old_receipt_reader_and_blocks_new_actions(tmp_path):
    path = plugin(tmp_path); cfg = config(tmp_path); registry = trading_components(cfg)
    old = registry.resolve(request())
    (path / "trading.py").write_text("this is invalid Python !")
    report = registry.reload(cfg)
    assert report["errors"]["venue"] == "SyntaxError"
    with pytest.raises(FinancialError, match="unavailable"):
        registry.resolve(request())
    assert registry.resolve(request(), binding=old.binding(), recovery=True).revision == old.revision


def test_changed_component_cannot_reuse_finite_grant(tmp_path):
    plugin(tmp_path); cfg = config(tmp_path); ctx = authorized(cfg)
    plugin(tmp_path, "2")
    gateway = FinancialGateway(cfg)
    action = gateway.prepare(ctx, request(), action_key="new")
    result = gateway.execute(ctx, action["action_id"], quote_hash=action["quote_hash"])
    assert result["status"] == "approval_required"
    assert gateway.store.get_action(action["action_id"], ctx)["state"] == "awaiting_approval"


def test_disabled_plugin_preserves_recovery(tmp_path):
    plugin(tmp_path); cfg = config(tmp_path); registry = trading_components(cfg)
    old = registry.resolve(request())
    cfg.data["plugins"] = {"disabled": ["venue"]}
    registry.reload(cfg)
    with pytest.raises(FinancialError, match="unavailable"):
        registry.resolve(request())
    assert registry.resolve(request(), binding=old.binding(), recovery=True).revision == old.revision


def test_recovery_rejects_tampered_snapshot(tmp_path):
    plugin(tmp_path); cfg = config(tmp_path); old = trading_components(cfg).resolve(request())
    fresh = TradingComponentRegistry(tmp_path)
    snapshot = fresh.cache_dir / "venue" / old.package_revision / "codec.py"
    snapshot.write_text("VERSION = 'corrupted'\n")
    with pytest.raises(FinancialError, match="recovery_unavailable"):
        fresh.resolve(request(), binding=old.binding(), recovery=True)


def test_plugin_cannot_override_cex_order_path(tmp_path):
    plugin(tmp_path, action="trade")
    report = trading_components(config(tmp_path)).describe()
    assert report["errors"]["venue"] == "cex_components_require_exchange_provider"
    assert any(item["id"] == "builtin:trading" for item in report["components"])


def test_closed_parameters_and_contract_are_enforced(tmp_path):
    spec = TradingComponentSpec("bad", "1", ("swap",), ("spot",), lambda c, r: object())
    with pytest.raises(FinancialError, match="invalid_component_parameters"):
        spec.validate_request({"kind": "swap", "parameters": {"private_key": "bad"}})
    from nerya.trading.components import BoundComponent
    with pytest.raises(FinancialError, match="contract_incomplete"):
        BoundComponent("user:p:bad", "a" * 64, spec).build(config(tmp_path), {"kind": "swap"})


def test_symlink_cannot_import_outside_workspace(tmp_path):
    source = plugin(tmp_path / "outside")
    root = tmp_path / "workspace"
    (root / "plugins").mkdir(parents=True)
    (root / "plugins" / "venue").symlink_to(source, target_is_directory=True)
    report = trading_components(config(root)).describe()
    assert report["errors"]["venue"] == "trading_plugin_symlink_forbidden"
    assert not (root / "runtime").exists()


def test_gateway_existing_builtins_also_have_binding(tmp_path):
    cfg = config(tmp_path)
    registry = trading_components(cfg)
    component = registry.resolve({"kind": "trade"})
    assert component.id == "builtin:trading"
    assert not any(row["live_verified"] for row in registry.describe()["components"])
