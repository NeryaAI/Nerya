"""Static + smoke-test validator for strategy packages.

Scope
-----
The validator runs *before* a :class:`StrategyPackage` is promoted into
``workspace/strategies/<id>/``. It must catch every failure mode that
the agent's free-form code generator can introduce *without* actually
executing the strategy in production. The runner re-runs
its own smoke checks at tick-time, but those are belt-and-braces;
this is the gate that decides whether the package is allowed to
exist on disk at all.

The validator runs in two layers:

1. **Schema layer** — load the manifest through
   :func:`nerya.strategies.package._parse_manifest` so every required
   field is present, modes are legal, and policy/llm-policy blocks
   parse. Catches "agent forgot ``schedule:``" failures.

2. **Static-policy layer** — AST-walk ``main.py`` (and any helper
   files inside the package root) to flag forbidden imports,
   forbidden builtins, secret-bearing string literals, and
   side-effect imports at module load time. Hard-blocks any package
   that:

   * imports ``ccxt``, ``web3``, ``solana``, ``socket``,
     ``subprocess``, ``http.client``, ``urllib.request``,
     ``aiohttp``, ``requests`` (anything that bypasses the facade);
   * imports any ``nerya.*`` submodule that isn't on the
     :data:`ALLOWED_NERYA_IMPORTS` list (so generated code can't
     instantiate ``LLMGateway`` or ``ConnectorRegistry`` itself);
   * uses ``eval``, ``exec``, ``compile``, ``__import__``,
     ``getattr(builtins, ...)``, ``open(`` outside the strategy root;
   * accesses environment variables via ``os.environ`` /
     ``os.getenv`` (secrets must come from the workspace vault);
   * shells out via ``os.system`` / ``subprocess.*``.

3. **Import smoke test** — load the entrypoint through a fake
   :class:`StrategyContext` (a no-op stub that records calls but
   refuses to do real work). The fake ``ctx`` raises a controlled
   exception when the strategy tries something dangerous (for
   example: it returns empty candles, no news, deny LLM/subagent,
   refuse trade submission with a clear message). The strategy
   should still be importable + callable; if it crashes during
   import we surface that as a hard error.

Result
------
``validate_strategy_package`` returns a :class:`StrategyValidation`
that distinguishes hard *blockers* (must be fixed before promotion)
from soft *warnings* (recorded but allowed). The CLI / API treats
``ok=False`` as a refusal to promote; warnings show up in the
proposal's diff/test_plan files for the operator to review.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..core import yaml_io
from ..core.errors import TradingError
from ..core.paths import WorkspacePaths
from .agent_task_mode import agent_task_requested
from .package import StrategyPackage, _parse_manifest, load_package


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allow / deny lists
# ---------------------------------------------------------------------------


FORBIDDEN_TOP_LEVEL_MODULES: frozenset[str] = frozenset(
    {
        # Direct exchange / wallet / chain access
        "ccxt", "ccxt_async", "ccxt_pro", "ccxtpro",
        "web3", "eth_account", "solana", "solders", "anchorpy",
        # Raw network
        "socket", "ssl", "asyncio", "aiohttp", "httpx", "requests",
        "urllib", "http", "smtplib",
        # Process / shell / filesystem traversal
        "subprocess", "os.system", "shutil",
        # Crypto primitives (strategies don't need these directly; if
        # they think they do, they should ask the operator)
        "cryptography", "Crypto", "nacl",
        # Live LLM provider SDKs (must go through ctx.llm)
        "openai", "anthropic", "google", "mistralai", "cohere",
        # ORM / DB drivers
        "sqlalchemy", "psycopg2", "psycopg", "pymongo",
    }
)
"""Top-level module names that strategies must not import.

Module-prefix matching is used for ``http`` / ``urllib`` / ``os.system``
style entries; an import like ``from urllib.request import urlopen`` is
rejected because the *root* module ``urllib`` is forbidden.
"""

ALLOWED_NERYA_IMPORTS: frozenset[str] = frozenset(
    {
        # The runtime types strategies legitimately need
        "nerya.strategies",
        "nerya.strategies.context",
        "nerya.strategies.result",
    }
)
"""``nerya.*`` imports the strategy is *allowed* to make.

