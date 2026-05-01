"""Static checks for agent-authored signal engines.

Plan §5 Task 3 step 5 forbids signal engines from importing dangerous
modules (``os``, ``subprocess``, ``socket``, ``ccxt``, exchange clients
etc.) and from sub-classing/calling things that perform IO.

The check is intentionally simple: parse the source to AST, enumerate
top-level import targets, reject any in the deny list.  This catches
the obvious cases without trying to be a full sandbox.

Inspired by ``../Vibe-Trading/agent/src/shadow_account/codegen.py:78``
(static checks on generated signal engines) but reimplemented inside
Nerya — no Vibe-Trading import.
"""
from __future__ import annotations

import ast
from pathlib import Path

from ...core.errors import NeryaError


class SignalEngineStaticCheckError(NeryaError):
    """Raised when a candidate signal engine fails static checks."""


_BANNED_TOP_MODULES: frozenset[str] = frozenset({
    "os",
    "subprocess",
    "socket",
    "ssl",
    "ftplib",
    "urllib",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "websocket",
    "websockets",
    "paramiko",
    "pyngrok",
    "ccxt",
    "ccxtpro",
    "binance",
    "ftx",
    "okx",
    "bybit",
    "kraken",
    "polymarket",
    "web3",
    "eth_account",
    "ethers",
    "pickle",
    "shelve",
    "marshal",
    "ctypes",
    "multiprocessing",
    "pty",
    "shutil",
    "tempfile",
    "pathlib",
    "io",
    "sys",
})


_REQUIRED_CLASS_NAME = "SignalEngine"
_REQUIRED_METHOD_NAME = "generate"


def static_check_source(source: str, *, label: str = "<signal_engine>") -> None:
    """Validate signal engine source code.

    Parameters
    ----------
    source:
        Module source code as a string.
    label:
        Human-readable identifier used in error messages, typically the
        module path.
    """

    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as exc:
        raise SignalEngineStaticCheckError(
            f"signal_engine_syntax_error:{label}:{exc.msg}") from exc

    banned: list[str] = []
    has_class = False
    has_method = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BANNED_TOP_MODULES:
                    banned.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _BANNED_TOP_MODULES:
                banned.append(node.module or "")
        elif isinstance(node, ast.ClassDef) and node.name == _REQUIRED_CLASS_NAME:
            has_class = True
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == _REQUIRED_METHOD_NAME:
                        has_method = True
                        break

    if banned:
        raise SignalEngineStaticCheckError(
            f"signal_engine_banned_imports:{label}:{','.join(sorted(set(banned)))}")
    if not has_class:
        raise SignalEngineStaticCheckError(
            f"signal_engine_missing_class:{label}:expected={_REQUIRED_CLASS_NAME}")
    if not has_method:
        raise SignalEngineStaticCheckError(
            f"signal_engine_missing_method:{label}:"
            f"expected={_REQUIRED_CLASS_NAME}.{_REQUIRED_METHOD_NAME}")


def static_check_module_path(path: str | Path) -> None:
    p = Path(path)
    if not p.is_file():
        raise SignalEngineStaticCheckError(
            f"signal_engine_missing_file:{p}")
    static_check_source(p.read_text(encoding="utf-8"), label=str(p))


__all__ = [
    "SignalEngineStaticCheckError",
    "static_check_module_path",
    "static_check_source",
]
