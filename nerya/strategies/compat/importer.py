"""Import foreign-framework strategy source into a Nerya strategy package.

The importer is the bridge between "I have a Freqtrade/VNpy strategy
file" and Nerya's package runtime. It

1. detects the framework (:mod:`.detect`, AST-only, never imports);
2. extracts class-level constants that drive manifest defaults
   (Freqtrade ``timeframe`` / ``can_short`` / ``stoploss`` /
   ``minimal_roi`` / ``startup_candle_count``; VNpy ``parameters`` and
   their default values);
3. writes a regular strategy package under
   ``workspace/strategies/<strategy_id>/``: a typed ``strategy.yml``
   (with the ``extras.framework`` block the entrypoint reads), the
   uploaded sources verbatim, and a generated three-line ``main.py``
   that delegates to :func:`nerya.strategies.run_compat_tick`;
4. validates the resulting package with the standard validator and
   reports any blockers alongside the import summary.

The generated package is ordinary: the runner, scheduler, backtest
engine, dashboard, and evolution loop all work on it unchanged. All
orders still flow through the risk-gated trading kernel — importing a
strategy never bypasses the Risk / Approval gates.
"""

from __future__ import annotations

import ast
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...core.errors import TradingError
from ..package import load_package_from_dir
from ..validator import _validate_loaded
from .detect import (
    FREQTRADE,
    FrameworkInfo,
    UNKNOWN,
    VNPY,
    detect_frameworks,
)

_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_COMPAT_ENTRYPOINT = '''"""Auto-generated Nerya compat entrypoint — do not edit by hand.

Regenerate via ``nerya strategy import-external`` or the agent's
strategy_import_external tool.
"""

from pathlib import Path

from nerya.strategies import run_compat_tick


def run(ctx):
    return run_compat_tick(ctx, package_root=Path(__file__).resolve().parent)
'''


@dataclass
class ImportExternalResult:
    """Outcome of :func:`import_external_strategy`."""

    strategy_id: str
    package_dir: str
    framework: str
    class_name: str
    source_files: tuple[str, ...]
    markets: tuple[str, ...]
    timeframe: str
    warnings: tuple[str, ...] = ()
    validation_ok: bool = False
    validation_issues: tuple[dict[str, Any], ...] = ()

    def asdict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "package_dir": self.package_dir,
            "framework": self.framework,
            "class_name": self.class_name,
            "source_files": list(self.source_files),
            "markets": list(self.markets),
            "timeframe": self.timeframe,
            "warnings": list(self.warnings),
            "validation_ok": self.validation_ok,
            "validation_issues": list(self.validation_issues),
        }