Anything else under ``nerya.*`` is hard-blocked; strategies must go
through the :class:`StrategyContext` facade to reach the trading
kernel, the LLM gateway, or the connector registry.
"""

DANGEROUS_BUILTINS: frozenset[str] = frozenset(
    {
        "eval", "exec", "compile", "__import__",
        "globals", "locals", "vars",
        "input", "open",  # ``open`` is allowed via ctx.state for KV
                          # writes; outside that, strategies have no
                          # business touching the filesystem directly.
    }
)

DANGEROUS_OS_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "system", "popen", "spawnv", "spawnvp", "spawnve", "spawnvpe",
        "execv", "execve", "execvp", "execvpe", "execl", "execle",
        "fork", "kill", "putenv", "unsetenv",
        "environ", "getenv",  # secret leakage
    }
)

BACKTEST_UNSUPPORTED_CONTEXT_ATTRIBUTES: dict[str, str] = {
    "market_data.get_candles": (
        "ctx.market_data.get_candles uses the native market_data tool shape, "
        "not the StrategyContext facade. Use ctx.market.candles(market, "
        "timeframe=..., limit=...) so the same code runs in live and backtest."
    ),
    "portfolio.get_account": (
        "ctx.portfolio.get_account is not part of the strategy facade. Use "
        "ctx.portfolio.equity_usd / cash_usd for account sizing and "
        "ctx.config.accounts[0] for the configured account id."
    ),
    "portfolio.get_positions": (
        "ctx.portfolio.get_positions is not part of the strategy facade. Use "
        "ctx.portfolio.positions(market), which returns this strategy's own "
        "position share and is mirrored by the backtest engine."
    ),
    "portfolio.recent_trades": (
        "ctx.portfolio.recent_trades is not part of StrategyContext. Use "
        "ctx.portfolio.positions(market), ctx.portfolio.equity_usd / cash_usd, "
        "or strategy_history outside the strategy runtime."
    ),
    "market.round_qty": (
        "ctx.market.round_qty is not part of StrategyContext. Size orders with "
        "ctx.policy/default_order_usd and submit through ctx.trading so the "
        "runtime risk gate handles exchange-specific rounding."
    ),
    "trading.place_market_order": (
        "ctx.trading.place_market_order is not part of StrategyContext. Use "
        "ctx.trading.submit_intent(...) or ctx.trading.open_position(...) so "
        "orders remain risk-gated and backtest-compatible."
    ),
    "account_id": (
        "ctx.account_id is not part of StrategyContext. Use "
        "ctx.config.accounts[0] when the strategy needs its configured account."
    ),
    "agent_task": (
        "ctx.agent_task is not part of StrategyContext. Import "
        "StrategyAgentTask from nerya.strategies and return "
        "StrategyAgentTask.dispatch(...), StrategyAgentTask.skip(...), or "
        "StrategyAgentTask.error(...) directly so validation and backtest use "
        "the same public SDK surface."
    ),
    "market_available": (
        "ctx.market_available is not part of StrategyContext. Use "
        "ctx.market.candles(ctx.config.markets[0], timeframe=..., limit=...) "
        "or ctx.market.features(ctx.config.markets[0], timeframe=..., "
        "lookback=...) and skip/hold when data is empty."
    ),
    "feature": (
        "ctx.feature is not part of StrategyContext. Use "
        "ctx.market.features(ctx.config.markets[0], timeframe=..., "
        "lookback=...) and read indicator values from the returned mapping."
    ),
}
"""Generated-code surfaces that validate but fail during backtest.

These are not security hazards; they are contract hazards. The agent often
mixes the native ``market_data`` tool schema or older SDK snippets into
``main.py``. Static validation should reject that before the operator sees a
"validated" proposal whose first real backtest crashes.
"""

_CANDLE_ROW_FIELDS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume"}
)
_RESULT_BUILDER_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"hold", "skip", "ok", "error"}
)
_RESULT_BUILDER_POSITIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "hold": ("reason", "metadata"),
    "skip": ("reason", "metadata"),
    "ok": ("reason", "metadata"),
    "error": ("message", "kind", "metadata"),
}

PLACEHOLDER_MARKET_PARTS: frozenset[str] = frozenset(
    {
        "unknown",
        "placeholder",
        "tbd",
        "todo",
        "changeme",
        "change-me",
        "token_contract",
        "token-address",
        "token_address",
    }
)
"""Manifest market fragments that mean the agent emitted a placeholder.

Runtime scanners can use a provider universe route such as
``BYREAL_ONCHAIN:solana``. They must not validate a fake token market like
``BYREAL_ONCHAIN:solana:UNKNOWN`` because that makes later backtest and
operator-review results look more certain than they are.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class StrategyValidationIssue:
    """One violation discovered by the validator.

    ``severity`` is "blocker" or "warning". The CLI/API rejects any
    promotion with at least one blocker. Warnings are surfaced in
    the proposal's ``test_plan.md`` so operators can fix them on
    their own schedule.
    """

    severity: str
    code: str
    message: str
    where: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyValidation:
    """Aggregate validation outcome.

    ``ok`` is true iff there are zero blockers. ``warnings`` and
    ``blockers`` are split for ergonomics; the full unsplit list is
    available via :attr:`issues`.
    """

    strategy_id: str
    package_hash: str
    ok: bool
    issues: list[StrategyValidationIssue] = field(default_factory=list)

    @property
    def blockers(self) -> list[StrategyValidationIssue]:
        return [i for i in self.issues if i.severity == "blocker"]

    @property
    def warnings(self) -> list[StrategyValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def asdict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "package_hash": self.package_hash,
            "ok": self.ok,
            "blockers": [i.asdict() for i in self.blockers],
            "warnings": [i.asdict() for i in self.warnings],
        }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def validate_strategy_package(
    paths: WorkspacePaths,
    strategy_id: str,
) -> StrategyValidation:
    """Validate an *already-promoted* strategy package.

    The runner uses this on demand (the operator hits "validate" on
    the dashboard); the generator uses :func:`validate_proposal_files`
    to validate file blobs before they ever land on disk.
    """

    package = load_package(paths, strategy_id)
    return _validate_loaded(package)


