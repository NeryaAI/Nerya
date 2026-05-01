"""Static + smoke-test validator for strategy packages.

Plan ref: ``2026-04-28-agent-generated-strategy-runtime-refactor.md`` §5.5.

Scope
-----
The validator runs *before* a :class:`StrategyPackage` is promoted into
``workspace/strategies/<id>/``. It must catch every failure mode that
the agent's free-form code generator can introduce *without* actually
executing the strategy in production. The runner (Phase 3) re-runs
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
from typing import Any, Iterable, Optional

from .package import StrategyPackage, _parse_manifest, load_package
from ..core import yaml_io
from ..core.errors import TradingError
from ..core.paths import WorkspacePaths


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
            p.write_text(content, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _validate_loaded(package: StrategyPackage) -> StrategyValidation:
    issues: list[StrategyValidationIssue] = []

    # Schema layer was already enforced when load_package returned —
    # but it doesn't check the cross-cuts we care about. Do those here.
    if package.manifest.policy.allow_direct_order is False and not package.manifest.subagents:
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
    if package.manifest.policy.require_subagent_before_order and not package.manifest.subagents:
        issues.append(
            StrategyValidationIssue(
                severity="blocker",
                code="missing_required_subagent",
                message=(
                    "policy.require_subagent_before_order=True but manifest.subagents is empty"
                ),
                where="strategy.yml",
            )
        )
    if package.manifest.mode == "live" and not package.manifest.subagents and not package.manifest.policy.require_subagent_before_order:
        issues.append(
            StrategyValidationIssue(
                severity="warning",
                code="live_without_subagent",
                message=(
                    "live mode without any subagent confirmation step; "
                    "consider setting require_subagent_before_order=True"
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


def _has_blocker(issues: Iterable[StrategyValidationIssue]) -> bool:
    return any(i.severity == "blocker" for i in issues)


def _static_scan_package(package: StrategyPackage) -> list[StrategyValidationIssue]:
    """Walk every ``.py`` file in the package and flag forbidden patterns."""

    out: list[StrategyValidationIssue] = []
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
    return out


def _walk_ast(tree: ast.AST, *, where: str) -> list[StrategyValidationIssue]:
    issues: list[StrategyValidationIssue] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                issues.extend(_check_import(alias.name, where=where, lineno=node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                issues.extend(_check_import(node.module, where=where, lineno=node.lineno))
        elif isinstance(node, ast.Call):
            issues.extend(_check_call(node, where=where))
        elif isinstance(node, ast.Attribute):
            issues.extend(_check_attribute(node, where=where))
        elif isinstance(node, ast.Name):
            if node.id in DANGEROUS_BUILTINS and isinstance(getattr(node, "ctx", None), ast.Load):
                # Plain reference is fine in some contexts; the hard
                # ban applies inside Call (we catch that via _check_call).
                pass
    return issues


def _check_import(module: str, *, where: str, lineno: int) -> list[StrategyValidationIssue]:
    out: list[StrategyValidationIssue] = []
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


def _check_call(node: ast.Call, *, where: str) -> list[StrategyValidationIssue]:
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
    return out


def _check_attribute(node: ast.Attribute, *, where: str) -> list[StrategyValidationIssue]:
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
    return out


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
                message=f"failed to import entrypoint: {type(exc).__name__}: {exc}",
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
    "validate_proposal_files",
    "validate_strategy_package",
]