def import_external_strategy(
    config: Any,
    sources: "str | Path | list[str | Path]",
    *,
    strategy_id: Optional[str] = None,
    framework: str = "auto",
    title: Optional[str] = None,
    description: str = "",
    markets: Optional[list[str]] = None,
    accounts: Optional[list[str]] = None,
    timeframe: Optional[str] = None,
    schedule: Optional[dict[str, Any]] = None,
    settings: Optional[dict[str, Any]] = None,
    mode: str = "paper",
    stake_amount: float = 0.0,
    overwrite: bool = False,
    class_name: Optional[str] = None,
) -> ImportExternalResult:
    """Create a Nerya strategy package from foreign framework source.

    ``sources`` is a ``.py`` file, a list of files, or a directory
    (imported recursively). ``framework`` may be ``"auto"``,
    ``"freqtrade"`` or ``"vnpy"``; auto detection is AST-based.
    ``mode`` is clamped to ``paper`` / ``shadow`` — external imports
    never land directly on ``live``; request ``live`` and the manifest
    records ``promotion_request: live`` instead so the package still
    goes through the normal promotion ladder. ``class_name`` supplies
    the strategy class explicitly when detection cannot locate the
    subclass (aliased / indirect bases).
    """

    root_dir = config.paths.strategies
    root_dir.mkdir(parents=True, exist_ok=True)

    files = _collect_source_files(sources)
    if not files:
        raise TradingError("no .py source files found to import")

    infos: list[tuple[Path, str, FrameworkInfo]] = []
    for path, text in files:
        from .detect import detect_framework

        infos.append((path, text, detect_framework(text, source_file=path.name)))

    resolved = _resolve_framework(framework, infos)
    if resolved.framework == UNKNOWN:
        raise TradingError(
            "could not detect a supported quant framework (freqtrade / vnpy) "
            "in the provided sources; pass framework='freqtrade' or 'vnpy' "
            "explicitly if the strategy subclasses the base class indirectly"
        )

    main_path, main_source, info = resolved.main_file
    warnings: list[str] = list(resolved.warnings)

    # A package without a resolvable strategy class would fail on every
    # tick with compat_error — refuse to generate one and demand the
    # class be named explicitly.
    if not info.class_name:
        if not class_name:
            raise TradingError(
                "could not locate the strategy subclass in the provided sources "
                "(aliased or indirect base class); pass an explicit class "
                "argument naming the strategy class and re-import"
            )
        info.class_name = class_name
        warnings.append(f"strategy class resolved from explicit argument: {class_name}")

    # Lifecycle clamp: an imported package must never start on live.
    requested_mode = str(mode or "paper").strip().lower()
    promotion_request: Optional[str] = None
    if requested_mode == "live":
        final_mode = "paper"
        promotion_request = "live"
        warnings.append(
            "mode=live requested — imported as paper; live requires the normal "
            "promotion ladder (recorded as promotion_request: live in the manifest)"
        )
    elif requested_mode in {"paper", "shadow"}:
        final_mode = requested_mode
    else:
        final_mode = "paper"
        warnings.append(f"unknown mode {requested_mode!r} — imported as paper")

    derived_id = _slugify(info.class_name or main_path.stem)
    final_id = strategy_id or derived_id
    if not _STRATEGY_ID_RE.match(final_id):
        raise TradingError(
            f"strategy_id {final_id!r} must match {_STRATEGY_ID_RE.pattern}"
        )
    package_dir = root_dir / final_id
    if package_dir.exists():
        if not overwrite:
            raise TradingError(
                f"strategy {final_id!r} already exists at {package_dir}; pass "
                "overwrite=True to replace it"
            )
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)

    manifest_facts: dict[str, Any] = (
        _freqtrade_facts(main_source, info)
        if resolved.framework == FREQTRADE
        else _vnpy_facts(main_source, info)
    )

    final_timeframe = timeframe or manifest_facts.get("timeframe") or _default_timeframe(
        resolved.framework
    )
    final_markets = _resolve_markets(markets, resolved.framework, info, warnings)
    final_accounts = list(accounts or ["paper_main"])

    every_seconds = max(_timeframe_seconds(final_timeframe), 60)
    final_schedule = schedule or {"type": "interval", "every_seconds": every_seconds}

    policy: dict[str, Any] = {
        "allow_direct_order": True,
        "max_run_seconds": 300,
        "max_sdk_calls_per_run": 200,
    }
    if stake_amount > 0:
        policy["default_order_usd"] = float(stake_amount)
    elif resolved.framework == FREQTRADE:
        policy["default_order_usd"] = 100.0

    extras_block: dict[str, Any] = {
        "name": resolved.framework,
        "source_file": main_path.name,
        "class": info.class_name,
        "settings": dict(settings or manifest_facts.get("settings") or {}),
    }
    if resolved.framework == FREQTRADE:
        extras_block["timeframe"] = final_timeframe
        extras_block["stake_amount"] = float(stake_amount or 0.0) or 100.0
        extras_block["can_short"] = bool(manifest_facts.get("can_short", False))
    else:
        extras_block["bar_timeframe"] = final_timeframe
        extras_block["init_bars"] = int(manifest_facts.get("init_bars", 500))

    strategy_yml: dict[str, Any] = {
        "version": 1,
        "strategy_id": final_id,
        "title": title or f"{info.class_name} ({resolved.framework})",
        "description": description
        or f"Imported {resolved.framework} strategy {info.class_name} from {main_path.name}",
        "mode": final_mode,
        "entrypoint": "main.py:run",
        "markets": final_markets,
        "accounts": final_accounts,
        "schedule": final_schedule,
        "policy": policy,
        "llm_policy": {"default_tier": "light", "max_calls_per_run": 2},
        "extras": {"framework": extras_block},
    }
    if promotion_request:
        # The operator asked for live at import time; the package still
        # starts as paper and this records the pending promotion.
        strategy_yml["promotion_request"] = promotion_request

    _write_yaml(package_dir / "strategy.yml", strategy_yml)
    (package_dir / "main.py").write_text(_COMPAT_ENTRYPOINT, encoding="utf-8")

    # Copy sources verbatim (flat: name collisions within the package
    # are unlikely for strategy bundles; keep the original filenames so
    # inter-module imports inside the bundle keep working).
    for path, _text, _info in infos:
        target = package_dir / path.name
        if target.resolve() != path.resolve():
            shutil.copy2(path, target)

    warnings.extend(_compat_warnings(resolved.framework, main_source))

    package = load_package_from_dir(package_dir)
    validation = _validate_loaded(package)
    return ImportExternalResult(
        strategy_id=final_id,
        package_dir=str(package_dir),
        framework=resolved.framework,
        class_name=info.class_name,
        source_files=tuple(sorted(p.name for p, _t, _i in infos)),
        markets=tuple(final_markets),
        timeframe=final_timeframe,
        warnings=tuple(warnings),
        validation_ok=bool(validation.ok),
        validation_issues=tuple(issue.asdict() for issue in validation.issues),
    )


