"""Live US equities financial data via Financial Datasets API.

Single source: https://api.financialdatasets.ai
Authentication: HTTP header ``x-api-key``. The API key is resolved
from environment variables or :class:`SecretVault`:

- environment:
  - ``FINANCIAL_DATASETS_API_KEY`` (legacy, single)
  - ``NERYA_FINANCIAL_DATASETS_KEYS`` (comma-separated multi-key, preferred)
- vault:
  - ``financial_datasets_api_key`` (legacy, single)
  - ``financial_datasets.keys`` (comma-separated multi-key)

Returned values are wrapped in Nerya's truth envelopes
(:func:`live_envelope` / :func:`degraded_envelope` / :func:`mock_envelope`)
so downstream skills know whether they're looking at live, fallback, or
mock data.

This module never logs or returns the API key.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..connectors.http import HttpTransport, UrllibHttp
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
)


log = logging.getLogger(__name__)


_BASE_URL = "https://api.financialdatasets.ai"
_ENV_KEY_MULTI = "NERYA_FINANCIAL_DATASETS_KEYS"
_ENV_KEY_SINGLE = "FINANCIAL_DATASETS_API_KEY"
_VAULT_KEY_MULTI = "financial_datasets.keys"
_VAULT_KEY_SINGLE = "financial_datasets_api_key"

# Fields that bloat tool outputs without adding analytical signal.
_REDUNDANT_FIELDS: frozenset[str] = frozenset({
    "accession_number", "currency", "period",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in _REDUNDANT_FIELDS}
    if isinstance(value, list):
        return [_strip(x) for x in value]
    return value


def _split_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = raw.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p and p.strip()]


def _resolve_api_keys() -> list[str]:
    """Resolve the FD API key list.

    Priority (later wins, dedup preserved):
        env ``FINANCIAL_DATASETS_API_KEY`` (legacy, single)
        env ``NERYA_FINANCIAL_DATASETS_KEYS`` (CSV, preferred)
        vault ``financial_datasets_api_key`` (single)
        vault ``financial_datasets.keys`` (CSV)
    """
    keys: list[str] = []
    seen: set[str] = set()

    def _push(values: list[str]):
        for v in values:
            if v and v not in seen:
                keys.append(v)
                seen.add(v)

    _push(_split_keys(os.environ.get(_ENV_KEY_SINGLE)))
    _push(_split_keys(os.environ.get(_ENV_KEY_MULTI)))

    workspace = os.environ.get("NERYA_WORKSPACE") or str(Path.home() / ".nerya")
    workspace_path = Path(workspace).expanduser()

    # Optional workspace plaintext fallback (managed by the dashboard
    # ``store: "workspace"`` mode). Mirrors the search-engines pattern.
    plaintext = workspace_path / "financial_datasets.json"
    if plaintext.exists():
        try:
            cfg = json.loads(plaintext.read_text(encoding="utf-8")) or {}
            raw = cfg.get("keys")
            if isinstance(raw, list):
                _push([str(k).strip() for k in raw if str(k).strip()])
            elif isinstance(raw, str):
                _push(_split_keys(raw))
        except Exception:
            pass

    try:
        from ..security.secrets import SecretVault
        vault_path = workspace_path / "vault" / "secrets.enc"
        if vault_path.exists():
            vault = SecretVault.open(vault_path)
            for name in (_VAULT_KEY_SINGLE, _VAULT_KEY_MULTI):
                try:
                    _push(_split_keys(vault.resolve(name)))
                except Exception:
                    continue
    except Exception:
        pass

    return keys


def _envelope_to_dict(env, **extras) -> dict[str, Any]:
    """Inline a RuntimeEnvelope into a plain JSON-able dict."""
    base = env.as_dict() if hasattr(env, "as_dict") else dict(env or {})
    base.update(extras)
    return base


def _build_setup_guidance(*, action: str = "", source_url: str = "") -> dict[str, Any]:
    """Reusable guidance shown when the Financial Datasets key is missing."""
    return {
        "dependency_type": "integration",
        "provider": "financial_datasets",
        "provider_label": "Financial Datasets API",
        "notes": [
            "Configure via environment variables or vault refs only; no "
            "separate skill-level config file is required.",
        ],
        "action": action,
        "source_url": source_url,
        "env": {
            "preferred": [_ENV_KEY_MULTI],
            "legacy": [_ENV_KEY_SINGLE],
        },
        "vault": {
            "preferred": [f"vault://{_VAULT_KEY_MULTI}"],
            "legacy": [f"vault://{_VAULT_KEY_SINGLE}"],
            "scope": "runtime",
        },
        "masked_chat_intake": {
            "enabled": True,
            "placeholder_token": "<<NERYA_SECRET:...>>",
            "help": (
                "Send key(s) directly in chat; secrets are masked before "
                "they reach the model and can be consumed as placeholders."
            ),
        },
        "setup_api": {
            "path": "/data/financial_datasets/keys",
            "method": "POST",
            "example": {"keys": "k1,k2,k3"},
        },
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class EquitiesClient:
    """Thin client over Financial Datasets API.

    Each method returns a dict::

        {
          "data": <stripped payload>,
          "_envelope": {"source": ..., "mode": "live"|"unavailable"|"mock", ...},
          "source_url": "https://api.financialdatasets.ai/..."
        }

    Multi-key rotation: on auth/quota/rate-limit failures the client
    silently rotates to the next key. If every key fails the response is
    marked degraded and ``data`` is empty.
    """

    http: HttpTransport = field(default_factory=UrllibHttp)
    keys: list[str] = field(default_factory=list)
    timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if not self.keys:
            self.keys = _resolve_api_keys()

    # -- low-level -----------------------------------------------------

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{_BASE_URL}{path}"

        if not self.keys:
            log.warning("[equities] no api keys configured for %s", path)
            guidance = _build_setup_guidance(action=f"set key for {path}", source_url=url)
            return {
                "data": {},
                "_envelope": _envelope_to_dict(degraded_envelope(
                    "financial_datasets",
                    error="Financial Datasets API key is not configured.",
                ),
                    missing_key=True,
                    setup_guidance=guidance,
                ),
                "source_url": url,
            }

        clean_params = {
            k: v for k, v in (params or {}).items() if v is not None and v != ""
        }
        last_err: str = ""
        attempted: list[str] = []
        for idx, key in enumerate(self.keys):
            try:
                status, body = self.http.request(
                    "GET", url,
                    headers={"x-api-key": key, "Accept": "application/json"},
                    params=clean_params,
                    timeout=self.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                attempted.append(f"key[{idx}] exception: {last_err}")
                continue

            if status in (200, 201):
                return {
                    "data": _strip(body or {}),
                    "_envelope": _envelope_to_dict(live_envelope(
                        "financial_datasets",
                        provider="financialdatasets.ai",
                    ), key_index=idx),
                    "source_url": _join_url(url, clean_params),
                }
            if status in (401, 402, 403, 429):
                last_err = f"HTTP {status}"
                attempted.append(f"key[{idx}] failover: {last_err}")
                continue  # rotate to next key
            # Non-retryable status — bail.
            last_err = f"HTTP {status}"
            attempted.append(f"key[{idx}] failed: {last_err}")
            break

        guidance = None
        if attempted:
            guidance = _build_setup_guidance(
                action=f"retry key rotation for {path}",
                source_url=url,
            )
        return {
            "data": {},
            "_envelope": _envelope_to_dict(degraded_envelope(
                "financial_datasets",
                error=f"all {len(self.keys)} key(s) exhausted: {last_err}",
                ), key_count=len(self.keys), attempted=attempted,
                setup_guidance=guidance,
            ),
            "source_url": url,
        }

    # -- public surface ------------------------------------------------

    def income_statements(self, ticker: str, *, period: str = "annual",
                          limit: int = 4, **filters: Any) -> dict[str, Any]:
        return self._request(
            "/financials/income-statements/",
            {"ticker": ticker, "period": period, "limit": limit, **filters},
        )

    def balance_sheets(self, ticker: str, *, period: str = "annual",
                       limit: int = 4, **filters: Any) -> dict[str, Any]:
        return self._request(
            "/financials/balance-sheets/",
            {"ticker": ticker, "period": period, "limit": limit, **filters},
        )

    def cash_flow_statements(self, ticker: str, *, period: str = "annual",
                             limit: int = 4, **filters: Any) -> dict[str, Any]:
        return self._request(
            "/financials/cash-flow-statements/",
            {"ticker": ticker, "period": period, "limit": limit, **filters},
        )

    def all_statements(self, ticker: str, *, period: str = "annual",
                       limit: int = 4, **filters: Any) -> dict[str, Any]:
        return self._request(
            "/financials/",
            {"ticker": ticker, "period": period, "limit": limit, **filters},
        )

    def metrics_snapshot(self, ticker: str) -> dict[str, Any]:
        return self._request("/financial-metrics/snapshot",
                             {"ticker": ticker})

    def historical_metrics(self, ticker: str, *, period: str = "annual",
                           limit: int = 20) -> dict[str, Any]:
        return self._request("/financial-metrics/",
                             {"ticker": ticker, "period": period, "limit": limit})

    def analyst_estimates(self, ticker: str, *, limit: int = 8) -> dict[str, Any]:
        return self._request("/analyst-estimates/",
                             {"ticker": ticker, "limit": limit})

    def earnings(self, ticker: str, *, limit: int = 4) -> dict[str, Any]:
        return self._request("/earnings/", {"ticker": ticker, "limit": limit})

    def segments(self, ticker: str, *, period: str = "annual",
                 limit: int = 4) -> dict[str, Any]:
        return self._request("/financials/segments/",
                             {"ticker": ticker, "period": period, "limit": limit})

    def insider_trades(self, ticker: str, *, limit: int = 20) -> dict[str, Any]:
        return self._request("/insider-trades/",
                             {"ticker": ticker, "limit": limit})

    def news(self, ticker: str, *, limit: int = 20) -> dict[str, Any]:
        return self._request("/news/", {"ticker": ticker, "limit": limit})

    def prices(self, ticker: str, *, interval: str = "day",
               limit: int = 120) -> dict[str, Any]:
        return self._request("/prices/",
                             {"ticker": ticker, "interval": interval, "limit": limit})

    def company_facts(self, ticker: str) -> dict[str, Any]:
        return self._request("/company/facts", {"ticker": ticker})

    def filings(self, ticker: str, *, form: str | None = None,
                limit: int = 10) -> dict[str, Any]:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if form:
            params["form"] = form
        return self._request("/filings/", params)

    def screener(self, filters: dict[str, Any]) -> dict[str, Any]:
        return self._request("/screener/", dict(filters or {}))


# ---------------------------------------------------------------------------
# Mock helpers (used when offline / for tests)
# ---------------------------------------------------------------------------


def mock_income_statements(ticker: str = "AAPL", *, periods: int = 4) -> dict[str, Any]:
    """Deterministic mock for offline / test runs."""
    items = [
        {
            "ticker": ticker,
            "report_period": f"2025-12-{31 - i*3:02d}",
            "fiscal_year": 2025 - i,
            "revenue": 400_000_000_000 - i * 25_000_000_000,
            "operating_income": 110_000_000_000 - i * 6_000_000_000,
            "net_income": 95_000_000_000 - i * 5_000_000_000,
            "earnings_per_share_diluted": 6.5 - i * 0.4,
            "gross_margin": 0.46 - i * 0.005,
            "operating_margin": 0.30 - i * 0.005,
        }
        for i in range(periods)
    ]
    return {
        "data": {"income_statements": items},
        "_envelope": _envelope_to_dict(mock_envelope(
            "financial_datasets", provider="financialdatasets.ai",
        )),
        "source_url": f"{_BASE_URL}/financials/income-statements/?ticker={ticker}",
    }


# ---------------------------------------------------------------------------
# Internal: URL builder for source_url field (does not perform request)
# ---------------------------------------------------------------------------


def _join_url(base: str, params: dict[str, Any]) -> str:
    if not params:
        return base
    from urllib.parse import urlencode
    return f"{base}?{urlencode({k: v for k, v in params.items() if v is not None})}"


__all__ = [
    "EquitiesClient",
    "mock_income_statements",
]