def validate_proposal_files(
    *,
    strategy_id: str,
    files: dict[str, str],
) -> StrategyValidation:
    """Validate the file blobs the generator produced for a proposal.

    ``files`` maps relative posix paths (``strategy.yml``, ``main.py``,
    ``subagents/<name>.agent.md``, ``tests/test_contract.py``) to their
    text content. We mirror the package layout into a temporary
    directory so the loader + AST walk can run without changing.

    Why mirror to disk
    ------------------
    The schema loader expects ``strategy.yml`` on disk and the AST
    walk follows ``import`` statements; an in-memory shim would
    complicate both. Tempdir ftw.
    """

    import tempfile

    with tempfile.TemporaryDirectory(prefix="nerya-strategy-validate-") as td:
        root = Path(td) / strategy_id
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            normalized = str(content).replace("\r\n", "\n").replace("\r", "\n")
            p.write_text(normalized, encoding="utf-8", newline="\n")
        # Build a synthetic StrategyPackage so the rest of the
        # pipeline doesn't need to special-case proposal validation.
        manifest_path = root / "strategy.yml"
        if not manifest_path.exists():
            return StrategyValidation(
                strategy_id=strategy_id,
                package_hash="",
                ok=False,
                issues=[
                    StrategyValidationIssue(
                        severity="blocker",
                        code="manifest_missing",
                        message="proposal must include strategy.yml at the package root",
                    )
                ],
            )
        try:
            raw = yaml_io.load(manifest_path)
            if not isinstance(raw, dict):
                raise TradingError(f"{manifest_path}: must be a YAML mapping")
            manifest = _parse_manifest(raw, source=manifest_path)
        except TradingError as exc:
            return StrategyValidation(
                strategy_id=strategy_id,
                package_hash="",
                ok=False,
                issues=[
                    StrategyValidationIssue(
                        severity="blocker",
                        code="manifest_schema",
                        message=f"manifest schema invalid: {exc}",
                        where="strategy.yml",
                    )
                ],
            )

        package = StrategyPackage(
            manifest=manifest,
            root=root,
            files=tuple(sorted(files.keys())),
            content_hash="",  # not yet promoted
        )
        result = _validate_loaded(package)
        result.strategy_id = strategy_id
        return result


def static_scan_blockers(
    package: StrategyPackage,
) -> list[StrategyValidationIssue]:
    """Return only the *blocker* issues from the static AST scan.

    Load-time enforcement surface for :class:`~nerya.strategies.runner.
    StrategyRunner`: the runner refuses to execute a live-mode package
    whose code trips a blocker (forbidden import, dangerous builtin,
    env access, …) even though the full promotion-time validation was
    supposed to have caught it. Cheap — pure AST walk, no imports, no
    fake-context smoke test.
    """

    return [
        issue
        for issue in _static_scan_package(package)
        if issue.severity == "blocker"
    ]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _validate_loaded(package: StrategyPackage) -> StrategyValidation:
    issues: list[StrategyValidationIssue] = []

    # Schema layer was already enforced when load_package returned —
    # but it doesn't check the cross-cuts we care about. Do those here.
    issues.extend(_manifest_placeholder_issues(package))
    if (
        package.manifest.policy.allow_direct_order is False
        and not package.manifest.subagents
        and not agent_task_requested(package.manifest)
    ):
        issues.append(
            StrategyValidationIssue(
                severity="warning",
                code="orphan_no_trade_path",
                message=(
                    "policy.allow_direct_order=False and no subagents are listed; "
                    "the strategy can only ever return HOLD"
                ),
                where="strategy.yml",
            )
        )
    main_path = package.root / package.manifest.entrypoint_module
    if not main_path.exists():
        issues.append(
            StrategyValidationIssue(
                severity="blocker",
                code="entrypoint_missing",
                message=f"entrypoint {package.manifest.entrypoint_module!r} not found",
                where=package.manifest.entrypoint_module,
            )
        )
        return StrategyValidation(
            strategy_id=package.strategy_id,
            package_hash=package.content_hash,
            ok=False,
            issues=issues,
        )

    issues.extend(_static_scan_package(package))
    if not _has_blocker(issues):
        issues.extend(_smoke_test_import(package))

    ok = not _has_blocker(issues)
    return StrategyValidation(
        strategy_id=package.strategy_id,
        package_hash=package.content_hash,
        ok=ok,
        issues=issues,
    )