# ---------------------------------------------------------------------------
# framework resolution
# ---------------------------------------------------------------------------


class _Resolved:
    def __init__(
        self,
        framework: str,
        main_file: "tuple[Path, str, FrameworkInfo]",
        warnings: list[str],
    ) -> None:
        self.framework = framework
        self.main_file = main_file
        self.warnings = warnings


def _resolve_framework(
    requested: str,
    infos: list[tuple[Path, str, FrameworkInfo]],
) -> _Resolved:
    requested = str(requested or "auto").strip().lower()
    if requested in {FREQTRADE, VNPY}:
        for path, text, info in infos:
            if info.framework == requested and info.class_name:
                return _Resolved(requested, (path, text, info), [])
        for path, text, info in infos:
            if info.framework == requested:
                return _Resolved(
                    requested,
                    (path, text, info),
                    [f"no {requested} strategy class found in {path.name}; using import-only detection"],
                )
        raise TradingError(f"framework={requested!r} but no matching source file was provided")

    bundle = {path.name: text for path, text, _info in infos}
    info = detect_frameworks(bundle)
    if info.detected and info.class_name:
        for path, text, cand in infos:
            if path.name == info.source_file and cand.class_name == info.class_name:
                return _Resolved(info.framework, (path, text, cand), [])
    if info.detected:
        for path, text, cand in infos:
            if cand.framework == info.framework:
                return _Resolved(
                    info.framework,
                    (path, text, cand),
                    ["framework detected from imports only; strategy class not located"],
                )
    # No signal at all — pick the largest file for the error message.
    fallback = max(infos, key=lambda item: len(item[1]))
    return _Resolved(UNKNOWN, fallback, [])


# ---------------------------------------------------------------------------
# class-constant extraction
# ---------------------------------------------------------------------------


def _freqtrade_facts(source: str, info: FrameworkInfo) -> dict[str, Any]:
    tree = _parse(source)
    cls = _find_class(tree, info.class_name)
    facts: dict[str, Any] = {"timeframe": None, "can_short": False}
    if cls is None:
        return facts
    for name in ("timeframe", "startup_candle_count", "can_short", "stoploss"):
        value = _class_constant(cls, name)
        if value is not None:
            facts[name] = value
    roi = _class_constant(cls, "minimal_roi")
    if isinstance(roi, dict):
        facts["minimal_roi"] = roi
    return facts


