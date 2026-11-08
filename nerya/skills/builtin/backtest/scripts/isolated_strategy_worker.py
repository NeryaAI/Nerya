"""Trusted bootstrap executed only inside the historical strategy Seatbelt.

Python attribute guards are compatibility boundaries, not a security sandbox.
The OS boundary and host's independent protocol validation are authoritative.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import resource
import sys
from types import MappingProxyType
import zoneinfo

_wire = None
_frame = None
_limit = 256 * 1024
_methods = frozenset()


def _normal(value, depth=0):
    if depth > 32:
        raise ValueError("JSON nesting limit exceeded")
    if isinstance(value, Enum):
        return _normal(value.value, depth + 1)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [_normal(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(type(key) is str for key in value):
        return {key: _normal(item, depth + 1) for key, item in value.items()}
    if isinstance(value, MappingProxyType):
        return _normal(dict(value), depth + 1)
    # These hooks execute only in the untrusted child, never in the host.
    method = getattr(value, "asdict", None)
    if callable(method):
        return _normal(method(), depth + 1)
    raise ValueError("SDK pipe values must be finite JSON")


def _write(value):
    raw = json.dumps(_normal(value), allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(raw) > _limit:
        raise ValueError("worker message byte limit exceeded")
    _wire.write(raw)
    _wire.flush()


def _read():
    raw = sys.stdin.buffer.readline(_limit + 1)
    if not raw:
        raise EOFError("host pipe closed")
    if len(raw) > _limit or not raw.endswith(b"\n"):
        raise ValueError("host message byte limit exceeded")
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    if not isinstance(value, dict):
        raise ValueError("host frame must be an object")
    return value


class JsonObject(dict):
    """Local JSON read model: supports mapping and public attribute access."""
    def __getattribute__(self, name):
        if name == "__class__":
            # Python ABC/isinstance checks inspect a mapping's type. This is
            # detached public JSON, with no host objects or capabilities.
            return dict.__getattribute__(self, name)
        if name == "__deepcopy__":
            # SDK candle_snapshots deep-copies returned rows. Return ordinary
            # detached JSON, never a proxy or a reference into host state.
            return lambda memo: _normal(self)
        if name.startswith("_"):
            raise AttributeError("private attributes are unavailable")
        try:
            return dict.__getattribute__(self, name)
        except AttributeError:
            if name in self:
                return self[name]
            raise

    def asdict(self):
        return dict(self)


def _objects(value):
    if isinstance(value, dict):
        return JsonObject({key: _objects(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_objects(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class PublicView:
    __slots__ = ("_values",)

    def __init__(self, values):
        object.__setattr__(self, "_values", values)

    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError("private attributes are unavailable")
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        raise AttributeError("context/config is read-only")

    def __getitem__(self, key):
        if not isinstance(key, str) or key.startswith("_"):
            raise KeyError(key)
        return object.__getattribute__(self, "_values")[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class ConfigView(PublicView):
    """Immutable bindings, detached dicts matching StrategyConfig.params/get."""
    def __getattribute__(self, name):
        value = PublicView.__getattribute__(self, name)
        return _normal(value) if isinstance(value, MappingProxyType) else value

    def __getitem__(self, key):
        value = PublicView.__getitem__(self, key)
        return _normal(value) if isinstance(value, MappingProxyType) else value


def _rpc(method, args, kwargs):
    if _frame is None or method not in _methods:
        raise AttributeError("SDK operation unavailable")
    _write({"type": "rpc", "frame": _frame, "method": method, "args": args, "kwargs": kwargs})
    response = _read()
    if set(response) != {"type", "frame", "value"} or response["type"] != "reply" or response["frame"] != _frame:
        raise ValueError("unexpected host RPC reply")
    return _objects(response["value"])


class RemoteSDK(PublicView):
    def __init__(self, name):
        values = {}
        def bind(method):
            def invoke(*args, **kwargs):
                return _rpc(method, args, kwargs)
            return invoke
        for method in _methods:
            group, leaf = method.split(".")
            if group == name:
                values[leaf] = bind(method)
        super().__init__(values)


def _context(snapshot):
    from nerya.strategies.config_view import StrategyConfig
    from nerya.strategies.context import StrategyClock
    from nerya.strategies.result import ResultBuilder

    raw = dict(snapshot.get("config") or {})
    known = {"strategy_id", "title", "mode", "markets", "accounts", "news_sources", "extras"}
    extras = {**{key: value for key, value in raw.items() if key not in known}, **raw.get("extras", {})}
    config = StrategyConfig(strategy_id=raw.get("strategy_id", snapshot.get("strategy_id", "")),
        title=raw.get("title", ""), mode=raw.get("mode", "paper"), markets=raw.get("markets", ()),
        accounts=raw.get("accounts", ()), news_sources=raw.get("news_sources", ()), extras=extras)
    config_values = {key: _freeze(config[key]) for key in known}
    config_values.update({key: _freeze(value) for key, value in config.extras.items() if not key.startswith("_")})
    config_values.update(params=_freeze(config.params), timeframe=config.timeframe)
    clock_data = snapshot.get("clock") or {}
    stamp = snapshot.get("clockts", clock_data.get("ts"))
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        raise ValueError("clockts must be finite closed-bar seconds")
    clock = StrategyClock()
    clock.freeze(iso=datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(), ms=int(stamp * 1000))
    values = {key: _objects(value) for key, value in snapshot.items() if key not in {"config", "clockts", "clock", "policy"}}
    values.update(config=ConfigView(config_values), policy=ConfigView({key: _freeze(value) for key, value in (snapshot.get("policy") or {}).items()}),
                  clock=PublicView({"now": clock.now, "now_ms": clock.now_ms, "now_ts_ms": clock.now_ms,
                                    "now_iso": clock.now_iso, "ts": stamp}),
                  result=ResultBuilder(), mode="backtest", runmode="backtest")
    for group in ("state", "market", "portfolio", "trading", "subagents", "audit"):
        values[group] = RemoteSDK(group)
    values["market_data"] = values["market"]
    values["symbol"] = snapshot.get("symbol", snapshot.get("market_name", ""))
    for name in ("open_position", "close_position", "reduce_position"):
        values[name] = getattr(values["trading"], name)
    return PublicView(values)


def main():
    global _wire, _frame, _limit, _methods
    request = json.loads(Path(sys.argv[1]).read_text())
    for kind, value in ((resource.RLIMIT_CPU, request["cpu_seconds"]), (resource.RLIMIT_FSIZE, request["file_bytes"]),
                        (resource.RLIMIT_NOFILE, 128), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(kind, (value, value))
    # stdout belongs exclusively to the protocol. Candidate print/os.write(1)
    # goes to bounded stderr, without contaminating RPC JSON.
    _wire = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    _limit, _methods = request["max_message_bytes"], frozenset(request["methods"])
    sys.path[:0] = request["dependencies"]
    zoneinfo.reset_tzpath(request["timezone_roots"])
    source = Path(request["source"]) / "nerya"
    spec = importlib.util.spec_from_file_location("nerya", source / "__init__.py", submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["nerya"] = module
    spec.loader.exec_module(module)
    candidate = Path(request["base"]) / "candidate"
    entrypoint = candidate / request["entrypoint"]
    sys.path.insert(0, str(candidate))
    spec = importlib.util.spec_from_file_location("_isolated_candidate", entrypoint, submodule_search_locations=[str(candidate)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    callback = getattr(module, request["callback"])
    if not callable(callback):
        raise TypeError("candidate callback is not callable")
    _write({"type": "ready"})
    while True:
        try:
            request = _read()
        except EOFError:
            return
        if set(request) != {"type", "frame", "snapshot"} or request["type"] != "run":
            raise ValueError("unexpected host bar frame")
        _frame = request["frame"]
        try:
            result = callback(_context(request["snapshot"]))
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            _write({"type": "result", "frame": _frame, "value": _normal(result)})
        except BaseException as exc:
            _write({"type": "error", "frame": _frame, "message": f"{type(exc).__name__}: {exc}"[:2000]})
            return
        finally:
            _frame = None


if __name__ == "__main__":
    main()