def _manifest_placeholder_issues(
    package: StrategyPackage,
) -> list[StrategyValidationIssue]:
    issues: list[StrategyValidationIssue] = []
    for market in package.manifest.markets:
        if _looks_placeholder_market(market):
            issues.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="placeholder_market",
                    message=(
                        f"strategy market {market!r} is a placeholder; use a "
                        "concrete provider market or a provider universe route "
                        "for runtime scanners"
                    ),
                    where="strategy.yml::markets",
                )
            )
    return issues


def _looks_placeholder_market(market: str) -> bool:
    text = str(market or "").strip()
    lowered = text.lower()
    if not lowered:
        return True
    if "<" in text or ">" in text or "..." in text:
        return True
    parts = [
        part.strip()
        for part in lowered.replace("/", ":").replace("\\", ":").split(":")
        if part.strip()
    ]
    return any(part in PLACEHOLDER_MARKET_PARTS for part in parts)


def _has_blocker(issues: Iterable[StrategyValidationIssue]) -> bool:
    return any(i.severity == "blocker" for i in issues)


def _static_scan_package(package: StrategyPackage) -> list[StrategyValidationIssue]:
    """Walk every ``.py`` file in the package and flag forbidden patterns."""

    out: list[StrategyValidationIssue] = []
    is_agent_task = agent_task_requested(package.manifest)
    for py_path in sorted(package.root.rglob("*.py")):
        rel = py_path.relative_to(package.root).as_posix()
        # Skip per-run state, etc.
        first = rel.split("/", 1)[0]
        if first in {"runs", "state", "versions", "reviews"}:
            continue
        try:
            source = py_path.read_text(encoding="utf-8")
        except OSError as exc:
            out.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="read_failed",
                    message=f"cannot read {rel}: {exc}",
                    where=rel,
                )
            )
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            out.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="syntax_error",
                    message=f"syntax error in {rel}: {exc.msg} (line {exc.lineno})",
                    where=rel,
                )
            )
            continue
        out.extend(_walk_ast(tree, where=rel))
        if is_agent_task:
            out.extend(_agent_task_contract_issues(tree, where=rel))
    return out


def _walk_ast(tree: ast.AST, *, where: str) -> list[StrategyValidationIssue]:
    issues: list[StrategyValidationIssue] = []
    candle_row_names = _collect_candle_row_names(tree)
    position_row_names = _collect_position_row_names(tree)
    position_collection_names = _collect_position_collection_names(tree)
    market_aliases = _collect_strategy_market_aliases(tree)
    result_aliases = _collect_result_builder_aliases(tree)
    called_attribute_ids = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                issues.extend(_check_import(alias.name, where=where, lineno=node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                issues.extend(_check_import(node.module, where=where, lineno=node.lineno))
        elif isinstance(node, ast.Call):
            issues.extend(
                _check_call(
                    node,
                    where=where,
                    market_aliases=market_aliases,
                    result_aliases=result_aliases,
                )
            )
        elif isinstance(node, ast.Attribute):
            issues.extend(
                _check_attribute(
                    node,
                    where=where,
                    candle_row_names=candle_row_names,
                    position_row_names=position_row_names,
                    position_collection_names=position_collection_names,
                    called_attribute_ids=called_attribute_ids,
                    market_aliases=market_aliases,
                    result_aliases=result_aliases,
                )
            )
        elif isinstance(node, ast.Name):
            if node.id in DANGEROUS_BUILTINS and isinstance(getattr(node, "ctx", None), ast.Load):
                # Plain reference is fine in some contexts; the hard
                # ban applies inside Call (we catch that via _check_call).
                pass
    return issues


def _agent_task_contract_issues(
    tree: ast.AST,
    *,
    where: str,
) -> list[StrategyValidationIssue]:
    """Validate StrategyAgentTask status semantics in generated code."""

    issues: list[StrategyValidationIssue] = []
    has_dispatch = False
    allowed_dispatch_keywords = {
        "prompt",
        "session_key",
        "metadata",
        "artifacts",
        "attached_skills",
        "reason",
    }
    allowed_constructor_keywords = {
        "status",
        "prompt",
        "session_key",
        "metadata",
        "artifacts",
        "attached_skills",
        "reason",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _flatten_attr(node.func)
        if name == "StrategyAgentTask.dispatch":
            has_dispatch = True
        elif name in {"StrategyAgentTask.skip", "StrategyAgentTask.error"}:
            if len(node.args) <= 1:
                continue
            issues.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="unsupported_agent_task_factory_argument",
                    message=(
                        f"{name} accepts only the reason as a positional "
                        "argument. Pass metadata as metadata=... instead of "
                        "positional arguments so validation and backtest use "
                        "the same public SDK contract."
                    ),
                    where=where,
                )
            )
            continue
        elif name == "StrategyAgentTask":
            arg_names = [
                keyword.arg if keyword.arg is not None else "**kwargs"
                for keyword in node.keywords
                if keyword.arg not in allowed_constructor_keywords
            ]
            if node.args:
                arg_names.insert(0, "positional arguments")
            if not arg_names:
                arg_names = ["direct constructor"]
            issues.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="unsupported_agent_task_constructor",
                    message=(
                        "StrategyAgentTask direct construction is not a public "
                        "strategy SDK surface for generated packages. Use "
                        "StrategyAgentTask.dispatch(prompt=..., metadata=...), "
                        "StrategyAgentTask.skip(...), or StrategyAgentTask.error(...). "
                        "unsupported keyword/constructor field(s): "
                        + ", ".join(repr(name) for name in arg_names)
                        + "."
                    ),
                    where=where,
                )
            )
            continue
        else:
            continue
        for keyword in node.keywords:
            if keyword.arg in allowed_dispatch_keywords:
                continue
            arg_name = keyword.arg if keyword.arg is not None else "**kwargs"
            issues.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="unsupported_agent_task_dispatch_argument",
                    message=(
                        "StrategyAgentTask.dispatch does not accept "
                        f"{arg_name!r}. Use only prompt, session_key, metadata, "
                        "artifacts, attached_skills, and reason."
                    ),
                    where=where,
                )
            )
    if has_dispatch:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _flatten_attr(node.func)
            if name not in {"ctx.result.hold", "ctx.result.skip"}:
                continue
            issues.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="agent_task_skip_status",
                    message=(
                        "agent-task strategies that dispatch an Agent decision "
                        "must encode non-dispatch branches with "
                        "StrategyAgentTask.skip(reason, metadata=...), not "
                        f"{name}(). This preserves status='skip' and avoids "
                        "spending an Agent turn when preconditions fail."
                    ),
                    where=where,
                )
            )
    return issues