def _vnpy_facts(source: str, info: FrameworkInfo) -> dict[str, Any]:
    tree = _parse(source)
    cls = _find_class(tree, info.class_name)
    facts: dict[str, Any] = {"settings": {}}
    if cls is None:
        return facts
    param_names = _class_constant(cls, "parameters")
    settings: dict[str, Any] = {}
    if isinstance(param_names, (list, tuple, set)):
        for name in param_names:
            value = _class_constant(cls, str(name))
            if value is not None:
                settings[str(name)] = value
    facts["settings"] = settings
    return facts


def _parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _find_class(tree: ast.Module | None, class_name: str) -> ast.ClassDef | None:
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _class_constant(cls: ast.ClassDef, name: str) -> Any:
    for node in cls.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    return ast.literal_eval(node.value)  # type: ignore[attr-defined]
                except (ValueError, AttributeError):
                    return None
    return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _collect_source_files(
    sources: "str | Path | list[str | Path]",
) -> list[tuple[Path, str]]:
    items: list[Path] = []
    raw = sources if isinstance(sources, (list, tuple)) else [sources]
    for item in raw:
        path = Path(str(item)).expanduser()
        if path.is_dir():
            items.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
        elif path.is_file():
            items.append(path)
        else:
            raise TradingError(f"source not found: {path}")
    out: list[tuple[Path, str]] = []
    for path in items:
        try:
            out.append((path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            raise TradingError(f"cannot read source {path}: {exc}") from exc
    return out


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", str(name or "").strip()).strip("_").lower()
    if not slug:
        slug = "imported_strategy"
    if not re.match(r"^[a-z]", slug):
        slug = f"s_{slug}"
    return slug[:63]


def _default_timeframe(framework: str) -> str:
    return "5m" if framework == FREQTRADE else "1m"


def _timeframe_seconds(timeframe: str) -> int:
    tf = str(timeframe or "1m").strip()
    match = re.match(r"^(\d+)([mhd])$", tf)
    if not match:
        return 300
    value, unit = int(match.group(1)), match.group(2)
    return value * {"m": 60, "h": 3600, "d": 86400}[unit]


def _resolve_markets(
    markets: Optional[list[str]],
    framework: str,
    info: FrameworkInfo,
    warnings: list[str],
) -> list[str]:
    if markets:
        cleaned = [str(m).strip() for m in markets if str(m).strip()]
        if cleaned:
            return cleaned
    warnings.append(
        "no market specified; defaulted to PAPER:BTCUSDT — re-import or edit "
        "strategy.yml::markets to target the intended market"
    )
    return ["PAPER:BTCUSDT"]


def _compat_warnings(framework: str, source: str) -> list[str]:
    warnings: list[str] = []
    lowered = source.lower()
    if framework == FREQTRADE:
        if "self.dp" in source:
            warnings.append(
                "strategy references self.dp (DataProvider) — informative/timeframe "
                "lookups are unsupported on Nerya and will raise at runtime"
            )
        if "merge_informative_pair" in lowered:
            warnings.append(
                "strategy merges informative pairs; the Nerya compat runtime is "
                "single-market and will not provide the extra timeframe"
            )
    if "import talib" in lowered or "from talib" in lowered:
        try:
            import importlib.util

            if importlib.util.find_spec("talib") is None:
                warnings.append(
                    "strategy imports talib but TA-Lib is not installed in this "
                    "environment — install it or the strategy will fail at import"
                )
        except (ImportError, ValueError):
            warnings.append("strategy imports talib which may not be available")
    if framework == VNPY and "on_tick" in lowered:
        warnings.append(
            "strategy defines on_tick — Nerya feeds bars; tick-driven logic runs "
            "once per bar with the close price as last_price"
        )
    return warnings


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    from ...core import yaml_io

    yaml_io.dump(path, payload)


__all__ = ["ImportExternalResult", "import_external_strategy"]
