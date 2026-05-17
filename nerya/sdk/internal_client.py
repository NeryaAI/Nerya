"""InternalClient — the single in-process handle used by skills, SubAgents,
the CLI and the file-based SDK bridge."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import Config, load_config
from ..skills.kernel import SkillKernel
from ..triggers.runtime import TriggerRuntime
from .agent_api import AgentAPI
from .llm_api import LLMAPI
from .message_api import MessageAPI
from .skill_api import SkillAPI
from .strategy_api import StrategyAPI
from .trading_api import TradingAPI
from .trigger_api import TriggerAPI


@dataclass
class InternalClient:
    config: Config
    skills: SkillKernel
    triggers_runtime: TriggerRuntime
    triggers: TriggerAPI
    trading: TradingAPI
    llm: LLMAPI
    strategy: StrategyAPI
    messages: MessageAPI
    skill: SkillAPI
    agent: AgentAPI

    @classmethod
    def boot(
        cls,
        workspace: str | Path | None = None,
        *,
        profile: str | None = None,
    ) -> "InternalClient":
        config = load_config(workspace, profile=profile)
        return cls.from_config(config)

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        skills: SkillKernel | None = None,
    ) -> "InternalClient":
        skills = skills or SkillKernel.boot(config)
        triggers_runtime = TriggerRuntime.boot(config)
        return cls(
            config=config,
            skills=skills,
            triggers_runtime=triggers_runtime,
            triggers=TriggerAPI(config=config, runtime=triggers_runtime),
            trading=TradingAPI(config=config, skills=skills),
            llm=LLMAPI(config=config, skills=skills),
            strategy=StrategyAPI(config=config, skills=skills),
            messages=MessageAPI(config=config, skills=skills),
            skill=SkillAPI(config=config, skills=skills),
            agent=AgentAPI(config=config, skills=skills),
        )


def boot(
    workspace: str | Path | None = None,
    *,
    profile: str | None = None,
) -> InternalClient:
    return InternalClient.boot(workspace, profile=profile)
