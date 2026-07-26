"""Connector / venue native tools.

These exist so the agent can authoritatively answer the question
*"is exchange X / data source Y already integrated?"* by querying the
in-process :class:`ExchangeProviderRegistry` instead of guessing from
the SKILL.md prose. The model historically claimed Polymarket "isn't
integrated" while a fully working :class:`PolymarketConnector` lived
under ``nerya/connectors/polymarket.py`` — that gap caused the operator
to ship a placeholder strategy with no real data feed. Promoting the
registry to a tool closes the loop.

Two tools live here:

* :func:`connector_list_handler` — every registered provider with
  ``id``, ``label``, ``kind``, ``aliases``, ``runtime``,
  ``install_hint``, ``description``, ``supports`` matrix, doc links
  *and* the absolute path to its source file when it's a builtin /
  workspace provider. The agent can read the source to learn the
  exact API shape before depending on it in a strategy.
* :func:`connector_view_handler` — the same payload narrowed to one
  provider, plus a ``source`` field that holds the first ~24KB of the
  module so the agent can confirm endpoint URLs / method names without
  also calling :func:`read_file`.

Both tools are read-only and safe — they don't open sockets, they
just enumerate the in-memory specs that ``provider_spec`` already built
at import time.
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import os
from pathlib import Path
from typing import Any

from ...connectors.mock_exchange import MockExchange
from ...connectors.provider_spec import CredentialField, ExchangeProviderSpec, get_registry
from ...connectors.registry import build_connector
from ...core.market_defaults import resolve_market_defaults
from ...core.truth import degraded_envelope, live_envelope
from ...data.candles import canonical_venue, fetch_candles, fetch_public_ticker
from ...data.features import compute_features
from ..tool_errors import schema_validation_result as _usage_error
from ..types import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


CONNECTOR_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": (
                "Optional filter. One of cex|dex|chain|prediction_market|"
                "options|futures|derivatives. Case-insensitive."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Optional free-text match against id / label / aliases / "
                "description (case-insensitive substring). Useful when the "
                "operator asks 'is polymarket integrated?' — pass query="
                "'polymarket' and look at ``count``."
            ),
        },
        "include_source": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, include the module file path for each builtin "
                "provider. Off by default to keep responses small."
            ),
        },
    },
}


CONNECTOR_VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "Provider id (or a venue alias). e.g. ``polymarket``, "
                "``binance``, ``bsc``. Case-insensitive."
            ),
        },
        "max_source_bytes": {
            "type": "integer",
            "minimum": 0,
            "default": 24000,
            "description": (
                "Cap on how many bytes of the connector source to inline "
                "when include_source is true. Set to 0 to skip the source "
                "body and only get metadata."
            ),
        },
        "include_source": {
            "type": "boolean",
            "default": False,
            "description": (
                "Include connector source code. Leave false for strategy/data "
                "routing; source reads are only for connector implementation "
                "or debugging."
            ),
        },
    },
    "required": ["id"],
}


MARKET_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "get_ticker",
                "get_mark_price",
                "get_candles",
                "calculate_features",
                "summarize_market",
                "compress_context",
            ],
            "description": (
                "Read-only market-data action. Use get_candles for OHLCV, "
                "calculate_features for last-bar indicators, summarize_market "
                "or compress_context for prompt-ready context."
            ),
        },
        "venue": {
            "type": "string",
            "description": "Optional venue when market/symbol is unqualified, e.g. binance.",
        },
        "market": {
            "type": "string",
            "description": (
                "Required market id, preferably VENUE:SYMBOL. For US equities "
                "use venue='yahoo' with market='NVDA'. For crypto use e.g. "
                "BINANCE:BTCUSDT."
            ),
        },
        "symbol": {
            "type": "string",
            "description": "Alias for market when venue is also supplied.",
        },
        "interval": {
            "type": "string",
            "default": "1m",
            "description": "Candle interval such as 1m, 5m, 1h, 1d.",
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 96,
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "Alias for count.",
        },
        "timeout_s": {
            "type": "number",
            "minimum": 0.25,
            "maximum": 15,
            "default": 5,
            "description": (
                "Optional per-request market data timeout in seconds. "
                "The runtime caps this so slow public feeds degrade quickly."
            ),
        },
    },
    "required": ["action"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATA_SOURCE_API_KEY_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "tushare": ("TUSHARE_TOKEN", "TUSHARE_API_KEY", "NERYA_TUSHARE_TOKEN"),
    "polygon_io": ("POLYGON_API_KEY", "POLYGON_IO_API_KEY", "NERYA_POLYGON_API_KEY"),
    "coinmarketcap": ("COINMARKETCAP_API_KEY", "CMC_API_KEY", "NERYA_CMC_API_KEY"),
    "glassnode": ("GLASSNODE_API_KEY", "NERYA_GLASSNODE_API_KEY"),
    "dune": ("DUNE_API_KEY", "NERYA_DUNE_API_KEY"),
    "messari": ("MESSARI_API_KEY", "NERYA_MESSARI_API_KEY"),
}


def _factory_module_path(spec: ExchangeProviderSpec) -> Path | None:
    """Resolve the on-disk module that defines this provider's connector.

    ``spec.factory`` is a closure built inside ``_register_builtins`` so
    its ``__module__`` always points at ``nerya.connectors.provider_spec``,
    not at ``polymarket`` / ``ccxt_adapter`` / etc. We grab the module
    of the first non-callable global the factory touches by inspecting
    its closure cells.
    """

    factory = spec.factory
    if factory is None:
        return None
    # Closures expose freevars / cells — try them first.
    try:
        cells = getattr(factory, "__closure__", None) or ()
        for cell in cells:
            try:
                obj = cell.cell_contents
            except ValueError:
                continue
            mod = inspect.getmodule(obj)
            if mod is None:
                continue
            f = getattr(mod, "__file__", None)
            if f and "connectors" in f and "provider_spec" not in f:
                return Path(f)
    except Exception:
        pass
    # Fallback: rely on the spec id (best-effort, may miss for ccxt).
    candidates = {
        "polymarket": "polymarket.py",
        "binance": "ccxt_adapter.py",
        "bybit": "ccxt_adapter.py",
        "okx": "ccxt_adapter.py",
        "hyperliquid": "ccxt_adapter.py",
        "ccxt": "ccxt_adapter.py",
        "mock": "mock_exchange.py",
        "mock_chain": "mock_chain.py",
        "evm": "evm_native.py",
        "bsc": "bsc_native.py",
        "solana": "solana_native.py",
    }
    fname = candidates.get(spec.id)
    if not fname:
        return None
    here = Path(__file__).resolve().parents[2] / "connectors" / fname
    return here if here.exists() else None


def _spec_to_dict(
    spec: ExchangeProviderSpec, *, include_source_path: bool = False,
    config_like: Any | None = None,
) -> dict[str, Any]:
    info = spec.to_info()
    info["credential_status"] = _credential_status(spec, config_like=config_like)
    if include_source_path:
        src = _factory_module_path(spec)
        info["source_path"] = str(src) if src is not None else None
    return info


def _credential_status(
    spec: ExchangeProviderSpec,
    *,
    config_like: Any | None = None,
) -> dict[str, Any]:
    required = [field for field in spec.credential_fields if field.required]
    if not required:
        return {
            "required": False,
            "status": "not_required",
            "configured": True,
            "required_fields": [],
            "configured_accounts": [],
            "env_candidates": [],
        }

    required_names = [field.name for field in required]
    configured_accounts = _configured_credential_accounts(
        spec,
        required_fields=required,
        config_like=config_like,
    )
    env_candidates = _credential_env_candidates(spec, required_fields=required)
    configured_by_env = _env_satisfies_required(required, env_candidates)
    configured = bool(configured_accounts) or configured_by_env
    return {
        "required": True,
        "status": "configured" if configured else "missing",
        "configured": configured,
        "required_fields": required_names,
        "configured_accounts": configured_accounts,
        "env_candidates": env_candidates,
        "should_retry": configured,
        "next_required_action": (
            ""
            if configured
            else "configure_provider_credentials"
        ),
        "operator_message": (
            ""
            if configured
            else (
                f"{spec.label} requires credentials before live data can be "
                "verified. Configure a vault-backed account credential or an "
                "approved environment variable, then retry the data request."
            )
        ),
    }


def _credential_env_candidates(
    spec: ExchangeProviderSpec,
    *,
    required_fields: list[CredentialField],
) -> list[dict[str, Any]]:
    env_names = _DATA_SOURCE_API_KEY_ENV_NAMES.get(spec.id, ())
    if not env_names:
        return []
    required_names = {field.name for field in required_fields}
    if "api_key" not in required_names:
        return []
    return [
        {
            "field": "api_key",
            "name": name,
            "present": bool(os.environ.get(name, "").strip()),
        }
        for name in env_names
    ]


def _env_satisfies_required(
    required_fields: list[CredentialField],
    env_candidates: list[dict[str, Any]],
) -> bool:
    required_names = {field.name for field in required_fields}
    if required_names == {"api_key"}:
        return any(bool(row.get("present")) for row in env_candidates)
    return False


def _configured_credential_accounts(
    spec: ExchangeProviderSpec,
    *,
    required_fields: list[CredentialField],
    config_like: Any | None,
) -> list[dict[str, Any]]:
    paths = getattr(config_like, "paths", None) if config_like is not None else None
    if paths is None:
        return []
    try:
        from ...trading.accounts import load_accounts
        accounts = load_accounts(paths)
    except Exception:
        return []
    accepted_venues = {spec.id, *spec.aliases}
    required_names = [field.name for field in required_fields]
    out: list[dict[str, Any]] = []
    for acc in accounts.values():
        venue = (getattr(acc, "venue", "") or "").strip().lower()
        if venue not in accepted_venues:
            continue
        raw = dict(getattr(acc, "raw", {}) or {})
        creds = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
        present_fields = [
            name for name in required_names
            if str(creds.get(name) or "").strip()
        ]
        if len(present_fields) != len(required_names):
            continue
        out.append({
            "id": str(getattr(acc, "id", "") or raw.get("id") or ""),
            "venue": venue,
            "fields": present_fields,
            "uses_vault_ref": {
                name: str(creds.get(name) or "").startswith("vault://")
                for name in present_fields
            },
        })
    return out


def _credential_missing_payload(
    venue: str,
    market: str,
    *,
    config_like: Any | None,
    capability: str,
) -> dict[str, Any] | None:
    spec = get_registry().find(venue)
    if spec is None:
        return None
    status = _credential_status(spec, config_like=config_like)
    if status.get("status") != "missing":
        return None
    return {
        "venue": venue,
        "market": market,
        "error": "credential_missing",
        "credential_status": status,
        "should_retry": False,
        "next_required_action": "configure_provider_credentials",
        "_envelope": degraded_envelope(
            capability,
            error="credential_missing",
            venue=venue.lower(),
        ).as_dict(),
    }


def _normalize_market(venue: str, market: str) -> str:
    venue_l = (venue or "").strip().lower()
    wallet_prefixes = {
        "okx_onchain": "OKX_ONCHAIN",
        "okx_os": "OKX_ONCHAIN",
        "bitget_onchain": "BITGET_ONCHAIN",
        "bitget_wallet": "BITGET_ONCHAIN",
        "binance_alpha": "BINANCE_ALPHA",
        "binance_web3": "BINANCE_ALPHA",
        "coinbase_wallet": "COINBASE_WALLET",
        "coinbase_exchange_wallet": "COINBASE_WALLET",
        "onchain": "ONCHAIN",
    }
    if venue_l in wallet_prefixes:
        prefix = wallet_prefixes[venue_l]
        if market.upper().startswith(f"{prefix}:"):
            return market
        return f"{prefix}:{market}"
    if ":" in market:
        return market
    return f"{venue.upper()}:{market.upper()}"


def _market_id(args: dict[str, Any], *, config_like: Any | None) -> tuple[str, str]:
    defaults = resolve_market_defaults(config_like)
    raw_venue = args.get("venue") or args.get("source")
    market = str(args.get("market") or args.get("symbol") or defaults["symbol"])
    if raw_venue:
        venue = str(raw_venue)
    elif ":" in market:
        venue = market.split(":", 1)[0]
    else:
        venue = str(defaults["venue"])
    return venue, _normalize_market(venue, market)


def _market_timeout_s(args: dict[str, Any]) -> float:
    raw = args.get("timeout_s")
    if raw is None:
        raw = os.environ.get("NERYA_MARKET_DATA_TIMEOUT_SECONDS") or 5.0
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = 5.0
    return max(0.25, min(timeout, 15.0))


def _apply_connector_timeout(conn: Any, timeout_s: float) -> Any:
    if conn is None:
        return conn
    try:
        current = getattr(conn, "timeout", None)
        setattr(conn, "timeout", min(float(current), timeout_s) if current else timeout_s)
    except Exception:
        pass
    try:
        timeout_ms = int(timeout_s * 1000)
        current_ms = getattr(conn, "timeout_ms", None)
        setattr(conn, "timeout_ms", min(int(current_ms), timeout_ms) if current_ms else timeout_ms)
    except Exception:
        pass
    return conn


_PUBLIC_MARKET_DATA_VENUES = frozenset(
    {
        "YAHOO",
        "BINANCE",
        "BINANCE_PERPETUAL",
        "OKX",
        "BYBIT",
        "BYBIT_PERPETUAL",
        "KRAKEN",
    }
)


def _venue_has_public_market_feed(venue: str) -> bool:
    """True when ``venue`` serves keyless public OHLCV / ticker.

    Read-only market data for these venues never needs trading API keys, so
    the data tools must not short-circuit on ``credential_missing`` for them.
    That false gate is what made the agent report it "could not fetch real
    klines" for BINANCE:BTCUSDT during strategy design — even though the
    public REST feed (and the backtest engine) load real candles with no
    keys at all. Credentialed-only data sources (Glassnode, Polygon.io,
    Tushare, …) are intentionally absent here so their credential guidance is
    preserved.
    """

    return canonical_venue(venue) in _PUBLIC_MARKET_DATA_VENUES


def _public_connector(
    venue: str,
    *,
    config_like: Any | None,
    timeout_s: float | None = None,
) -> Any | None:
    venue_l = (venue or "").strip().lower()
    if venue_l in {"mock", "paper"}:
        return MockExchange()
    if not venue_l:
        return None
    spec = get_registry().find(venue_l)
    if spec is None or spec.id in {"mock", "mock_chain", "paper", "paper_chain"}:
        return None
    cfg, workspace, vault_passphrase = _resolve_account_for_venue(
        venue_l, config_like=config_like,
    )
    if timeout_s is not None:
        cfg = dict(cfg)
        cfg["timeout_s"] = timeout_s
        cfg["timeout_ms"] = int(timeout_s * 1000)
    try:
        conn = build_connector(
            cfg, workspace=workspace, vault_passphrase=vault_passphrase,
        )
        if timeout_s is not None:
            conn = _apply_connector_timeout(conn, timeout_s)
        return conn
    except Exception:
        return None


def _resolve_account_for_venue(
    venue: str,
    *,
    config_like: Any | None,
) -> tuple[dict[str, Any], Path | None, str | None]:
    """Find an operator-registered account row for ``venue``.

    Without this, the agent's ``market_data`` tool builds
    every connector with an empty cfg, which works for keyless venues
    (yahoo, ccxt-public) but raises "API key required" for credentialed
    data sources (Tushare, Polygon, Glassnode, Messari, …).

    Lookup is best-effort:
    1. If ``config_like`` carries a :class:`WorkspacePaths` (it does when
       it's a :class:`Config` instance), load ``accounts.yml`` and look
       for any row whose ``venue`` matches ``venue`` case-insensitively.
       If found, return that row's raw cfg + the workspace + the vault
       passphrase resolved from environment.
    2. Otherwise (or on any error), return a bare cfg so the existing
       code path is preserved.

    The workspace + vault_passphrase carry through to
    :func:`_data_source_creds`, which uses them to resolve any
    ``vault://`` reference declared on ``credentials.api_key``.

    Connectors that ship an env-var fallback (Tushare, Polygon — see
    :func:`nerya.connectors.data_sources._env_fallback_api_key`) will
    pick the env var up even if step 1 returns the bare cfg.
    """
    venue_l = (venue or "").strip().lower()
    bare: tuple[dict[str, Any], Path | None, str | None] = (
        {"venue": venue_l, "live": False},
        None,
        None,
    )
    if not venue_l or config_like is None:
        return bare
    paths = getattr(config_like, "paths", None)
    if paths is None:
        return bare
    try:
        from ...trading.accounts import load_accounts
    except Exception:
        return bare
    try:
        accounts = load_accounts(paths)
    except Exception:
        return bare
    for acc in accounts.values():
        if (getattr(acc, "venue", "") or "").strip().lower() != venue_l:
            continue
        cfg = dict(getattr(acc, "raw", {}) or {})
        cfg["venue"] = venue_l
        cfg.setdefault("live", False)
        workspace_root = getattr(paths, "root", None)
        if workspace_root is not None and not isinstance(workspace_root, Path):
            try:
                workspace_root = Path(workspace_root)
            except Exception:
                workspace_root = None
        passphrase = _vault_passphrase_from_env()
        return cfg, workspace_root, passphrase
    return bare


def _vault_passphrase_from_env() -> str | None:
    """Pull the vault passphrase from environment, if exposed.

    Mirrors the convention used elsewhere in Nerya (e.g. CLI / API
    layers): operators set ``NERYA_VAULT_PASSPHRASE`` in their shell
    so credentialed factories can decrypt vault refs without an
    interactive prompt. Returning ``None`` lets the factory raise its
    normal "vault locked" error if the operator stored a credential
    but didn't expose the passphrase.
    """
    import os as _os
    val = (_os.environ.get("NERYA_VAULT_PASSPHRASE") or "").strip()
    return val or None


def _count(args: dict[str, Any]) -> int:
    raw = args.get("count", args.get("limit", 96))
    try:
        return max(1, min(int(raw), 500))
    except (TypeError, ValueError):
        return 96


def _clean_envelope(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if rows and isinstance(rows[0], dict):
        env = rows[0].get("_envelope")
        if isinstance(env, dict):
            return env
    return None


def _row_timestamp(row: Any) -> Any:
    if isinstance(row, dict):
        return (
            row.get("timestamp")
            or row.get("ts")
            or row.get("time")
            or row.get("datetime")
            or row.get("date")
        )
    if isinstance(row, (list, tuple)) and row:
        return row[0]
    return None


def _iso_utc_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1_000_000_000_000:
            seconds /= 1000.0
        try:
            return (
                datetime.fromtimestamp(seconds, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") and "T" in text:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _candle_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "bars": 0,
            "first_timestamp": None,
            "last_timestamp": None,
            "first_timestamp_iso": None,
            "last_timestamp_iso": None,
        }
    first_ts = _row_timestamp(rows[0])
    last_ts = _row_timestamp(rows[-1])
    return {
        "bars": len(rows),
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "first_timestamp_iso": _iso_utc_timestamp(first_ts),
        "last_timestamp_iso": _iso_utc_timestamp(last_ts),
    }


def _context_text(market: str, interval: str, candles: list[dict[str, Any]], features: dict[str, Any]) -> str:
    if not candles:
        return (
            f"Market context for {market} ({interval}): no OHLCV rows available; "
            "treat technical indicators as unavailable."
        )
    last = candles[-1]
    macd = features.get("macd") if isinstance(features.get("macd"), dict) else {}
    bbands = features.get("bbands") if isinstance(features.get("bbands"), dict) else {}
    return (
        f"Market context for {market} ({interval}, rows={len(candles)}): "
        f"last_close={features.get('close')}, ret_1={features.get('ret_1')}, "
        f"rsi_14={features.get('rsi_14')}, ema_20={features.get('ema_20')}, "
        f"atr_14={features.get('atr_14')}, adx_14={features.get('adx_14')}, "
        f"macd_hist={macd.get('hist')}, bb_upper={bbands.get('upper')}, "
        f"bb_lower={bbands.get('lower')}, volume={last.get('volume')}, "
        f"indicator_backend={features.get('indicator_backend')}."
    )


def _market_candles(args: dict[str, Any], *, config_like: Any | None) -> dict[str, Any]:
    venue, market = _market_id(args, config_like=config_like)
    interval = str(args.get("interval") or "1m")
    count = _count(args)
    timeout_s = _market_timeout_s(args)
    if not _venue_has_public_market_feed(venue):
        missing = _credential_missing_payload(
            venue,
            market,
            config_like=config_like,
            capability="candles",
        )
        if missing is not None:
            return {
                **missing,
                "interval": interval,
                "count": 0,
                "coverage": _candle_coverage([]),
                "candles": [],
            }
    connector = _public_connector(venue, config_like=config_like, timeout_s=timeout_s)
    rows = fetch_candles(
        market,
        count=count,
        interval=interval,
        connector=connector,
        allow_mock=False,
        config_like=config_like,
    )
    envelope = _clean_envelope(rows) or (
        live_envelope(source=venue.lower(), venue=venue.lower()).as_dict()
        if rows
        else degraded_envelope(
            "candles",
            error="no_rows",
            venue=venue.lower(),
        ).as_dict()
    )
    coverage = _candle_coverage(rows)
    return {
        "venue": venue,
        "market": market,
        "interval": interval,
        "count": len(rows),
        "coverage": coverage,
        "first_timestamp_iso": coverage["first_timestamp_iso"],
        "last_timestamp_iso": coverage["last_timestamp_iso"],
        "candles": rows,
        "_envelope": envelope,
    }


def _market_ticker(args: dict[str, Any], *, config_like: Any | None) -> dict[str, Any]:
    venue, market = _market_id(args, config_like=config_like)
    timeout_s = _market_timeout_s(args)
    if not _venue_has_public_market_feed(venue):
        missing = _credential_missing_payload(
            venue,
            market,
            config_like=config_like,
            capability="ticker",
        )
        if missing is not None:
            return missing
    last_error = "venue_unavailable"
    connector = _public_connector(venue, config_like=config_like, timeout_s=timeout_s)
    if connector is not None:
        try:
            t = connector.get_ticker(market)
            return {
                "venue": getattr(t, "venue", venue),
                "market": market,
                "bid": t.bid,
                "ask": t.ask,
                "mid": t.mid,
                "last": t.last,
                "ts_ms": getattr(t, "ts_ms", 0),
                "_envelope": live_envelope(
                    source=venue.lower(), venue=venue.lower()
                ).as_dict(),
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    # Keyless public REST fallback so public venues still resolve a quote even
    # when an account-scoped connector cannot be built or errors out.
    snap = fetch_public_ticker(market, allow_mock=False, config_like=config_like)
    if snap and snap.get("price"):
        return {
            "venue": venue,
            "market": market,
            "bid": snap.get("bid"),
            "ask": snap.get("ask"),
            "mid": snap.get("mid") or snap.get("price"),
            "last": snap.get("last") or snap.get("price"),
            "ts_ms": snap.get("ts_ms") or 0,
            "_envelope": snap.get("_envelope")
            or live_envelope(source=venue.lower(), venue=venue.lower()).as_dict(),
        }
    return {
        "venue": venue,
        "market": market,
        "error": last_error,
        "_envelope": degraded_envelope(
            "ticker",
            error=last_error,
            venue=venue.lower(),
        ).as_dict(),
    }


def market_data_handler(call: ToolCall, *, config_like: Any | None = None) -> ToolResult:
    """Read-only compatibility surface for ``market_data.*`` calls."""

    args = dict(call.arguments or {})
    action = str(args.get("action") or "").strip()
    if not action:
        return _usage_error(call, "action is required.")
    if not (str(args.get("market") or "").strip() or str(args.get("symbol") or "").strip()):
        return _usage_error(
            call,
            "market or symbol is required; market_data does not infer a default market.",
        )

    if action in {"get_ticker", "get_mark_price"}:
        data = _market_ticker(args, config_like=config_like)
        if action == "get_mark_price":
            data = {
                **data,
                "mark_price": data.get("mid") or data.get("last"),
            }
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)

    if action == "get_candles":
        data = _market_candles(args, config_like=config_like)
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)

    if action in {"calculate_features", "summarize_market", "compress_context"}:
        data = _market_candles(args, config_like=config_like)
        candles = data.get("candles") if isinstance(data.get("candles"), list) else []
        features = compute_features(candles)
        data["features"] = features
        if action in {"summarize_market", "compress_context"}:
            data["context"] = _context_text(
                str(data.get("market") or ""),
                str(data.get("interval") or "1m"),
                candles,
                features,
            )
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)

    return _usage_error(call, f"unsupported market_data action: {action}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def connector_list_handler(call: ToolCall, *, config_like: Any | None = None) -> ToolResult:
    """List all connectors registered in the in-process provider spec."""

    args = call.arguments or {}
    kind = (args.get("kind") or "").strip().lower()
    query = (args.get("query") or "").strip().lower()
    include_source = bool(args.get("include_source", False))

    rows: list[dict[str, Any]] = []
    for spec in get_registry().list_specs():
        if kind and spec.kind.lower() != kind:
            continue
        if query:
            haystack = " ".join([
                spec.id, spec.label, spec.description,
                " ".join(spec.aliases),
            ]).lower()
            if query not in haystack:
                continue
        rows.append(
            _spec_to_dict(
                spec,
                include_source_path=include_source,
                config_like=config_like,
            )
        )

    rows.sort(key=lambda r: (r.get("kind", ""), r.get("id", "")))
    suggested_data_api_checks: list[dict[str, Any]] = []
    if query and not rows:
        suggested_data_api_checks = [
            {
                "op": "list",
                "provider": "wallet",
                "query": query,
            },
            {
                "op": "list",
                "provider": "onchainos",
                "query": query,
            },
        ]
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "count": len(rows),
            "query": query,
            "connectors": rows,
            **(
                {"suggested_data_api_checks": suggested_data_api_checks}
                if suggested_data_api_checks
                else {}
            ),
            "hint": (
                "If a venue / data source is in this list it is *already "
                "integrated* — wire the strategy to it directly. If it is "
                "missing, first check whether it is exposed by a "
                "provider-specific data_api surface via op='list' and "
                "op='schema'. Use concrete call shapes returned by those "
                "tool results instead of guessing provider routes. Only "
                "after connector and data_api surfaces are missing should "
                "you follow the coding skill (extending-nerya.md) "
                "to author a real Connector subclass under "
                "nerya/connectors/<vendor>.py + register it via "
                "_register_builtins. Do not mock the data and do not "
                "ship a temp script."
            ),
        },
    )


def connector_view_handler(call: ToolCall, *, config_like: Any | None = None) -> ToolResult:
    """Detail for a single provider, including (optionally) its source."""

    args = call.arguments or {}
    raw = (args.get("id") or "").strip().lower()
    if not raw:
        return _usage_error(call, "id is required (provider id or alias).")
    try:
        max_bytes = int(args.get("max_source_bytes", 24000))
    except (TypeError, ValueError):
        return _usage_error(call, "max_source_bytes must be an integer.")
    if max_bytes < 0:
        return _usage_error(call, "max_source_bytes must be >= 0.")

    spec = get_registry().find(raw)
    if spec is None:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "found": False,
                "id": raw,
                "hint": (
                    f"No provider matches {raw!r}. This venue is *not* "
                    "registered as an exchange Connector. Before authoring "
                    "new code, check data_api providers for long-tail data: "
                    "use op='list' and op='schema' on relevant providers, "
                    "then call only concrete actions returned by the catalog. "
                    "Only if those are missing, author a real Connector under "
                    "nerya/connectors/<vendor>.py and register it in "
                    "provider_spec._register_builtins. Read official docs "
                    "first; do not mock."
                ),
            },
        )

    info = _spec_to_dict(spec, include_source_path=True, config_like=config_like)
    info["found"] = True
    if _venue_has_public_market_feed(spec.id) or any(
        _venue_has_public_market_feed(alias) for alias in spec.aliases
    ):
        info["public_market_data"] = {
            "available": True,
            "requires_credentials": False,
            "note": (
                "Read-only OHLCV and ticker for this venue load from public "
                "endpoints with no API keys. Fetch them with market_data "
                "(get_candles / calculate_features) or ctx.market.candles(...). "
                "Credentials are only needed to place live orders — not to "
                "design or backtest a strategy."
            ),
        }
    include_source = bool(args.get("include_source", False))
    if not include_source and max_bytes > 0:
        info["source_omitted"] = True
        info["source_hint"] = (
            "Source omitted by default. For strategy authoring, use connector "
            "metadata plus data_api capability results and move to "
            "strategy_draft_proposal (then edit the staged proposal files); "
            "request include_source=true only when "
            "implementing or debugging connector code."
        )
        max_bytes = 0

    src_path = _factory_module_path(spec)
    if max_bytes > 0 and src_path is not None and src_path.exists():
        try:
            text = src_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = f"<failed to read {src_path}: {exc}>"
        if len(text) > max_bytes:
            info["source"] = text[:max_bytes]
            info["source_truncated"] = True
            info["source_total_bytes"] = len(text)
        else:
            info["source"] = text
            info["source_truncated"] = False
            info["source_total_bytes"] = len(text)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=info,
    )


__all__ = [
    "CONNECTOR_LIST_SCHEMA",
    "CONNECTOR_VIEW_SCHEMA",
    "connector_list_handler",
    "connector_view_handler",
]