def _check_import(module: str, *, where: str, lineno: int) -> list[StrategyValidationIssue]:
    out: list[StrategyValidationIssue] = []
    if module == "strategy_sdk" or module.startswith("strategy_sdk."):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_sdk_import",
                message=(
                    "strategy_sdk is not available in Nerya strategy backtests. "
                    "Use the public SDK import: from nerya.strategies import "
                    "StrategyContext, StrategyResult, StrategyAgentTask "
                    f"(line {lineno})."
                ),
                where=where,
            )
        )
        return out
    head = module.split(".", 1)[0]
    if head in FORBIDDEN_TOP_LEVEL_MODULES or module in FORBIDDEN_TOP_LEVEL_MODULES:
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="forbidden_import",
                message=f"import of forbidden module {module!r} at line {lineno}",
                where=where,
            )
        )
        return out
    if module.startswith("nerya"):
        if not _is_allowed_nerya(module):
            out.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="forbidden_nerya_import",
                    message=(
                        f"strategy may not import {module!r}; only "
                        f"{sorted(ALLOWED_NERYA_IMPORTS)!r} are allowed"
                    ),
                    where=where,
                )
            )
    return out


def _is_allowed_nerya(module: str) -> bool:
    if module in ALLOWED_NERYA_IMPORTS:
        return True
    return any(module.startswith(prefix + ".") for prefix in ALLOWED_NERYA_IMPORTS)


def _check_call(
    node: ast.Call,
    *,
    where: str,
    market_aliases: set[str],
    result_aliases: set[str],
) -> list[StrategyValidationIssue]:
    out: list[StrategyValidationIssue] = []
    func = node.func
    name = _flatten_attr(func)
    if name in DANGEROUS_BUILTINS:
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="dangerous_builtin",
                message=f"call to dangerous builtin {name}() at line {node.lineno}",
                where=where,
            )
        )
    elif name and name.startswith("os."):
        attr = name.split(".", 1)[1]
        if attr in DANGEROUS_OS_ATTRIBUTES:
            out.append(
                StrategyValidationIssue(
                    severity="blocker",
                    code="dangerous_os_call",
                    message=f"call to {name}() at line {node.lineno}",
                    where=where,
                )
            )
    elif name in {"subprocess.run", "subprocess.Popen", "subprocess.call",
                   "subprocess.check_call", "subprocess.check_output"}:
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="subprocess",
                message=f"strategies may not shell out via {name}() (line {node.lineno})",
                where=where,
            )
        )
    elif _is_strategy_market_call(name, "candles", market_aliases):
        out.extend(_check_market_candles_call(node, where=where))
    elif _is_strategy_market_call(name, "features", market_aliases):
        out.extend(_check_market_features_call(node, where=where))
    elif _is_strategy_market_call(name, "ticker", market_aliases):
        out.extend(_check_market_ticker_call(node, where=where))
    result_method = _result_factory_method_name(name, result_aliases)
    if result_method is not None:
        out.extend(
            _check_result_factory_call(
                node,
                method=result_method,
                call_name=name,
                where=where,
            )
        )
    return out


def _is_strategy_market_call(
    flattened_name: str,
    method: str,
    market_aliases: set[str],
) -> bool:
    if flattened_name in {f"ctx.market.{method}", f"context.market.{method}"}:
        return True
    owner, _, attr = flattened_name.rpartition(".")
    return attr == method and owner in market_aliases


