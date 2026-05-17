"""Default ``mcp_servers.yml`` stub + auto-create helper.

USER decision E-5 (locked) = ``seed_empty_config`` — on first agent
boot, if ``<workspace>/connectors/mcp_servers.yml`` does not exist,
the bootstrap writes this stub. The stub declares all 17 known
servers (3 zero-key open-tier, 4 free-key, 10 paid) but enables only
the 3 zero-key ones by default so a fresh install gets immediate value
without leaking any vault refs to the network.

The catalogue records the vetted default server set for this workspace.
See ``tmp/finance_services_integration/phase_e_free_alts.md`` for the
full justification. Every entry has a ``notes`` field calling out:

* what the server replaces (when it's a free alternative to a paid
  upstream service);
* what credential the operator needs to provision via
  ``nerya secrets put <name>`` and what scope to grant.

The stub lives as a Python string constant rather than a separate
``.yml`` template file so:

* the seed is versioned with the runtime code (no risk of the
  on-disk template drifting from the loader expectations);
* there's nothing to import / package — adding the stub doesn't
  require touching ``MANIFEST.in`` or wheel data files.
"""

from __future__ import annotations

from pathlib import Path

from ...core.atomic_write import atomic_write_text


SEED_HEADER = """\
# Nerya MCP connector catalogue.
#
# This file is auto-created on first agent boot by
# nerya.mcp.connectors.bootstrap.bootstrap_mcp_connectors().
#
# Edit it to enable additional servers or to adjust auth.
# Restart Nerya (or call connectors.bootstrap.reload_mcp_connectors())
# for changes to take effect.
#
# Three categories of server are declared below:
#
#   (1) ZERO-KEY OPEN-TIER  — enabled: true by default
#       sec_edgar / yahoo_finance / coingecko
#       Just work; no vault setup needed. (Earlier seeds shipped
#       finviz_free here but its uvx package was unstable; removed.)
#
#   (2) FREE-KEY            — enabled: false; needs a free signup
#       fred / alpha_vantage / fmp / polygon
#       Sign up at the linked URL, get a key, store it in the vault:
#         nerya secrets put mcp_<server>_api_key --kind bearer --scope mcp.read
#       Then flip enabled: true and restart.
#
#   (3) PAID-ONLY           — enabled: false; needs an institutional contract
#       pitchbook / chronograph / aiera / mtnewswire / daloopa /
#       morningstar / factset / sp_global / moodys / lseg
#       Same vault-store flow; URL is preserved so an operator with a
#       contract can switch them on without editing source.
#
# For every PAID entry there's usually a free-tier alternative listed
# above. See `notes:` on each entry for what each server replaces.
"""


