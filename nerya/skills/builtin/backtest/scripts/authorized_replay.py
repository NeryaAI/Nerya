"""Operator-authorized model policy and tape integration, outside candidate code."""

from __future__ import annotations

import math
from pathlib import Path
import re
import time
from dataclasses import replace

from .....core.errors import TradingError
from .....llm.gateway import LLMGateway
from .....llm.messages import OpenAIMessagesBackend
from .....security.secrets import SecretVault
from .....strategies.package import load_package_from_dir
from .decision_producer import DecisionProducer, MAX_RETROSPECTIVE_REPLAY_TOKENS
from .retrospective_replay import run_retrospective


def authorized_policy(config):
    policy = config.get("backtest.retrospective_replay") or {}
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        raise TradingError("retrospective_replay_requires_operator_authorization")
    if (
        not isinstance(policy.get("authorization_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", policy["authorization_id"])
        or type(policy.get("max_calls")) is not int
        or not 1 <= policy["max_calls"] <= 20
        or type(policy.get("max_tokens")) is not int
        or not 1 <= policy["max_tokens"] <= MAX_RETROSPECTIVE_REPLAY_TOKENS
        or type(policy.get("max_output_tokens", 4096)) is not int
        or not 1 <= policy.get("max_output_tokens", 4096) <= 16384
        or not isinstance(policy.get("deadline"), (float, int))
        or not math.isfinite(policy["deadline"])
        or not time.time() < policy["deadline"] <= time.time() + 1800
        or not all(
            isinstance(policy.get(k), str) and policy[k]
            for k in ["provider", "model", "key_ref"]
        )
        or not policy["key_ref"].startswith("vault://")
    ):
        raise TradingError("invalid_or_expired_retrospective_authorization")
    return dict(policy)


def execute_authorized_replay(
    config,
    strategy_root,
    cfg,
    *,
    candles_by_market,
    timeframe_candles_by_market,
    artefacts_dir,
    strategy_config,
    check_cancel=None,
    cancel_token=None,
):
    policy = authorized_policy(config)
    package = load_package_from_dir(Path(strategy_root))
    role = str(
        package.manifest.extras.get("params", {}).get("confirmation_agent") or ""
    )
    if role not in package.manifest.subagents or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,79}", role
    ):
        raise TradingError("retrospective_confirmation_role_required")
    prompt_path = package.root / "subagents" / (role + ".agent.md")
    if (
        prompt_path.is_symlink()
        or not prompt_path.is_file()
        or prompt_path.stat().st_size > 16000
    ):
        raise TradingError("retrospective_role_prompt_missing_or_unsafe")
    prompt = prompt_path.read_text(encoding="utf-8")
    vault = SecretVault.open(config.paths.vault_enc)
    key = bytes.fromhex(
        vault.resolve(policy["key_ref"][8:], required_scope="backtest:decision_sign")
    )
    gateway = LLMGateway(config)
    try:
        tier = str(policy.get("tier") or "medium")
        # One pinned backend: fallbacks would invalidate the signed model policy.
        route = gateway._messages_route_cfgs(
            tier, provider_override=policy["provider"], model_override=policy["model"]
        )[0]
        if (
            route.get("provider") != policy["provider"]
            or route.get("model") != policy["model"]
        ):
            raise TradingError("retrospective_model_route_mismatch")
        backend = gateway._resolve_messages_backend(
            tier,
            route_cfg=route,
            provider_override=policy["provider"],
            model_override=policy["model"],
        )
        # This adapter enforces max_tokens, deadline and cancellation. Other
        # adapters need their own proved bounded contract before enrollment.
        if type(backend) is not OpenAIMessagesBackend:
            raise TradingError("retrospective_backend_not_bounded")
        backend = replace(backend, max_attempts=1)
        producer = DecisionProducer(
            config.paths.vault / "retrospective-decisions.sqlite3",
            authorization_id=policy["authorization_id"],
            backend=backend,
            provider=policy["provider"],
            model=policy["model"],
            max_calls=policy["max_calls"],
            max_tokens=policy["max_tokens"],
            deadline=policy["deadline"],
            cancel_token=cancel_token,
            reasoning_effort=policy.get("reasoning_effort"),
        )
        return run_retrospective(
            package,
            cfg,
            candles_by_market=candles_by_market,
            timeframe_candles_by_market=timeframe_candles_by_market,
            strategy_config=strategy_config,
            producer=producer,
            host_key=key,
            tape_path=Path(artefacts_dir) / "decision_tape.json",
            role=role,
            role_prompt=prompt,
            max_output_tokens=policy.get("max_output_tokens", 4096),
            check_cancel=check_cancel,
        )
    finally:
        if gateway._con is not None:
            gateway._con.close()
