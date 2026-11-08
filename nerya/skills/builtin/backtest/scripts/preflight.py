"""Static replay preflight. Never imports or executes user strategy code."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from typing import Any

from .....strategies.agent_task_mode import agent_task_requested
from .....strategies.workflow_service import _read_files
from .....strategies.verification import source_revision


class BacktestPreflightError(ValueError):
    reason = "backtest_preflight_failed"
    def __init__(self, receipt: dict[str, Any]):
        self.receipt = receipt
        super().__init__("; ".join(row["message"] for row in receipt["blockers"]))


_UNKNOWN = object()


def _literal(node: ast.AST | None):
    if node is None:
        return _UNKNOWN
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _literal(node.operand)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return -value if isinstance(node.op, ast.USub) else value
    return _UNKNOWN


def _call_arg(node: ast.Call, position: int, keyword: str):
    if len(node.args) > position:
        return node.args[position]
    return next((item.value for item in node.keywords if item.arg == keyword), None)


def _temporal_bias_issues(path: str, tree: ast.AST) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find high-confidence future-data patterns without executing strategy code.

    Nerya's replay context exposes only the historical prefix, so full-frame
    aggregations and iloc[-1] do not have Freqtrade's full-dataframe semantics
    here. Report operations that can move later observations backward in time
    as research warnings; static analysis cannot prove alignment.
    """
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()

    def add(bucket: list[dict[str, Any]], code: str, message: str, node: ast.AST):
        key = (code, int(getattr(node, "lineno", 0) or 0), message)
        if key in seen:
            return
        seen.add(key)
        bucket.append({"code": code, "message": message, "file": path, "line": getattr(node, "lineno", None)})

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""

        if method in {"shift", "diff", "pct_change"}:
            period_node = _call_arg(node, 0, "periods")
            if period_node is None:
                continue
            period = _literal(period_node)
            if isinstance(period, (int, float)) and not isinstance(period, bool):
                if period < 0:
                    add(warnings, "lookahead_negative_shift",
                        f"{name} uses periods={period}; negative shifts read future observations.", node)
            elif period is _UNKNOWN:
                add(warnings, "lookahead_dynamic_shift",
                    f"{name} has a dynamic periods value; preflight cannot prove it is non-negative.", node)

        if method in {"bfill", "backfill"}:
            add(warnings, "lookahead_backward_fill",
                f"{name} fills earlier rows from later observations; use a causal forward/rolling alternative.", node)

        if method == "fillna":
            method_node = _call_arg(node, 1, "method")
            fill_method = _literal(method_node)
            if isinstance(fill_method, str) and fill_method.lower() in {"bfill", "backfill"}:
                add(warnings, "lookahead_backward_fill",
                    f"{name}(method={fill_method!r}) moves future values backward in time.", node)

        if method == "rolling":
            center_node = next((item.value for item in node.keywords if item.arg == "center"), None)
            center = _literal(center_node)
            if center is True:
                add(warnings, "lookahead_centered_window",
                    f"{name} uses center=True; a centered window includes future observations.", node)
            elif center_node is not None and center is _UNKNOWN:
                add(warnings, "lookahead_dynamic_center",
                    f"{name} has a dynamic center value; preflight cannot prove the window is trailing-only.", node)

        if method == "merge_asof" or name.endswith("merge_asof"):
            direction_node = next((item.value for item in node.keywords if item.arg == "direction"), None)
            direction = _literal(direction_node)
            if isinstance(direction, str) and direction.lower() == "forward":
                add(warnings, "lookahead_forward_asof",
                    f"{name} uses direction='forward'; rows can receive values that were not known yet.", node)
            elif direction_node is not None and direction is _UNKNOWN:
                add(warnings, "lookahead_dynamic_asof",
                    f"{name} has a dynamic direction; preflight cannot prove point-in-time alignment.", node)

        if method == "interpolate":
            direction_node = next((item.value for item in node.keywords if item.arg == "limit_direction"), None)
            direction = _literal(direction_node)
            if isinstance(direction, str) and direction.lower() in {"backward", "both"}:
                add(warnings, "lookahead_backward_interpolation",
                    f"{name} uses limit_direction={direction!r}; later values may fill earlier timestamps.", node)

        if method == "resample":
            label_node = next((item.value for item in node.keywords if item.arg == "label"), None)
            label = _literal(label_node)
            if label_node is None or label != "right":
                add(warnings, "time_alignment_resample",
                    f"{name} is not explicitly label='right'; verify aggregated bars become available only after close.", node)

        if name in {"time.time", "time.time_ns", "datetime.now", "datetime.utcnow", "date.today"}:
            add(warnings, "nondeterministic_wall_clock",
                f"{name} reads wall-clock time during replay; prefer ctx.clock for reproducible historical decisions.", node)
        if name.startswith("random.") or ".random." in name or name in {
            "uuid.uuid4", "secrets.token_bytes", "secrets.token_hex", "secrets.token_urlsafe"
        }:
            add(warnings, "nondeterministic_randomness",
                f"{name} introduces replay randomness unless explicitly seeded and versioned.", node)

        if (name in {"open", "pd.read_csv", "pd.read_json", "pd.read_parquet", "pd.read_pickle",
                     "pandas.read_csv", "pandas.read_json", "pandas.read_parquet", "pandas.read_pickle"}
                or method in {"read_text", "read_bytes"}):
            add(warnings, "external_strategy_data_read",
                f"{name} reads data outside ctx.market; preflight cannot prove point-in-time availability or future-data isolation.", node)
    return [], warnings


