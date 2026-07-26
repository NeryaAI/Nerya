"""Build a compressed context for a SubAgent using only its allowed skills.

Historically the context builder hardcoded two exact skill ids
(``market_data`` + ``news_social``). That made it impossible for a new
skill to plug into the sub-agent context surface without editing Python.

This module now walks each subagent's allowed skills, reads the skill
manifest, and dispatches any action whose tag set advertises a context
capability. Capability tags are declared in the manifest itself, so the
context layer is fully registry-driven:

* ``context.market`` — action should be invoked with ``{market}`` and
  is expected to return ``{context: str}`` or a small structured
  result the builder can render.
* ``context.news`` — action should be invoked with ``{topic, limit}``
  and is expected to return ``{items: [{title}, ...]}``.
Exact-skill-id lookup is preserved as a fallback so existing installs
keep working even when their skill manifests don't yet declare tags.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.config import Config
from ..core.market_defaults import resolve_market_defaults
from ..data.candles import fetch_candles
from ..data.features import compute_features
from ..skills.kernel import SkillKernel
from .registry import SubAgentSpec


# Canonical capability-tag ids. Kept in sync with the skill manifest
CAP_MARKET = "context.market"
CAP_NEWS = "context.news"


def _infer_market_from_text(
    text: str,
    *,
    config: Config | None = None,
    default_venue: str | None = None,
) -> str | None:
    """Parse a natural-language message for a token reference.

    Returns a venue-qualified market id (``BINANCE:BTCUSDT``) or ``None``
    when no known symbol is mentioned.
    """
    if not text or not isinstance(text, str):
        return None
    defaults = resolve_market_defaults(config)
    venue = (default_venue or defaults["venue"]).upper()
    aliases: dict[str, str] = defaults["aliases"]
    quote = defaults["quote"]
    low = text.lower()
    m = re.search(r"\b([a-zA-Z_]+)\s*:\s*([A-Za-z0-9]{3,20})\b", text)
    if m:
        return f"{m.group(1).upper()}:{m.group(2).upper()}"
    for token, symbol in aliases.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", low):
            sym = symbol.upper()
            if len(sym) <= 5 and quote and not sym.endswith(quote):
                sym = f"{sym}{quote}"
            return f"{venue}:{sym}"
    return None


def _iter_capability_actions(
    skills: SkillKernel,
    allowed_skills: list[str],
    capability: str,
) -> list[tuple[str, str]]:
    """Return ``(skill_id, action_name)`` pairs for each action tagged
    with ``capability`` among ``allowed_skills``."""
    out: list[tuple[str, str]] = []
    registry = getattr(skills, "registry", None)
    if registry is None:
        return out
    for skill_id in allowed_skills:
        try:
            entry = registry.get(skill_id)
        except Exception:
            continue
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        for action_name, spec in (manifest.actions or {}).items():
            tags = set(getattr(spec, "tags", []) or [])
            # Also treat a manifest-level ``capabilities`` list as tags.
            tags.update(getattr(manifest, "tags", []) or [])
            if capability in tags:
                out.append((skill_id, action_name))
    return out


def _render_builtin_market_context(config: Config, market: str) -> list[str]:
    rows = fetch_candles(
        market,
        count=96,
        interval="1m",
        allow_mock=False,
        config_like=config,
    )
    features = compute_features(rows)
    if not rows:
        return [
            "-- market_data --",
            (
                f"{market}: no OHLCV rows available; indicators are unavailable "
                "for this prompt."
            ),
        ]
    macd = features.get("macd") if isinstance(features.get("macd"), dict) else {}
    return [
        "-- market_data --",
        (
            f"{market}: rows={len(rows)}, close={features.get('close')}, "
            f"ret_1={features.get('ret_1')}, rsi_14={features.get('rsi_14')}, "
            f"ema_20={features.get('ema_20')}, atr_14={features.get('atr_14')}, "
            f"macd_hist={macd.get('hist')}, "
            f"indicator_backend={features.get('indicator_backend')}"
        ),
    ]


def _render_market_context(
    config: Config, skills: SkillKernel, spec: SubAgentSpec, market: str,
    strategy_id: str | None,
) -> list[str]:
    """Dispatch any ``context.market`` capability-tagged action, falling
    back to the historical ``market_data.compress_context`` behaviour when
    no manifest currently advertises the tag."""
    lines: list[str] = []
    providers = _iter_capability_actions(skills, spec.allowed_skills, CAP_MARKET)
    for skill_id, action_name in providers:
        try:
            summary = skills.call(
                skill_id, action_name,
                payload={"market": market},
                caller=f"subagent:{spec.name}",
                strategy_id=strategy_id,
            )
        except Exception as exc:
            lines.append(f"({skill_id}.{action_name} unavailable: {exc})")
            continue
        lines.append(f"-- {skill_id} --")
        ctx = summary.get("context")
        if isinstance(ctx, str) and ctx.strip():
            lines.append(ctx)
            continue
        # Fallback rendering when the skill returns something richer.
        for k, v in (summary or {}).items():
            if k in ("context", "raw", "envelope", "_envelope"):
                continue
            lines.append(f"{k}: {v}")
    if not lines and (
        "market_data" in spec.allowed_skills
        or "markets" in spec.allowed_skills
    ):
        lines.extend(_render_builtin_market_context(config, market))
    return lines


def _render_news_context(
    skills: SkillKernel, spec: SubAgentSpec, market: str,
    strategy_id: str | None,
) -> list[str]:
    """Dispatch ``context.news`` providers (falling back to the
    historical ``news_social.get_recent_news`` path)."""
    lines: list[str] = []
    providers = _iter_capability_actions(skills, spec.allowed_skills, CAP_NEWS)
    if not providers and "news_social" in spec.allowed_skills:
        providers = [("news_social", "get_recent_news")]
    for skill_id, action_name in providers:
        try:
            res = skills.call(
                skill_id, action_name,
                payload={"topic": market.split(":")[-1], "limit": 3},
                caller=f"subagent:{spec.name}",
                strategy_id=strategy_id,
            )
        except Exception:
            continue
        items = res.get("items") if isinstance(res, dict) else None
        if items:
            lines.append(f"-- {skill_id} --")
            for item in items:
                lines.append(f"* {item.get('title', '')}")
    return lines


def build_context(
    config: Config,
    skills: SkillKernel,
    spec: SubAgentSpec,
    *,
    payload: dict[str, Any],
    strategy_id: str | None = None,
) -> str:
    lines: list[str] = []
    market = payload.get("market") or payload.get("symbol")
    if not market:
        text = payload.get("text") or payload.get("message") or payload.get("prompt")
        market = _infer_market_from_text(text or "", config=config)
        if market:
            lines.append(f"(market inferred from user text: {market})")
    if market:
        lines.extend(_render_market_context(config, skills, spec, market, strategy_id))
        lines.extend(_render_news_context(skills, spec, market, strategy_id))
    return "\n".join(lines) or "(no structured context)"