def _check_market_candles_call(
    node: ast.Call,
    *,
    where: str,
) -> list[StrategyValidationIssue]:
    first_arg_is_timeframe = _first_arg_is_timeframe_literal(node)
    missing_market = (
        not node.args
        or first_arg_is_timeframe
    ) and not any(
        keyword.arg == "market" for keyword in node.keywords
    )
    alias_keywords = [
        str(keyword.arg)
        for keyword in node.keywords
        if keyword.arg in {"interval", "count"}
    ]
    if not missing_market and not alias_keywords:
        return []

    details: list[str] = []
    if missing_market:
        details.append("missing required market argument")
    if first_arg_is_timeframe:
        details.append("positional timeframe was passed where market is required")
    if alias_keywords:
        details.append(f"unsupported keyword alias(es): {', '.join(alias_keywords)}")
    return [
        StrategyValidationIssue(
            severity="blocker",
            code="unsupported_strategy_context_surface",
            message=(
                "ctx.market.candles must use the StrategyContext facade as "
                "ctx.market.candles(market, timeframe=..., limit=...). "
                f"{'; '.join(details)} at line {node.lineno}."
            ),
            where=where,
        )
    ]


def _check_market_features_call(
    node: ast.Call,
    *,
    where: str,
) -> list[StrategyValidationIssue]:
    first_arg_is_timeframe = _first_arg_is_timeframe_literal(node)
    missing_market = (
        not node.args
        or first_arg_is_timeframe
    ) and not any(
        keyword.arg == "market" for keyword in node.keywords
    )
    alias_keywords = [
        str(keyword.arg)
        for keyword in node.keywords
        if keyword.arg in {"interval", "count", "limit", "symbol", "feature"}
    ]
    if not missing_market and not alias_keywords:
        return []

    details: list[str] = []
    if missing_market:
        details.append("missing required market argument")
    if first_arg_is_timeframe:
        details.append("positional timeframe was passed where market is required")
    if alias_keywords:
        details.append(f"unsupported keyword alias(es): {', '.join(alias_keywords)}")
    return [
        StrategyValidationIssue(
            severity="blocker",
            code="unsupported_strategy_context_surface",
            message=(
                "ctx.market.features must use the StrategyContext facade as "
                "ctx.market.features(market, timeframe=..., lookback=...). "
                f"{'; '.join(details)} at line {node.lineno}."
            ),
            where=where,
        )
    ]


def _check_market_ticker_call(
    node: ast.Call,
    *,
    where: str,
) -> list[StrategyValidationIssue]:
    missing_market = not node.args and not any(
        keyword.arg == "market" for keyword in node.keywords
    )
    unsupported_keywords = [
        str(keyword.arg)
        for keyword in node.keywords
        if keyword.arg not in {"market", "account"}
    ]
    if not missing_market and not unsupported_keywords:
        return []

    details: list[str] = []
    if missing_market:
        details.append("missing required market argument")
    if unsupported_keywords:
        details.append(f"unsupported keyword(s): {', '.join(unsupported_keywords)}")
    return [
        StrategyValidationIssue(
            severity="blocker",
            code="unsupported_strategy_context_surface",
            message=(
                "ctx.market.ticker must use the StrategyContext facade as "
                "ctx.market.ticker(market, account=...). "
                f"{'; '.join(details)} at line {node.lineno}."
            ),
            where=where,
        )
    ]


def _check_result_factory_call(
    node: ast.Call,
    *,
    method: str,
    call_name: str,
    where: str,
) -> list[StrategyValidationIssue]:
    if method not in _RESULT_BUILDER_ALLOWED_METHODS:
        return [
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    f"{call_name} is not part of the StrategyResult facade. "
                    "Use ctx.result.hold/skip/ok/error for terminal outcomes, "
                    "ctx.trading.open_position or ctx.trading.submit_intent for "
                    "entries, and ctx.trading.close_position for exits "
                    f"(line {node.lineno})."
                ),
                where=where,
            )
        ]
    if not node.args:
        return []
    field_names = _RESULT_BUILDER_POSITIONAL_FIELDS[method]
    allowed = ", ".join(f"{field}=..." for field in field_names)
    return [
        StrategyValidationIssue(
            severity="blocker",
            code="unsupported_strategy_context_surface",
            message=(
                f"{call_name} uses keyword-only SDK arguments; pass {allowed} "
                f"instead of positional arguments at line {node.lineno}."
            ),
            where=where,
        )
    ]


