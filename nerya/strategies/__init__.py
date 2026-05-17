"""nerya.strategies — Agent-generated strategy runtime.

Implements the 7 plan in

A strategy is an *agent-generated package* under
``workspace/strategies/<strategy_id>/`` containing:

* ``strategy.yml`` — typed manifest (markets, accounts, schedule, policy,
  optional tuning block, llm policy, subagents).
* ``strategy.md`` — operator-readable playbook / intent.
* ``main.py`` — the entrypoint imported by :class:`StrategyRunner`. It
  exposes ``run(ctx: StrategyContext) -> StrategyResult``.
* ``subagents/<name>.agent.md`` — strategy-scoped subagent prompts.
* ``runs/<run_id>.json`` / ``state/state.json`` / ``versions/<hash>.json``
  — per-run artifacts written by the runner.

Generated code only sees the :class:`StrategyContext` facade — no raw
``Config``, vault, signer, connector, or ``SkillKernel``. Every order
goes through ``ctx.trading.submit_intent`` which still passes through
the existing risk-gated trading kernel.

Public surface:

* :class:`StrategyManifest` / :class:`StrategyPackage` — typed loaders.
* :class:`StrategyContext` / :class:`StrategyResult` — runtime facade.
* :class:`StrategyRunner` — single-tick executor.
"""

from __future__ import annotations

from .package import (
    StrategyAgentProfile,
    StrategyAgentSessionConfig,
    StrategyLLMPolicy,
    StrategyManifest,
    StrategyPackage,
    StrategyPolicy,
    StrategySchedule,
    StrategyTuningConfig,
    load_package,
    load_packages,
)
from .agent_task import StrategyAgentTask
from .prompt_io import StrategyPromptIO
from .result import ResultBuilder, StrategyResult, StrategyResultStatus
from .context import (
    StrategyAudit,
    StrategyClock,
    StrategyConfig,
    StrategyContext,
    StrategyDedupe,
    StrategyLLMFacade,
    StrategyMarket,
    StrategyMessages,
    StrategyNews,
    StrategyPnL,
    StrategyPortfolio,
    StrategyPosition,
    StrategyPolicyView,
    StrategyRuntimeError,
    StrategyState,
    StrategySubAgents,
    StrategyTrading,
    StrategyTriggerContext,
    build_strategy_context,
)
from .state import (
    KillSwitchState,
    StrategyKillSwitch,
    StrategyRunRecord,
    StrategyRunStore,
    StrategyVersionRecord,
    StrategyVersionRegistry,
    new_run_id,
)
from .runner import (
    StrategyRunInputs,
    StrategyRunOutputs,
    StrategyRunner,
    StrategyTimeoutError,
)
from .performance import StrategyPerformanceSnapshot, build_snapshot
from .evolution import StrategyEvolutionRunner, TuningRunResult

__all__ = [
    "KillSwitchState",
    "ResultBuilder",
    "StrategyAudit",
    "StrategyAgentProfile",
    "StrategyAgentSessionConfig",
    "StrategyAgentTask",
    "StrategyClock",
    "StrategyConfig",
    "StrategyContext",
    "StrategyDedupe",
    "StrategyEvolutionRunner",
    "StrategyKillSwitch",
    "StrategyLLMFacade",
    "StrategyLLMPolicy",
    "StrategyManifest",
    "StrategyMarket",
    "StrategyMessages",
    "StrategyNews",
    "StrategyPnL",
    "StrategyPackage",
    "StrategyPerformanceSnapshot",
    "StrategyPortfolio",
    "StrategyPosition",
    "StrategyPromptIO",
    "StrategyPolicy",
    "StrategyPolicyView",
    "StrategyResult",
    "StrategyResultStatus",
    "StrategyRunInputs",
    "StrategyRunOutputs",
    "StrategyRunRecord",
    "StrategyRunStore",
    "StrategyRunner",
    "StrategyRuntimeError",
    "StrategySchedule",
    "StrategyState",
    "StrategySubAgents",
    "StrategyTimeoutError",
    "StrategyTrading",
    "StrategyTriggerContext",
    "StrategyTuningConfig",
    "StrategyVersionRecord",
    "StrategyVersionRegistry",
    "TuningRunResult",
    "build_snapshot",
    "build_strategy_context",
    "load_package",
    "load_packages",
    "new_run_id",
]