def _value(node: ast.AST):
    """Resolve only constants and the explicit historical runmode. No eval."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute) and node.attr == "runmode" and isinstance(node.value, ast.Name) and node.value.id in {"ctx", "context"}:
        return "backtest"
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) >= 2 and isinstance(node.args[0], ast.Name)
            and node.args[0].id in {"ctx", "context"} and _value(node.args[1]) == "runmode"):
        return "backtest"
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = _value(node.left), _value(node.comparators[0])
        if left is not _UNKNOWN and right is not _UNKNOWN:
            if isinstance(node.ops[0], (ast.Eq, ast.Is)):
                return left == right
            if isinstance(node.ops[0], (ast.NotEq, ast.IsNot)):
                return left != right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _value(node.operand)
        return not value if value is not _UNKNOWN else _UNKNOWN
    if isinstance(node, ast.IfExp):
        test = _value(node.test)
        return _value(node.body if test else node.orelse) if test is not _UNKNOWN else _UNKNOWN
    if isinstance(node, (ast.Dict, ast.List, ast.Set)):
        return "nonempty" if (node.keys if isinstance(node, ast.Dict) else node.elts) else None
    return _UNKNOWN


def inspect_package(package: Any, cfg: Any) -> tuple[dict[str, Any], dict[str, str]]:
    files, omitted = _read_files(package.root)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    trees: dict[str, ast.AST] = {}

    def issue(code: str, message: str, path: str, line: int | None = None):
        blockers.append({"code": code, "message": message, "file": path, "line": line})

    if omitted:
        issue("incomplete_source_snapshot", "source snapshot omitted files: " + ", ".join(map(str, omitted)), "strategy.yml")
    from .....strategies.configuration_contract import configuration_issues
    for code,message in configuration_issues(package.manifest):
        if code == "insufficient_closed_history_lookback":
            warnings.append({"code": code, "message": message, "file": "strategy.yml"})
        else:
            issue(code,message,"strategy.yml")
    # Protocol identity comes from installed component metadata, not task prose.
    # This producer models candles and exchange-like fills, not pool accounting
    # or reserve accrual; another data feed cannot supply an execution model.
    from .....strategies.request_scope import _protocol, ScopeError
    for market in package.manifest.markets:
        venue, separator, _ = str(market).partition(':')
        if not separator:
            continue
        try:
            component = _protocol(venue)
        except ScopeError:
            continue
        required = set(component['market_types']) & {'lp', 'lending'}
        if required:
            warnings.append({'code': 'protocol_replay_model_required', 'file': 'strategy.yml',
                  'message': f'{market}: native OHLCV replay cannot model this protocol; candles alone '
                  'do not establish pool/reserve state, fees, liquidity, cashflows or execution. '
                  'Preserve the candidate and original window; select an explicitly supported '
                  'protocol replay producer with attested inputs. Do not substitute a candle '
                  'feed, mock rows or another protocol to clear this gap.'})
    for name, content in files.items():
        if not name.endswith(".py") or name.startswith("tests/"):
            continue
        try:
            trees[name] = ast.parse(content, filename=name)
            compile(trees[name], name, "exec")
            temporal_blockers, temporal_warnings = _temporal_bias_issues(name, trees[name])
            blockers.extend(temporal_blockers)
            warnings.extend(temporal_warnings)
        except SyntaxError as exc:
            issue("syntax_error", f"{name}: {exc.msg}", name, exc.lineno)
    entry = str(package.manifest.entrypoint)
    module, _, function = entry.partition(":")
    module_path = module if module.endswith(".py") else module.replace(".", "/") + ".py"
    tree = trees.get(module_path)
    if tree is None:
        issue("entrypoint_missing", f"entrypoint source not found: {module_path}", module_path)
    else:
        # Resolve only explicitly imported, trusted SDK callables. Do not
        # execute strategy source or import user modules during preflight.
        imported_indicators = {}
        runner_names = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "nerya.strategies.indicators":
                from .....strategies import indicators as sdk_indicators
                for imported in node.names:
                    if imported.name in sdk_indicators.__all__:
                        imported_indicators[imported.asname or imported.name] = getattr(sdk_indicators, imported.name)
            if isinstance(node, ast.ImportFrom) and node.module == "nerya.strategies.signal_runner":
                runner_names.update(imported.asname or imported.name for imported in node.names if imported.name == "run_signal_strategy")
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                    or node.func.id not in runner_names or len(node.args) < 2
                    or not isinstance(node.args[1], ast.Name) or node.args[1].id not in imported_indicators):
                continue
            params = package.manifest.extras.get("params") or {}
            if not isinstance(params, dict):
                continue  # Reported by configuration_issues above.
            values = params.get("indicator", {})
            override = next((kw.value for kw in node.keywords if kw.arg == "parameters"), None)
            parameters_known = True
            if override is not None:
                try:
                    values = ast.literal_eval(override)
                except (ValueError, TypeError):
                    parameters_known = False
                    warnings.append({"code":"dynamic_indicator_parameters", "file":module_path, "line":node.lineno,
                        "message":"Runtime validates dynamically constructed indicator parameters before candle reads or orders."})
            try:
                from .....strategies.signal_runner import indicator_parameters
                if parameters_known:
                    indicator_parameters(imported_indicators[node.args[1].id], values)
            except (ValueError, TypeError) as exc:
                issue("invalid_indicator_parameters", str(exc), module_path, node.lineno)
            if not any(kw.arg == "protection" for kw in node.keywords):
                from .historical_protection import normalize_protection
                try:
                    normalize_protection(params.get("protection"), side="long")
                except ValueError as exc:
                    issue("unsupported_protection", str(exc), "strategy.yml")
        definitions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        chosen = "build_agent_task" if agent_task_requested(package.manifest) and "build_agent_task" in definitions else function
        if chosen not in definitions:
            # Class/callable and framework entrypoints are checked by the
            # existing runtime loader, not rejected by an incomplete AST guess.
            warnings.append({"code": "dynamic_entrypoint", "message": f"runtime must resolve {entry}"})
        visited: set[str] = set()

        class Scan(ast.NodeVisitor):
            def visit_If(self, node):
                test = _value(node.test)
                for child in (node.body if test is True else node.orelse if test is False else [*node.body, *node.orelse]):
                    self.visit(child)

            def visit_Call(self, node):
                name = ast.unparse(node.func)
                if name.endswith(".trading.open_position"):
                    for keyword in node.keywords:
                        if keyword.arg == "protection":
                            value = _value(keyword.value)
                            try:
                                if sum(1 for _ in ast.walk(keyword.value)) <= 200:
                                    value = ast.literal_eval(keyword.value)
                            except (ValueError, TypeError):
                                pass
                            if isinstance(value, dict) or value is None or value is False:
                                from .historical_protection import normalize_protection
                                try:
                                    normalize_protection(value)
                                except ValueError as exc:
                                    issue("unsupported_protection", str(exc), module_path, node.lineno)
                            else:
                                warnings.append({"code": "dynamic_protection", "file": module_path, "line": node.lineno,
                                                 "message": "dynamic protection must resolve to a supported historical execution model"})
                if isinstance(node.func, ast.Name) and node.func.id in definitions and node.func.id not in visited:
                    visited.add(node.func.id)
                    self.visit(definitions[node.func.id])
                self.generic_visit(node)

        if chosen in definitions:
            visited.add(chosen)
            Scan().visit(definitions[chosen])

    # Scan import names without importing strategy code or third-party modules.
    # Only module-level imports are mandatory; optional/try imports are warnings
    # left to runtime. Package-local and relative imports use the snapshot.
    checked: set[str] = set()
    for name, tree in trees.items():
        for node in tree.body:
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imports = [alias.name.split(".")[0] for alias in node.names] if isinstance(node, ast.Import) else ([node.module.split(".")[0]] if node.module and not node.level else [])
            for module in imports:
                if module in checked or module in sys.stdlib_module_names:
                    continue
                checked.add(module)
                if module + ".py" in files or any(p.startswith(module + "/") for p in files):
                    continue
                try:
                    available = importlib.util.find_spec(module) is not None
                except (ImportError, ValueError, AttributeError):
                    available = False
                if not available:
                    issue("dependency_missing", f"missing dependency {module!r}; install through the approved environment or preserve the strategy while removing that dependency", name, node.lineno)
    receipt = {"ok": not blockers, "phase": "preflight", "blockers": blockers, "warnings": warnings,
               "source_revision": source_revision(files), "entrypoint": entry,
               "checks": ["source_snapshot", "python_syntax", "entrypoint", "module_dependencies",
                          "known_replay_surfaces", "temporal_bias_static_scan"],
               "config_sha256": hashlib.sha256(json.dumps(cfg.asdict(), sort_keys=True, allow_nan=False).encode()).hexdigest(),
               "scope": "Static preflight plus causal replay architecture checks. Static scanning cannot prove unexecuted dynamic branches, external point-in-time data correctness, or strategy performance."}
    return receipt, files
