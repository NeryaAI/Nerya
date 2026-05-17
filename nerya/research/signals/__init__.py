"""Signal engine contract.

A signal engine is an agent-authored Python module under a candidate
directory.  It MUST expose a class named ``SignalEngine`` with a
``generate(data_map)`` method.  The output is normalised into
:class:`SignalFrame` records, which are then compiled into
:class:`IntentCandidate` records by :mod:`nerya.research.signals.compiler`.

We never call exchange or wallet APIs from research code.
"""
from __future__ import annotations

from .compiler import (
    IntentCandidate,
    compile_signal_to_intent_candidate,
    compile_signals,
)
from .loader import SignalEngineLoadError, load_signal_engine_module
from .protocol import (
    SignalEngineProtocol,
    SignalFrame,
    SignalFrameError,
    coerce_signal_frame,
)
from .static_check import (
    SignalEngineStaticCheckError,
    static_check_module_path,
    static_check_source,
)

__all__ = [
    "IntentCandidate",
    "SignalEngineLoadError",
    "SignalEngineProtocol",
    "SignalEngineStaticCheckError",
    "SignalFrame",
    "SignalFrameError",
    "coerce_signal_frame",
    "compile_signal_to_intent_candidate",
    "compile_signals",
    "load_signal_engine_module",
    "static_check_module_path",
    "static_check_source",
]