def _check_attribute(
    node: ast.Attribute,
    *,
    where: str,
    candle_row_names: set[str],
    position_row_names: set[str],
    position_collection_names: set[str],
    called_attribute_ids: set[int],
    market_aliases: set[str],
    result_aliases: set[str],
) -> list[StrategyValidationIssue]:
    out: list[StrategyValidationIssue] = []
    name = _flatten_attr(node)
    if name == "os.environ" or name == "os.getenv":
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="env_access",
                message=(
                    "strategies may not read environment variables; "
                    f"use ctx.state for run-local state ({name} at line {node.lineno})"
                ),
                where=where,
            )
        )
    issue = _strategy_context_contract_issue(name, lineno=node.lineno, where=where)
    if issue is not None:
        out.append(issue)
    result_method = _result_factory_method_name(name, result_aliases)
    if (
        result_method is not None
        and result_method not in _RESULT_BUILDER_ALLOWED_METHODS
        and id(node) not in called_attribute_ids
    ):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    f"{name} is not part of the StrategyResult facade. Use "
                    "ctx.result.hold/skip/ok/error for terminal outcomes, "
                    "ctx.trading.open_position or ctx.trading.submit_intent "
                    "for entries, and ctx.trading.close_position for exits "
                    f"(line {node.lineno})."
                ),
                where=where,
            )
        )
    if (
        (
            name in {"ctx.market.features", "context.market.features"}
            or name in {f"{alias}.features" for alias in market_aliases}
        )
        and id(node) not in called_attribute_ids
    ):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    "ctx.market.features is a StrategyContext method, not a "
                    "mapping. Call ctx.market.features(market, timeframe=..., "
                    f"lookback=...) before reading indicator values (line {node.lineno})."
                ),
                where=where,
            )
        )
    if (
        name in {"ctx.portfolio.positions", "context.portfolio.positions"}
        and id(node) not in called_attribute_ids
    ):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    "ctx.portfolio.positions is a StrategyContext method, not "
                    "an iterable property. Call ctx.portfolio.positions(market) "
                    f"before iterating positions (line {node.lineno})."
                ),
                where=where,
            )
        )
    if (
        isinstance(node.ctx, ast.Load)
        and node.attr in _CANDLE_ROW_FIELDS
        and isinstance(node.value, ast.Name)
        and node.value.id in candle_row_names
    ):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    "StrategyContext candle rows are dicts; use "
                    f"{node.value.id}[{node.attr!r}] instead of "
                    f"{node.value.id}.{node.attr} at line {node.lineno}."
                ),
                where=where,
            )
        )
    if (
        isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id in position_collection_names
    ):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    "ctx.portfolio.positions(market) returns a list; "
                    "iterate positions or select a row before reading fields "
                    f"instead of {node.value.id}.{node.attr} at line {node.lineno}."
                ),
                where=where,
            )
        )
    if (
        isinstance(node.ctx, ast.Load)
        and node.attr == "get"
        and isinstance(node.value, ast.Name)
        and node.value.id in position_row_names
    ):
        out.append(
            StrategyValidationIssue(
                severity="blocker",
                code="unsupported_strategy_context_surface",
                message=(
                    "StrategyPosition rows are dataclasses; use "
                    f"{node.value.id}.market, {node.value.id}.side, "
                    f"{node.value.id}.size, or {node.value.id}.entry_price "
                    f"instead of {node.value.id}.get(...) at line {node.lineno}."
                ),
                where=where,
            )
        )
    return out


def _collect_candle_row_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        generators = getattr(node, "generators", None)
        if generators is not None:
            for generator in generators:
                if _looks_like_candle_iter(generator.iter):
                    _collect_target_names(generator.target, names)
        elif isinstance(node, ast.For) and _looks_like_candle_iter(node.iter):
            _collect_target_names(node.target, names)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _collect_candle_row_assignment_names(target, node.value, names)
        elif isinstance(node, ast.AnnAssign):
            _collect_candle_row_assignment_names(node.target, node.value, names)
    return names


def _collect_position_row_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _looks_like_positions_iter(node.iter):
            _collect_target_names(node.target, names)
    return names


def _collect_position_collection_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _looks_like_positions_iter(node.value):
            for target in node.targets:
                _collect_target_names(target, names)
        elif isinstance(node, ast.AnnAssign) and _looks_like_positions_iter(node.value):
            _collect_target_names(node.target, names)
    return names


def _collect_candle_row_assignment_names(
    target: ast.AST,
    value: ast.AST | None,
    out: set[str],
) -> None:
    if value is None:
        return
    if _looks_like_candle_row_expr(value):
        _collect_target_names(target, out)
        return
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        for child_target, child_value in zip(target.elts, value.elts):
            _collect_candle_row_assignment_names(child_target, child_value, out)


