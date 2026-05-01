"""Usage journal writer + thin in-memory/SQLite spend tracker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.redaction import fingerprint, redact_dict


@dataclass
class LLMUsageJournal:
    journal_path: Path
    security_path: Path

    def record(self, *, tier: str, task: str, caller: str, tokens: int,
               usd: float, prompt_preview: str, response_preview: str,
               debug_full: bool = False,
               reasoning_text: str = "",
               reasoning_tokens: int = 0,
               reasoning_effort: str = "",
               provider: str = "",
               model: str = "") -> None:
        base: dict[str, Any] = {
            "kind": "llm.call",
            "tier": tier,
            "task": task,
            "caller": caller,
            "tokens": int(tokens),
            "usd": round(float(usd), 5),
            "prompt_sha12": fingerprint(prompt_preview),
            "prompt_len": len(prompt_preview),
            "response_sha12": fingerprint(response_preview),
            "response_len": len(response_preview),
        }
        if provider:
            base["provider"] = provider
        if model:
            base["model"] = model
        if reasoning_effort:
            base["reasoning_effort"] = reasoning_effort
        if reasoning_tokens:
            base["reasoning_tokens"] = int(reasoning_tokens)
        if reasoning_text:
            # Reasoning summaries can be long; keep a length+sha by default
            # and only persist the full text under debug_full.
            base["reasoning_sha12"] = fingerprint(reasoning_text)
            base["reasoning_len"] = len(reasoning_text)
            if debug_full:
                base["reasoning"] = reasoning_text
        if debug_full:
            base["prompt"] = prompt_preview
            base["response"] = response_preview
        jsonl.append(self.journal_path, base)
        jsonl.append(self.security_path, {
            "kind": "llm.audit",
            "caller": caller,
            "payload": redact_dict({"tier": tier, "task": task,
                                     "tokens": tokens, "usd": usd,
                                     "reasoning_tokens": reasoning_tokens,
                                     "reasoning_effort": reasoning_effort}),
        })
