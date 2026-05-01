"""Safe loader for candidate-authored signal engine modules.

Plan §5 Task 3 step 4 forbids loading signal engines from anywhere
outside ``workspace/strategies/<strategy_id>/candidates/<candidate_id>/``.
We enforce this by resolving paths relative to the workspace root and
rejecting anything that escapes.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType

from ...core.errors import NeryaError
from ..artifacts import candidate_signal_engine_path
from .static_check import (
    SignalEngineStaticCheckError,
    static_check_module_path,
)


class SignalEngineLoadError(NeryaError):
    """Raised when a signal engine module cannot be loaded safely."""


def load_signal_engine_module(
    workspace: str | Path,
    strategy_id: str,
    candidate_id: str,
) -> ModuleType:
    """Load and return the candidate ``signal_engine.py`` module.

    The function performs three checks before exec'ing:

    1. The path resolves under ``workspace/strategies/<strategy_id>/candidates/<candidate_id>/``.
    2. The file exists and contains the required ``SignalEngine``
       class with a ``generate`` method.
    3. No banned imports are present.
    """

    workspace_root = Path(workspace).resolve()
    target = candidate_signal_engine_path(workspace_root, strategy_id,
                                            candidate_id).resolve()

    try:
        target.relative_to(workspace_root)
    except ValueError as exc:
        raise SignalEngineLoadError(
            f"signal_engine_outside_workspace:{target}") from exc

    if not target.is_file():
        raise SignalEngineLoadError(f"signal_engine_missing:{target}")

    try:
        static_check_module_path(target)
    except SignalEngineStaticCheckError as exc:
        raise SignalEngineLoadError(str(exc)) from exc

    module_name = (
        f"_nerya_research_signal_{strategy_id}_{candidate_id}_"
        f"{uuid.uuid4().hex[:8]}"
    )

    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:
        raise SignalEngineLoadError(
            f"signal_engine_spec_failed:{target}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - defensive
        sys.modules.pop(module_name, None)
        raise SignalEngineLoadError(
            f"signal_engine_exec_failed:{target}:{exc}") from exc

    if not hasattr(module, "SignalEngine"):
        raise SignalEngineLoadError(
            f"signal_engine_missing_class_after_load:{target}")
    return module


__all__ = ["SignalEngineLoadError", "load_signal_engine_module"]