DEFAULT_MCP_SERVERS_YML = SEED_HEADER + """
version: 1

servers:

  # ====== (1) ZERO-KEY OPEN-TIER — enabled by default ======

  - id: sec_edgar
    enabled: true
    namespace: edgar
    transport:
      kind: stdio
      command: ["uvx", "sec-edgar-mcp"]
      startup_timeout: 60
      read_timeout: 60
      env:
        # SEC fair-use policy requires a User-Agent identifier on EDGAR
        # API requests (not a credential — just identification). The
        # default below works for low volume; for any production use,
        # store your own identifier in the vault and promote to env_refs:
        #   nerya secrets put mcp_sec_edgar_user_agent --kind bearer \
        #     --scope mcp.read --value "Your Org (you@your.org)"
        # then in this file:
        #   env_refs: { SEC_EDGAR_USER_AGENT: vault://mcp_sec_edgar_user_agent }
        SEC_EDGAR_USER_AGENT: "Nerya MCP Agent (mcp-bridge@nerya.local)"
    auth:
      kind: none
    notes: >
      SEC EDGAR via stefanoamorelli/sec-edgar-mcp v1.0.8+ (edgartools-backed,
      AGPL-3.0). Stdio = no third-party hosting risk. Replaces upstream
      financial-services daloopa server. (Earlier seeds used a third-party
      hosted HTTP endpoint at secedgar.cyanheads.com which proved fragile.)

  - id: yahoo_finance
    enabled: true
    namespace: yahoo
    transport:
      kind: stdio
      command: ["uvx", "yahoo-finance-mcp"]
      startup_timeout: 45
      read_timeout: 60
    auth:
      kind: none
    # Overlap filter. Native YahooFinanceConnector
    # (registered as venue "yahoo" in nerya/connectors/provider_spec.py)
    # already exposes equity OHLC via the built-in market_data tool
    # (call as market_data(venue="yahoo", market="AAPL", interval="1d")).
    # Drop the MCP duplicate so the agent has exactly one path for OHLC.
    # The 8 unique fundamentals/holders/options/news tools below are kept.
    deny_tools:
      - get_historical_stock_prices
    notes: >
      yfinance — quotes / fundamentals / options / news.
      Replaces upstream morningstar / lseg / factset (general financials tier).
      OHLC tool (`get_historical_stock_prices`) is denied via deny_tools —
      use native `market_data(venue="yahoo", market=TICKER)` instead.

  - id: coingecko
    enabled: true
    namespace: coingecko
    transport:
      kind: http
      url: https://mcp.api.coingecko.com/mcp
      timeout_seconds: 30
      # CoinGecko speaks the modern MCP Streamable HTTP transport —
      # SSE-framed responses + an explicit initialize handshake. The
      # transport handles the SSE parsing and the Mcp-Session-Id replay
      # automatically; auto_initialize: true tells it to also run the
      # mandatory initialize handshake before any tools/list call.
      auto_initialize: true
    auth:
      kind: none
    notes: >
      CoinGecko official keyless MCP (Public Beta) — 76 tools across crypto
      spot pricing, DEX/onchain analytics, NFT trends, and category leaders.
      Subject to shared rate limits; for higher throughput, get a free Demo
      or Pro key at https://www.coingecko.com/en/api/pricing then switch
      url to https://mcp.pro-api.coingecko.com/mcp + bearer auth.
      Replaces the previous finviz_free entry (HTML-scraper-backed package
      that failed to start under uvx).

  # ====== (2) FREE-KEY — disabled until vault populated ======

  - id: fred
    enabled: false
    namespace: fred
    transport:
      kind: stdio
      command: ["uvx", "fred-mcp-server"]
      startup_timeout: 45
      env_refs:
        FRED_API_KEY: vault://mcp_fred_api_key
    auth:
      kind: none
    notes: >
      FRED economic data — free key from https://fred.stlouisfed.org/docs/api/api_key.html
      Replaces upstream moodys server (economic indicators tier).
      Provision: nerya secrets put mcp_fred_api_key --kind bearer --scope mcp.read

  - id: alpha_vantage
    enabled: false
    namespace: av
    transport:
      kind: http
      url: https://mcp.alphavantage.co/mcp
      timeout_seconds: 30
    auth:
      kind: bearer_static
      token_ref: vault://mcp_alpha_vantage_api_key
    notes: >
      Alpha Vantage — free key from https://alphavantage.co/support/#api-key
      Replaces upstream sp_global (Kensho kfinance) server.
      Provision: nerya secrets put mcp_alpha_vantage_api_key --kind bearer --scope mcp.read

  - id: fmp
    enabled: false
    namespace: fmp
    transport:
      kind: stdio
      command: ["uvx", "fmp-mcp"]
      startup_timeout: 45
      env_refs:
        FMP_API_KEY: vault://mcp_fmp_api_key
    auth:
      kind: none
    notes: >
      Financial Modeling Prep — free tier covers ~250 endpoints.
      Replaces upstream factset / sp_global (deep fundamentals tier).
      Provision: nerya secrets put mcp_fmp_api_key --kind bearer --scope mcp.read

  - id: polygon
    enabled: false
    namespace: poly
    transport:
      kind: stdio
      command: ["uvx", "mcp_polygon"]
      startup_timeout: 45
      env_refs:
        POLYGON_API_KEY: vault://mcp_polygon_api_key
    auth:
      kind: none
    notes: >
      Polygon.io — free tier covers basic market data.
      Provision: nerya secrets put mcp_polygon_api_key --kind bearer --scope mcp.read

  # ====== (3) PAID-ONLY — disabled, upstream URL preserved ======

  - id: pitchbook
    enabled: false
    namespace: pitchbook
    transport: { kind: http, url: https://premium.mcp.pitchbook.com/mcp }
    auth:
      kind: oauth_client_credentials
      client_id_ref: vault://mcp_pitchbook_client_id
      client_secret_ref: vault://mcp_pitchbook_client_secret
      token_url_ref: vault://mcp_pitchbook_token_url
    notes: >
      PitchBook — paid contract required. No good free alternative for
      private-company / VC fundraising data. Crunchbase API exists but
      is also paid.

  - id: chronograph
    enabled: false
    namespace: chronograph
    transport: { kind: http, url: https://ai.chronograph.pe/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_chronograph_token
    notes: >
      Chronograph — paid PE portfolio analytics. No free equivalent
      (too niche). Operator must contract directly.

  - id: aiera
    enabled: false
    namespace: aiera
    transport: { kind: http, url: https://mcp-pub.aiera.com/ }
    auth:
      kind: oauth_client_credentials
      client_id: "32kfd2pd43kfrhbkf7dcbt1v47"  # public OAuth client_id (per Aiera docs)
      client_secret_ref: vault://mcp_aiera_client_secret
      token_url: https://mcp-pub.aiera.com/oauth/token
    notes: >
      Aiera — earnings calls + transcripts. Aiera subscription required.
      The OAuth client_id is published in their docs; only client_secret
      is sensitive.

  - id: mtnewswire
    enabled: false
    namespace: mtnewswire
    transport: { kind: http, url: https://vast-mcp.blueskyapi.com/mtnewswires }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_mtnewswire_token
    notes: >
      MT Newswire — paid newswire. No good free alternative for
      institutional-grade financial newswires.

  - id: daloopa
    enabled: false
    namespace: daloopa
    transport: { kind: http, url: https://mcp.daloopa.com/server/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_daloopa_token
    notes: >
      Daloopa — paid SEC filings + parsed financials.
      The free alt is sec_edgar above (filing reader).

  - id: morningstar
    enabled: false
    namespace: morningstar
    transport: { kind: http, url: https://mcp.morningstar.com/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_morningstar_token
    notes: >
      Morningstar — paid fund / equity research.
      The free alt is yahoo_finance above.

  - id: factset
    enabled: false
    namespace: factset
    transport: { kind: http, url: https://mcp.factset.com/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_factset_token
    notes: >
      FactSet — paid institutional data terminal.
      The free alts are fmp + yahoo_finance above.

  - id: sp_global
    enabled: false
    namespace: spglobal
    transport: { kind: http, url: https://kfinance.kensho.com/integrations/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_sp_global_token
    notes: >
      S&P Global / Kensho — paid.
      The free alt is alpha_vantage above.

  - id: moodys
    enabled: false
    namespace: moodys
    transport: { kind: http, url: https://api.moodys.com/genai-ready-data/m1/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_moodys_token
    notes: >
      Moody's — paid economic data.
      The free alt is fred above.

  - id: lseg
    enabled: false
    namespace: lseg
    transport: { kind: http, url: https://api.analytics.lseg.com/lfa/mcp }
    auth:
      kind: bearer_static
      token_ref: vault://mcp_lseg_token
    notes: >
      LSEG (Refinitiv) — paid.
      The free alt is yahoo_finance above.
"""


def ensure_mcp_servers_config(path: Path) -> bool:
    """Write the seed stub at ``path`` if no file exists yet.

    Returns ``True`` if a new file was written, ``False`` if the file
    already existed (operator-owned, never overwritten).
    """

    p = Path(path)
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, DEFAULT_MCP_SERVERS_YML)
    return True


__all__ = [
    "DEFAULT_MCP_SERVERS_YML",
    "SEED_HEADER",
    "ensure_mcp_servers_config",
]
