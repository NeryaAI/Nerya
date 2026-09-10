"""Framework-compat layer — run mainstream quant strategies on Nerya.

This package lets a Nerya strategy package host strategy code written
for a *foreign* quant framework (Freqtrade, VNpy, …) without installing
that framework. Three pieces cooperate:

* :mod:`.detect` — AST-scans uploaded source files and answers "which
  framework is this" (``freqtrade`` / ``vnpy`` / ``unknown``).
* :mod:`.shims` — lightweight, dependency-free stand-ins for the
  framework base classes (``freqtrade.strategy.IStrategy``,
  ``vnpy_ctastrategy.CtaTemplate``, ``vnpy.trader.utility`` utilities).
  They are registered into :data:`sys.modules` under the *real* module
  names, but only when the genuine framework is not importable, so a
  strategy written against the real package keeps using it.
* :mod:`.freqtrade_adapter` / :mod:`.vnpy_adapter` — tick executors
  that drive the foreign strategy class from Nerya's
  :class:`~nerya.strategies.context.StrategyContext` (candles in,
  risk-gated intents out) with identical semantics in live runs and
  backtest replay.

The importer (:mod:`.importer`) turns uploaded source files into a
regular Nerya strategy package whose generated ``main.py`` entrypoint
delegates to :func:`nerya.strategies.run_compat_tick`. Nothing in the
runner, validator, scheduler, or backtest engine changes: a compat
package is an ordinary package.
"""

from __future__ import annotations

from .detect import (
    FrameworkInfo,
    detect_framework,
    detect_frameworks,
)
from .entrypoint import (
    CompatRunError,
    load_framework_strategy,
    run_compat_tick,
)
from .importer import ImportExternalResult, import_external_strategy

__all__ = [
    "CompatRunError",
    "FrameworkInfo",
    "ImportExternalResult",
    "detect_framework",
    "detect_frameworks",
    "import_external_strategy",
    "load_framework_strategy",
    "run_compat_tick",
]
