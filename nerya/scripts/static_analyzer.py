"""Static analyzer for user scripts.

Refuses scripts that import forbidden modules, touch sensitive paths,
shell out, reach the network directly, or invoke dynamic-code primitives.
A real deployment would also use seccomp/pledge/AppContainer — this
layer is the first line of defence.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FORBIDDEN_IMPORTS = {
    # shells / subprocess / dynamic code / serialization gadgets
    "subprocess", "os.system", "ctypes", "pickle", "marshal",
    "importlib", "runpy", "codeop", "multiprocessing",
    # networking stacks
    "socket", "ssl", "requests", "urllib", "urllib3",
    "http", "httpx", "aiohttp", "websocket", "websockets",
    "paramiko", "asyncssh", "ftplib", "smtplib",
    # messaging / notification tokens
    "telegram", "discord", "slack_sdk",
    # wallet / signer
    "web3", "solana", "eth_account", "hdwallet", "bitcoinlib",
    "mnemonic", "bip32", "bip39",
    # env / secrets
    "dotenv", "python_dotenv", "keyring",
    # Exchange SDKs: must never be called from user scripts; they talk
    # to private APIs and sign orders directly, bypassing the Nerya
    # Trading SDK (which we own end-to-end).
    "ccxt", "ccxt_async", "binance", "python_binance",
    "bybit", "okx", "hyperliquid", "kraken", "coinbase",
    "gate_api", "kucoin", "mexc_api",
    # filesystem escape
    "shutil",
}

FORBIDDEN_ATTRS = {
    # Env reading - scripts must not access provider keys
    "os.environ", "os.getenv", "os.putenv", "os.unsetenv",
    # Dynamic exec
    "builtins.eval", "builtins.exec", "builtins.compile",
    "builtins.__import__",
    # Filesystem shims
    "shutil.rmtree", "shutil.copy", "shutil.copyfile",
    "shutil.move",
    # Serialization gadgets
    "pickle.loads", "pickle.load",
    "marshal.loads", "marshal.load",
}

# Names whose *call* is forbidden even without an attribute chain
# (e.g. ``eval("...")`` or ``exec(src)`` at module level).
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__"}

FORBIDDEN_STRINGS = (
    # SSH / wallet
    "~/.ssh", ".ssh/", "id_rsa", "id_ed25519",
    "wallet.json", "keystore.json", "mnemonic", "seed_phrase",
    # env files
    ".env", "environ", "API_KEY", "SECRET_KEY",
    # common suspicious var names
    "api_key", "private_key", "bot_token",
    # browser data
    "Cookies", "Login Data", "Web Data",
    # Nerya sensitive files that scripts have no business touching
    "nerya.yml", "limits.yml", "accounts.yml", "exchanges.yml",
    "secrets.refs.yml", "secrets.enc", "keyring.ref",
    # signer policy / approvals
    "policy_signer", "signer_policy",
)


@dataclass
class Finding:
    severity: str      # "error" | "warn"
    code: str
    message: str
    line: int = 0


@dataclass
class AnalysisResult:
    findings: list[Finding]

    @property
    def is_safe(self) -> bool:
        return not has_errors(self.findings)

    @property
    def warnings(self) -> list[str]:
        return [f.message for f in self.findings]


def _check(tree: ast.AST) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if (mod in FORBIDDEN_IMPORTS
                        or mod.split(".")[0] in FORBIDDEN_IMPORTS):
                    findings.append(Finding(
                        "error", "forbidden_import",
                        f"forbidden import: {mod}", node.lineno))
        elif isinstance(node, ast.ImportFrom):
            mod_full = node.module or ""
            mod = mod_full.split(".")[0]
            if mod in FORBIDDEN_IMPORTS or mod_full in FORBIDDEN_IMPORTS:
                findings.append(Finding(
                    "error", "forbidden_import",
                    f"forbidden import: {mod_full}", node.lineno))
        elif isinstance(node, ast.Attribute):
            attr_full = _full_attr(node)
            if attr_full in FORBIDDEN_ATTRS:
                findings.append(Finding(
                    "error", "forbidden_attr",
                    f"forbidden attribute access: {attr_full}", node.lineno))
        elif isinstance(node, ast.Call):
            # Direct calls to eval()/exec()/compile()/__import__()
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_NAMES:
                findings.append(Finding(
                    "error", "forbidden_call",
                    f"forbidden call: {fn.id}()", node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            lc = node.value.lower()
            for needle in FORBIDDEN_STRINGS:
                if needle.lower() in lc:
                    findings.append(Finding(
                        "error", "suspicious_string",
                        f"suspicious string literal: {needle!r}",
                        node.lineno))
                    break
    return findings


def analyze_source(src: str) -> AnalysisResult:
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return AnalysisResult([Finding("error", "syntax",
                                        f"syntax error: {exc}", exc.lineno or 0)])
    return AnalysisResult(_check(tree))


def analyze(path: Path) -> list[Finding]:
    src = Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [Finding("error", "syntax", f"syntax error: {exc}", exc.lineno or 0)]
    return _check(tree)


def _full_attr(node: ast.Attribute) -> str:
    parts: list[str] = []
    cur: Any = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)