def _looks_like_candle_row_expr(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and _looks_like_candle_iter(node.value)


def _looks_like_positions_iter(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _flatten_attr(node.func) in {
        "ctx.portfolio.positions",
        "context.portfolio.positions",
    }


def _collect_strategy_market_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "market"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in {"ctx", "context"}
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _collect_result_builder_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "result"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in {"ctx", "context"}
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _result_factory_method_name(
    flattened_name: str,
    result_aliases: set[str],
) -> str | None:
    if flattened_name in {
        "ctx.result",
        "context.result",
        "StrategyResult",
        *result_aliases,
    }:
        return None
    owner, _, method = flattened_name.rpartition(".")
    if not owner or not method:
        return None
    if owner in {"ctx.result", "context.result", "StrategyResult", *result_aliases}:
        return method
    return None


def _looks_like_candle_iter(node: ast.AST) -> bool:
    if not isinstance(node, ast.Name):
        return False
    name = node.id.lower()
    return (
        name == "candles"
        or name.startswith("candles_")
        or name.endswith("_candles")
        or "_candles_" in name
    )


def _first_arg_is_timeframe_literal(node: ast.Call) -> bool:
    return bool(node.args) and _looks_like_timeframe_literal(node.args[0])


def _looks_like_timeframe_literal(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    text = node.value.strip().lower()
    return len(text) > 1 and text[-1] in {"m", "h", "d", "w"} and text[:-1].isdigit()


def _collect_target_names(node: ast.AST, out: set[str]) -> None:
    if isinstance(node, ast.Name):
        out.add(node.id)
        return
    if isinstance(node, (ast.Tuple, ast.List)):
        for child in node.elts:
            _collect_target_names(child, out)


def _strategy_context_contract_issue(
    name: str,
    *,
    lineno: int,
    where: str,
) -> StrategyValidationIssue | None:
    if not name:
        return None
    parts = name.split(".")
    if not parts or parts[0] not in {"ctx", "context", "_ctx"}:
        return None
    suffix = ".".join(parts[1:])
    message = BACKTEST_UNSUPPORTED_CONTEXT_ATTRIBUTES.get(suffix)
    if not message:
        return None
    return StrategyValidationIssue(
        severity="blocker",
        code="unsupported_strategy_context_surface",
        message=f"{message} (line {lineno})",
        where=where,
    )


def _flatten_attr(node: ast.AST) -> str:
    """Render an attribute chain back to dotted form for matching."""

    parts: list[str] = []
    cur: Any = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Call):
        # Calls inside attribute chains (e.g. ``ctx.market.ticker(...)``)
        # — flattening past them isn't useful.
        return ""
    parts.reverse()
    return ".".join(parts)


def _entrypoint_import_error_message(exc: Exception) -> str:
    base = f"failed to import entrypoint: {type(exc).__name__}: {exc}"
    text = str(exc)
    if (
        isinstance(exc, ImportError)
        and (
            "StrategyAgentTask" in text
            or "StrategyContext" in text
            or "StrategyResult" in text
            or "strategy_sdk" in text
            or "nerya.strategy" in text
        )
    ):
        return (
            base
            + "; use the public SDK import: "
            + "from nerya.strategies import StrategyContext, "
            + "StrategyResult, StrategyAgentTask"
        )
    return base


def _smoke_test_import(package: StrategyPackage) -> list[StrategyValidationIssue]:
    """Try to import the entrypoint with a dummy ``StrategyContext``."""

    issues: list[StrategyValidationIssue] = []
    main_path = package.root / package.manifest.entrypoint_module
    suffix = uuid.uuid4().hex[:8]
    module_name = f"_nerya_strategy_validate.{package.manifest.strategy_id}.{suffix}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        main_path,
        submodule_search_locations=[str(package.root)],
    )
    if spec is None or spec.loader is None:
        issues.append(
            StrategyValidationIssue(
                severity="blocker",
                code="spec_failed",
                message=f"cannot build import spec for {package.manifest.entrypoint_module}",
                where=package.manifest.entrypoint_module,
            )
        )
        return issues
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    added_path = str(package.root)
    sys_path_inserted = added_path not in sys.path
    if sys_path_inserted:
        sys.path.insert(0, added_path)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        issues.append(
            StrategyValidationIssue(
                severity="blocker",
                code="import_failed",
                message=_entrypoint_import_error_message(exc),
                where=package.manifest.entrypoint_module,
            )
        )
        return issues
    finally:
        sys.modules.pop(module_name, None)
        if sys_path_inserted:
            try:
                sys.path.remove(added_path)
            except ValueError:
                pass

    entry = getattr(module, package.manifest.entrypoint_func, None)
    if entry is None:
        issues.append(
            StrategyValidationIssue(
                severity="blocker",
                code="entrypoint_attr_missing",
                message=(
                    f"entrypoint {package.manifest.entrypoint!r} resolves to a "
                    f"missing attribute"
                ),
                where=package.manifest.entrypoint_module,
            )
        )
        return issues
    if not callable(entry):
        issues.append(
            StrategyValidationIssue(
                severity="blocker",
                code="entrypoint_not_callable",
                message=(
                    f"entrypoint {package.manifest.entrypoint!r} is not callable"
                ),
                where=package.manifest.entrypoint_module,
            )
        )
        return issues
    return issues


__all__ = [
    "ALLOWED_NERYA_IMPORTS",
    "DANGEROUS_BUILTINS",
    "FORBIDDEN_TOP_LEVEL_MODULES",
    "StrategyValidation",
    "StrategyValidationIssue",
    "static_scan_blockers",
    "validate_proposal_files",
    "validate_strategy_package",
]
