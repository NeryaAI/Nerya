"""Multi-engine web search with per-engine multi-key rotation.

Supported engines (in default chain order):
``exa`` → ``tavily`` → ``perplexity`` → ``brave`` → ``serper`` →
``bing`` → ``duckduckgo`` (keyless fallback) → ``duckduckgo_lite``.

Standalone CLI usage::

    python -m nerya.skills.builtin.research.scripts.web_search \\
        --json '{"query": "Apple Q4 earnings", "max_results": 8}'

    # Force a specific engine chain:
    python -m nerya.skills.builtin.research.scripts.web_search \\
        --json '{"query": "...", "engines": ["exa", "tavily", "duckduckgo"]}'

    # Inline keys (otherwise resolved from workspace/search_engines.json
    # → NERYA_SEARCH_<ENGINE>_KEYS env → vault://search.<engine>.keys):
    python -m nerya.skills.builtin.research.scripts.web_search --json '{
      "query": "...",
      "engines": ["exa"],
      "keys": {"exa": ["k1", "k2"]}
    }'

Output schema::

    {
      "ok": bool,
      "query": str,
      "engine": str,            # engine that returned the results
      "engine_chain": [str],    # full chain considered
      "key_index": int,         # which key (0-based) succeeded; -1 if keyless
      "fallback_errors": [str], # one entry per failed engine attempt
      "elapsed_ms": int,
      "count": int,
      "results": [{"title": str, "url": str, "snippet": str,
                   "source": str, "engine": str, "key_index": int}]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from ._engine_config import resolve_config
from ._engines import (
    EngineError,
    EngineKeyExhausted,
    EngineKeylessFailure,
    build_adapter,
)


_DEFAULT_MAX_RESULTS = 8
_HARD_RESULT_CAP = 25
_KEYLESS_FALLBACK_ENGINES = ("searxng", "duckduckgo", "duckduckgo_lite")


def _normalize_keys_arg(value: Any) -> dict[str, list[str]]:
    if not value:
        return {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for engine, keys in value.items():
        if isinstance(keys, str):
            out[engine] = [k.strip() for k in keys.split(",") if k.strip()]
        elif isinstance(keys, list):
            out[engine] = [str(k).strip() for k in keys if str(k).strip()]
    return out


def _normalize_base_urls_arg(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for engine, url in value.items():
        if not isinstance(engine, str):
            continue
        text = str(url or "").strip().rstrip("/")
        if text:
            out[engine.strip().lower()] = text
    return out


def run(
    *,
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    engine: str | None = None,
    engines: list[str] | None = None,
    keys: dict[str, list[str]] | None = None,
    base_urls: dict[str, str] | None = None,
    **engine_kwargs: Any,
) -> dict[str, Any]:
    if not query:
        return {"ok": False, "error": "query is required", "results": []}

    max_results = max(1, min(int(max_results), _HARD_RESULT_CAP))
    if engine and not engines:
        engines = [engine]

    cfg = resolve_config(
        engines=engines,
        region=region,
        safesearch=safesearch,
        extra_keys=_normalize_keys_arg(keys),
        extra_base_urls=_normalize_base_urls_arg(base_urls),
    )
    chain = cfg.usable_chain()
    engine_chain = list(cfg.engines)
    config_sources = dict(cfg.sources)
    initial_unusable_errors = [
        f"{e.name}: no API keys configured" for e in cfg.engines if not e.usable
    ]
    if not chain and engines:
        seen = {e.name for e in cfg.engines}
        fallback_engines = [
            name for name in _KEYLESS_FALLBACK_ENGINES if name not in seen
        ]
        if fallback_engines:
            fallback_cfg = resolve_config(
                engines=fallback_engines,
                region=region,
                safesearch=safesearch,
                extra_keys=_normalize_keys_arg(keys),
                extra_base_urls=_normalize_base_urls_arg(base_urls),
            )
            fallback_chain = fallback_cfg.usable_chain()
            engine_chain.extend(fallback_cfg.engines)
            if fallback_chain:
                chain = fallback_chain
                config_sources["fallback"] = "keyless"
    if not chain:
        return {
            "ok": False,
            "query": query,
            "engine_chain": [e.name for e in engine_chain],
            "fallback_errors": initial_unusable_errors
            or [f"{e.name}: unavailable" for e in engine_chain],
            "elapsed_ms": 0,
            "count": 0,
            "results": [],
            "error": "no usable engines (configure keys or fall back to duckduckgo)",
        }

    started = time.monotonic()
    errors: list[str] = []
    used_engine = ""
    used_key_index = -1
    results: list[dict[str, Any]] = []

    for spec in chain:
        try:
            adapter = build_adapter(spec.name, spec.keys, **spec.adapter_config())
        except ValueError as exc:
            errors.append(f"{spec.name}: {exc}")
            continue
        try:
            results = adapter.run(
                query=query, max_results=max_results,
                region=cfg.region, safesearch=cfg.safesearch,
                **engine_kwargs,
            )
        except EngineKeyExhausted as exc:
            errors.append(str(exc))
            continue
        except EngineKeylessFailure as exc:
            errors.append(f"{spec.name}: {exc}")
            continue
        except EngineError as exc:
            errors.append(f"{spec.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{spec.name}: {type(exc).__name__}: {exc}")
            continue
        if results:
            used_engine = spec.name
            used_key_index = results[0].get("key_index", -1)
            break
        errors.append(f"{spec.name}: empty result set")

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if not results:
        return {
            "ok": False,
            "query": query,
            "engine_chain": [e.name for e in engine_chain],
            "fallback_errors": [*initial_unusable_errors, *errors],
            "elapsed_ms": elapsed_ms,
            "count": 0,
            "results": [],
        }

    return {
        "ok": True,
        "query": query,
        "engine": used_engine,
        "engine_chain": [e.name for e in engine_chain],
        "key_index": used_key_index,
        "fallback_errors": [*initial_unusable_errors, *errors],
        "elapsed_ms": elapsed_ms,
        "count": len(results),
        "results": results,
        "config_sources": config_sources,
    }


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--query", dest="query", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    query = args.query or payload.get("query") or ""
    engines_raw = payload.get("engines")
    engines: list[str] | None = None
    if isinstance(engines_raw, str):
        engines = [e.strip() for e in engines_raw.split(",") if e.strip()]
    elif isinstance(engines_raw, list):
        engines = [str(e).strip() for e in engines_raw if str(e).strip()]

    try:
        result = run(
            query=query,
            max_results=int(payload.get("max_results") or _DEFAULT_MAX_RESULTS),
            region=str(payload.get("region") or "wt-wt"),
            safesearch=str(payload.get("safesearch") or "moderate"),
            engine=payload.get("engine"),
            engines=engines,
            keys=payload.get("keys") or None,
            base_urls=payload.get("base_urls") or None,
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
