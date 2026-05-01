"""LLM SDK wrapper. Calls go through the llm_skill, never directly to providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..skills.kernel import SkillKernel


@dataclass
class LLMAPI:
    config: Config
    skills: SkillKernel

    def _call(self, action: str, payload: dict[str, Any],
              *, caller: str = "sdk") -> dict[str, Any]:
        return self.skills.call("llm", action, payload=payload, caller=caller)

    def classify(self, *, prompt: str, labels: list[str] | None = None,
                 caller: str = "sdk") -> dict[str, Any]:
        return self._call("classify", {"prompt": prompt, "labels": labels or []},
                          caller=caller)

    def compress(self, *, text: str, max_tokens: int = 512,
                 caller: str = "sdk") -> dict[str, Any]:
        return self._call("compress", {"text": text, "max_tokens": max_tokens},
                          caller=caller)

    def extract_json(self, *, prompt: str, schema: dict | None = None,
                     tier: str = "light", caller: str = "sdk") -> dict[str, Any]:
        return self._call("extract_json",
                          {"prompt": prompt, "schema": schema, "tier": tier},
                          caller=caller)

    def analyze_signal(self, *, context: str, schema: dict | None = None,
                       caller: str = "sdk") -> dict[str, Any]:
        return self._call("analyze_signal",
                          {"context": context, "schema": schema},
                          caller=caller)

    def generate_script_proposal(self, *, goal: str, constraints: dict | None = None,
                                 caller: str = "sdk") -> dict[str, Any]:
        return self._call("generate_script_proposal",
                          {"goal": goal, "constraints": constraints or {}},
                          caller=caller)

    def capabilities(self) -> dict[str, Any]:
        """Return the live provider capability matrix.

        Mirrors :meth:`nerya.llm.gateway.LLMGateway.capabilities`; exposed
        here so SDK callers (dashboards, CLI, tests) can see per-tier
        provider capability support without having to reach into the
        internal gateway.
        """
        from ..llm.gateway import LLMGateway

        gateway = LLMGateway(self.config)
        return gateway.capabilities()

    def reasoning_settings(self) -> dict[str, Any]:
        """Return per-tier reasoning configuration.

        Output shape::

            {
              "light":  {"provider": "openai",   "model": "gpt-5.4-mini",
                          "reasoning_effort": "low",
                          "reasoning_summary": "concise"},
              "medium": {...},
              "high":   {...},
            }

        SDK callers (CLI ``nerya doctor``, dashboard ``Settings → Models``,
        e2e harnesses) use this to verify the reasoning knobs are wired
        end to end without dispatching a real call. Empty strings are
        returned when the tier is not configured for reasoning (e.g.
        non-reasoning model or knob unset).
        """
        caps = self.capabilities() or {}
        tiers = (caps.get("tiers") or {}) if isinstance(caps, dict) else {}
        out: dict[str, Any] = {}
        for name, info in tiers.items():
            if not isinstance(info, dict):
                continue
            out[name] = {
                "provider": info.get("provider", ""),
                "model": info.get("model", ""),
                "reasoning_effort": info.get("reasoning_effort", ""),
                "reasoning_summary": info.get("reasoning_summary", ""),
            }
        return out
