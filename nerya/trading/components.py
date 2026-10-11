"""Versioned financial transports contributed by workspace trading plugins.

The gateway owns identity, approvals, reservations and submission state. A
component owns market/protocol facts, quotes and transport-specific recovery.
Only operator-approved code in plugins/<id>/trading.py is imported. Plugin
packages are snapshotted before import so an uncertain submission can still
be queried with its original implementation after a reload or process restart.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..financial.contracts import FinancialError, KINDS, digest

ABI_VERSION = 1
MARKET_TYPES = frozenset({"spot", "linear", "inverse", "option", "prediction", "lp", "lending", "transfer"})
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_REVISION = re.compile(r"[a-f0-9]{64}\Z")
_SUFFIXES = frozenset({".py", ".json", ".toml", ".yaml", ".yml", ".abi"})


class FinancialTransport(Protocol):
    def quote(self, request: dict) -> dict: ...
    def validate(self, request: dict, quote: dict) -> None: ...
    def execute(self, request: dict, quote: dict, submitted: Callable[[dict], None]) -> dict: ...
    def status(self, request: dict, quote: dict, submission: dict) -> dict: ...


@dataclass(frozen=True)
class TradingComponentSpec:
    id: str
    version: str
    actions: tuple[str, ...]
    market_types: tuple[str, ...]
    factory: Callable[[Any, dict], FinancialTransport]
    parameters_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    dependencies: tuple[tuple[str, str], ...] = ()
    abi_version: int = ABI_VERSION
    account_state: bool = False
    cancel: bool = False

    def __post_init__(self):
        from jsonschema import Draft202012Validator
        if self.abi_version != ABI_VERSION:
            raise FinancialError("trading_component_abi_incompatible", 422)
        if not _ID.fullmatch(self.id) or not self.version or not callable(self.factory):
            raise FinancialError("invalid_trading_component", 422)
        if not self.actions or set(self.actions) - KINDS or not self.market_types or set(self.market_types) - MARKET_TYPES:
            raise FinancialError("invalid_trading_component_capabilities", 422)
        if self.parameters_schema.get("type") != "object" or self.parameters_schema.get("additionalProperties") is not False:
            raise FinancialError("component_parameters_must_be_closed_schema", 422)
        Draft202012Validator.check_schema(self.parameters_schema)

    def validate_request(self, request):
        from jsonschema import Draft202012Validator
        if request["kind"] not in self.actions:
            raise FinancialError("unsupported_financial_capability", 422)
        if next(Draft202012Validator(self.parameters_schema).iter_errors(request.get("parameters", {})), None):
            raise FinancialError("invalid_component_parameters", 400)

    def validate_dependencies(self):
        for distribution,expected in self.dependencies:
            try:actual=importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError as exc:
                raise FinancialError("trading_component_dependency_unavailable",422) from exc
            if actual!=expected:raise FinancialError("trading_component_dependency_changed",422)

    def info(self):
        return {"version": self.version, "actions": list(self.actions), "market_types": list(self.market_types),
                "abi_version": self.abi_version, "parameters_schema": self.parameters_schema,
                "dependencies": list(self.dependencies), "account_state": self.account_state, "cancel": self.cancel}


@dataclass(frozen=True)
class BoundComponent:
    id: str
    revision: str
    spec: TradingComponentSpec
    plugin_id: str | None = None
    package_revision: str | None = None

    def binding(self):
        return {"id": self.id, "revision": self.revision, "abi_version": ABI_VERSION,
                "plugin_id": self.plugin_id, "package_revision": self.package_revision}

    def build(self, config, request):
        self.spec.validate_request(request)
        self.spec.validate_dependencies()
        adapter = self.spec.factory(config, request)
        methods = ["quote", "validate", "execute", "status"]
        if self.spec.account_state:
            methods.append("account_state")
        if self.spec.cancel:
            methods.append("cancel")
        if any(not callable(getattr(adapter, name, None)) for name in methods):
            raise FinancialError("trading_component_contract_incomplete", 422)
        return ComponentTransport(self, adapter)


class ComponentTransport:
    def __init__(self, component, adapter):
        self.component_binding = component.binding()
        self.component = component
        self.adapter = adapter

    def __getattr__(self, name):
        return getattr(self.adapter, name)


def _builtin_components():
    root=Path(__file__).parents[1]
    core_hash=hashlib.sha256()
    for directory in ("financial","trading","connectors","wallet"):
        for source in sorted((root/directory).rglob("*.py")):
            core_hash.update(source.relative_to(root).as_posix().encode())
            core_hash.update(hashlib.sha256(source.read_bytes()).digest())
    dependencies={}
    for distribution in ("ccxt","polymarket-client","eth-account","eth-abi"):
        try:dependencies[distribution]=importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:dependencies[distribution]=None
    routes = (
        ("trading", "trading_adapter", "TradingFunds", ("trade", "swap"), ("spot", "linear", "prediction")),
        ("cex_funds", "adapters", "CexFunds", ("exchange_transfer", "withdraw"), ("transfer",)),
        ("evm_funds", "adapters", "EvmFunds", ("wallet_transfer", "contract_approval"), ("transfer",)),
        ("solana_funds", "solana_funds", "SolanaFunds", ("wallet_transfer", "contract_approval"), ("transfer",)),
        ("bridge", "lifi", "LifiFunds", ("bridge_swap",), ("transfer",)),
        ("aave", "defi.aave", "AaveFunds", ("lend_supply", "lend_withdraw", "borrow", "repay"), ("lending",)),
        ("permit2", "defi.permit2", "Permit2Funds", ("permit2_approval",), ("transfer",)),
        ("uniswap", "defi.uniswap", "UniswapFunds", ("lp_add","lp_remove","lp_collect"), ("lp",)),
        ("pancakeswap", "defi.pancakeswap", "PancakeSwapFunds", ("lp_add","lp_remove","lp_collect"), ("lp",)),
        ("prediction_settlement", "defi.prediction", "PredictionRedemptionFunds", ("redeem",), ("prediction",)),
    )
    result = {}
    for name, module, adapter, actions, markets in routes:
        def factory(config, request, module=module, adapter=adapter):
            cls = getattr(importlib.import_module("nerya.financial." + module), adapter)
            return cls(config, request)
        parameters={"type":"object","properties":{"expiration":{"type":"integer","minimum":1,"maximum":2**48-1}},"required":["expiration"],"additionalProperties":False} if name=="permit2" else {"type":"object","properties":{},"additionalProperties":False}
        if name in {"uniswap", "pancakeswap"}:
            from ..financial.defi.uniswap import LP_SCHEMA
            parameters=LP_SCHEMA
        if name == 'prediction_settlement':
            from ..financial.defi.prediction import REDEMPTION_SCHEMA
            parameters = REDEMPTION_SCHEMA
        spec = TradingComponentSpec(name, "1", actions, markets, factory,parameters_schema=parameters,account_state=name in {"aave","uniswap","pancakeswap"})
        revision = digest({"abi": ABI_VERSION, "manifest": spec.info(), "source":core_hash.hexdigest(),"dependencies":dependencies})
        cid = "builtin:" + name
        result[cid] = BoundComponent(cid, revision, spec)
    return result


def _package_files(path):
    files = []
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if "__pycache__" in relative.parts or any(part.startswith(".") for part in relative.parts):
            continue
        if item.is_symlink():
            raise FinancialError("trading_plugin_symlink_forbidden", 422)
        if item.is_file() and item.suffix in _SUFFIXES:
            files.append(item)
    if len(files) > 256 or sum(item.stat().st_size for item in files) > 8 * 1024 * 1024:
        raise FinancialError("trading_plugin_package_too_large", 422)
    return files


def package_revision(path):
    fingerprint = hashlib.sha256()
    for item in _package_files(path):
        relative = item.relative_to(path).as_posix().encode()
        fingerprint.update(len(relative).to_bytes(4, "big") + relative)
        content = item.read_bytes()
        fingerprint.update(len(content).to_bytes(8, "big") + content)
    return fingerprint.hexdigest()


class TradingComponentRegistry:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self._lock = threading.RLock()
        self._active: dict[str, BoundComponent] = _builtin_components()
        self._history: dict[tuple[str, str], BoundComponent] = {}
        self._sources: dict[str, str] = {}
        self._disabled: set[str] = set()
        self.errors: dict[str, str] = {}

    @property
    def cache_dir(self):
        return self.workspace / "runtime" / "trading_plugins" / "revisions"

    def _snapshot(self, plugin_id, source, revision):
        destination = self.cache_dir / plugin_id / revision
        if destination.exists():
            if package_revision(destination) != revision:
                raise FinancialError("trading_plugin_revision_tampered", 403)
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=destination.parent))
        try:
            for item in _package_files(source):
                target = stage / item.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, target)
            if package_revision(stage) != revision:
                raise FinancialError("trading_plugin_changed_during_load", 409)
            try:
                os.rename(stage, destination)
            except OSError:
                if not destination.is_dir():raise
                if package_revision(destination) != revision:raise FinancialError("trading_plugin_revision_tampered", 403)
            return destination
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def _import(self, plugin_id, snapshot, revision):
        # Revision and workspace names prevent sys.modules leakage. Relative
        # imports resolve exclusively inside this immutable package.
        name = "nerya_trading_plugin_" + digest(str(self.workspace))[:16] + "_" + plugin_id + "_" + revision
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, snapshot / "trading.py", submodule_search_locations=[str(snapshot)])
            if spec is None or spec.loader is None:
                raise FinancialError("trading_plugin_import_failed", 422)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                for key in list(sys.modules):
                    if key == name or key.startswith(name + "."):
                        del sys.modules[key]
                raise
        declarations = getattr(module, "TRADING_COMPONENTS", None)
        if not isinstance(declarations, (tuple, list)) or not declarations:
            raise FinancialError("trading_plugin_components_required", 422)
        result = {}
        for component in declarations:
            if not isinstance(component, TradingComponentSpec):
                raise FinancialError("invalid_trading_component", 422)
            # Ordinary CEX orders retain CCXT, RiskGate and OrderTracker.
            if "trade" in component.actions:
                raise FinancialError("cex_components_require_exchange_provider", 422)
            component.validate_dependencies()
            cid = "user:" + plugin_id + ":" + component.id
            if cid in result:
                raise FinancialError("duplicate_trading_component", 422)
            bound = BoundComponent(cid, digest({"package": revision, "manifest": component.info()}), component, plugin_id, revision)
            result[cid] = bound
        return result

    def reload(self, config):
        plugins = config.get("plugins", {}) or {}
        disabled = set(plugins.get("disabled", []) or [])
        root = self.workspace / "plugins"
        with self._lock:
            self._active.update(_builtin_components())
            found = {}
            if plugins.get("enabled", True) and root.is_dir():
                for directory in sorted(root.iterdir()):
                    if directory.is_symlink():
                        if (directory / "trading.py").exists():
                            self.errors[directory.name] = "trading_plugin_symlink_forbidden"
                        continue
                    if directory.is_dir() and (directory / "trading.py").is_file() and directory.name not in disabled:
                        found[directory.name] = directory
            for plugin_id in set(self._sources) - set(found):
                self._active = {key: value for key, value in self._active.items() if value.plugin_id != plugin_id}
                del self._sources[plugin_id]
            self._disabled = disabled
            for plugin_id, directory in found.items():
                try:
                    if not _ID.fullmatch(plugin_id):
                        raise FinancialError("invalid_trading_plugin_id", 422)
                    revision = package_revision(directory)
                    if self._sources.get(plugin_id) == revision:
                        self.errors.pop(plugin_id, None)
                        continue
                    snapshot = self._snapshot(plugin_id, directory, revision)
                    staged = self._import(plugin_id, snapshot, revision)
                    # Publish all contributions from one package together.
                    self._active = {key: value for key, value in self._active.items() if value.plugin_id != plugin_id}
                    self._active.update(staged)
                    self._history.update({(key, value.revision): value for key, value in staged.items()})
                    self._sources[plugin_id] = revision
                    self.errors.pop(plugin_id, None)
                except Exception as exc:
                    self.errors[plugin_id] = getattr(exc, "code", type(exc).__name__)
            return self.describe()

    def resolve(self, request, *, binding=None, recovery=False):
        with self._lock:
            if binding:
                cid = binding.get("id", "")
                if binding.get("abi_version") != ABI_VERSION or request.get("component_id", cid) != cid:
                    raise FinancialError("financial_component_binding_mismatch", 403)
                if not recovery:
                    current = self._active.get(cid)
                    if not current or current.binding() != binding or current.plugin_id in self.errors:
                        raise FinancialError("financial_component_revision_changed", 403)
                    self._verify_snapshot(current)
                    current.spec.validate_request(request)
                    return current
                if cid.startswith("builtin:"):
                    current = self._active.get(cid)
                    if not current or current.binding() != binding:
                        raise FinancialError("financial_component_recovery_unavailable", 503)
                    current.spec.validate_request(request)
                    return current
                old = self._history.get((cid, binding.get("revision")))
                if old is None:
                    pid, revision = binding.get("plugin_id", ""), binding.get("package_revision", "")
                    if not isinstance(pid, str) or not _ID.fullmatch(pid) or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
                        raise FinancialError("financial_component_binding_mismatch", 403)
                    snapshot = self.cache_dir / pid / revision
                    if not snapshot.is_dir() or package_revision(snapshot) != revision:
                        raise FinancialError("financial_component_recovery_unavailable", 503)
                    staged = self._import(pid, snapshot, revision)
                    self._history.update({(key, value.revision): value for key, value in staged.items()})
                    old = self._history.get((cid, binding.get("revision")))
                if old is None or old.binding() != binding:
                    raise FinancialError("financial_component_binding_mismatch", 403)
                self._verify_snapshot(old)
                old.spec.validate_request(request)
                return old
            cid = request.get("component_id")
            if not cid:
                cid = {"trade": "builtin:trading", "swap": "builtin:trading",
                       "exchange_transfer": "builtin:cex_funds", "withdraw": "builtin:cex_funds",
                       "wallet_transfer": "builtin:solana_funds" if request.get("chain") == "solana" else "builtin:evm_funds",
                       "contract_approval": "builtin:solana_funds" if request.get("chain") == "solana" else "builtin:evm_funds",
                       "bridge_swap": "builtin:bridge","lend_supply":"builtin:aave","lend_withdraw":"builtin:aave",
                       "borrow":"builtin:aave","repay":"builtin:aave","permit2_approval":"builtin:permit2",
                       "lp_add":"builtin:uniswap","lp_remove":"builtin:uniswap","lp_collect":"builtin:uniswap"}.get(request["kind"])
                if request['kind'] in {'lp_add', 'lp_remove', 'lp_collect'} and request.get('protocol') == 'pancakeswap_v3':
                    cid = 'builtin:pancakeswap'
                if request['kind'] == 'redeem' and request.get('protocol') == 'polymarket_ctf':
                    cid = 'builtin:prediction_settlement'
                if not cid:
                    raise FinancialError("financial_component_id_required", 400)
            current = self._active.get(cid)
            if not current or current.plugin_id in self.errors:
                raise FinancialError("financial_component_unavailable", 422)
            self._verify_snapshot(current)
            current.spec.validate_request(request)
            return current

    def _verify_snapshot(self,component):
        if component.plugin_id:
            path=self.cache_dir/component.plugin_id/component.package_revision
            if not path.is_dir() or package_revision(path)!=component.package_revision:
                raise FinancialError("trading_plugin_revision_tampered",403)

    def describe(self):
        return {"abi_version": ABI_VERSION, "components": [
            {"id": item.id, "revision": item.revision, **item.spec.info(), "available": item.plugin_id not in self.errors,
             "live_verified": False} for item in sorted(self._active.values(), key=lambda value: value.id)], "errors": dict(self.errors)}

    def revision(self,cid):
        with self._lock:
            component=self._active.get(cid)
            if not component or component.plugin_id in self.errors:
                raise FinancialError("grant_component_resource_unavailable",422)
            return component.revision


_registries: dict[Path, TradingComponentRegistry] = {}
_registry_lock = threading.RLock()


def trading_components(config):
    root = config.paths.root.resolve()
    with _registry_lock:
        registry = _registries.setdefault(root, TradingComponentRegistry(root))
    registry.reload(config)
    return registry
