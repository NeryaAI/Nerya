# 26 - Seventh Pass Security Approval Data Connector Gaps

## Status (2026-04-25)

Section status:

1. **Prompt firewall** — PARTIALLY COMPLETED. `Nerya/nerya/security/prompt_injection.py` provides the static regex; `Nerya/nerya/agent/prompt_firewall.py` integrates into the kernel pipeline. Severity-aware policy engine → Plan 26 P1.
2. **Redaction** — PARTIALLY COMPLETED. `Nerya/nerya/core/redaction.py` covers regex-based redaction; structured-secret provenance → Plan 26 P1.
3. **Vault / key handling** — PARTIALLY COMPLETED. `Nerya/nerya/security/vault.py` covers encryption + integrity. Rotation / non-default passphrase preflight → Plan 20 P1.
4. **Approval semantics** — COMPLETED foundationally. Skill manifests carry `approval_gate`; kernel routes through `Nerya/nerya/harness/tool_runner.py`; streaming events publish `approval.request`.
5. **Risk-gate assumptions** — COMPLETED. Manifest-level `risk_gate` (`required` / `not_required`) enforced in `Nerya/nerya/skills/runtime.py`.
6. **Data-provider hardcoding** — PARTIALLY COMPLETED. Provider catalog at `Nerya/nerya/llm/providers.py` + `Nerya/nerya/llm/model_catalog.py`; runtime override via `Nerya/nerya/llm/credential_pool.py`.
7. **Wallet / connectors** — PARTIALLY COMPLETED. `Nerya/nerya/wallet/` + `Nerya/nerya/connectors/` cover the basics; OAuth flows → Plan 30/31.
8. **OAuth / provider auth** — COMPLETED 2026-04-25 (scaffolding).
   - `Nerya/nerya/security/provider_auth.py` (~390 lines): `ProviderAuthRecord`, `ProviderAuthStore`, `ProviderAuthManager`, `ProviderConfig`, structured `NeedsReauth` errors, vault-backed token persistence, refresh callback hook.
   - `Nerya/nerya/api/routes_provider_auth.py`: `GET/POST /security/provider_auth/{list,status,register,revoke,refresh,reauth}` + `register_default_configs()` for openai/anthropic/google/okx_os/mcp_server.
   - `Nerya/nerya/api/local_server.py:16,42` registers the new module so the routes ship by default.
   - `Nerya/nerya/core/paths.py:152-156` adds `paths.security` + `paths.provider_auth`.
   - Tests: `Nerya/tests/test_provider_auth.py` (23 cases — register, revoke, refresh callback success/failure, vault redaction, route registration, reauth payload, default configs).
   - Live OAuth flows (Google device-code, MCP OAuth) plug in via `ProviderConfig.refresh_fn`; the scaffolding never makes network calls itself.
9. **Sandbox / security-tool parity** — PARTIALLY COMPLETED. `Nerya/nerya/skills/builtin/operator_skill/actions.py` enforces destructive-command refusal + workspace chroot; remote sandboxes → Plan 21 P2.

Status: PARTIALLY COMPLETED — every backend foundation exists; remaining work is policy-engine + OAuth + remote-sandbox tracked under Plans 20/26/30/31.

This addendum captures another set of gaps that were not explicit enough in `00-25`: prompt firewall/redaction, vault/key handling, approval semantics, risk-gate assumptions, data-provider hardcoding, wallet/connectors, OAuth/provider auth, and sandbox/security-tool parity with Hermes.

## 1. Prompt Firewall Is A Static Regex List, Not A Policy Engine

### Nerya Evidence

- `nerya/security/prompt_injection.py:32` defines `_SUSPICIOUS_PATTERNS` as a Python list of regexes.
- `nerya/security/prompt_injection.py:34` to `nerya/security/prompt_injection.py:36` hardcodes classic instruction override patterns.
- `nerya/security/prompt_injection.py:38` to `nerya/security/prompt_injection.py:40` hardcodes risk/approval bypass phrases.
- `nerya/security/prompt_injection.py:42` to `nerya/security/prompt_injection.py:44` hardcodes live-trading phrases.
- `nerya/security/prompt_injection.py:46` to `nerya/security/prompt_injection.py:47` hardcodes secret-exfiltration phrases.
- `nerya/security/prompt_injection.py:49` to `nerya/security/prompt_injection.py:54` hardcodes limit tampering, direct-order injection, and jailbreak phrases.
- `nerya/security/prompt_injection.py:58` to `nerya/security/prompt_injection.py:64` only returns matching regex patterns.

### Hermes Evidence

- `hermes-agent/tools/approval.py:715` to `hermes-agent/tools/approval.py:723` combines multiple command guards into one approval decision.
- `hermes-agent/tools/tirith_security.py:615` onward runs an external security scanner for commands, with fail-open/fail-closed behavior.
- `hermes-agent/tools/web_tools.py:1185` to `hermes-agent/tools/web_tools.py:1196` explicitly blocks URLs containing embedded secrets before fetching.

### Required Alignment

- Replace one static regex list with a policy engine that has severity, source, action, match span, false-positive handling, allowlist/denylist, and per-channel policy.
- Split “untrusted content should be fenced” from “operator request should be refused”; today a user legitimately asking about injection can trip global rules.
- Add prompt-firewall tests for multilingual attacks, encoded secrets, tool-output injection, HTML/Markdown/attachment injection, and benign security questions.
- Add policy provenance so the dashboard can show which rule blocked a turn and how to tune it.

## 2. Redaction Is Defensive But Too Narrow And Pattern-Based

### Nerya Evidence

- `nerya/core/redaction.py:18` to `nerya/core/redaction.py:29` hardcodes regex patterns for Ethereum keys, AWS keys, generic long strings, Telegram bot tokens, and OpenAI-style keys.
- `nerya/core/redaction.py:31` to `nerya/core/redaction.py:37` hardcodes key-name hints like `api_key`, `private_key`, `token`, `authorization`, and `auth`.
- `nerya/core/redaction.py:40` to `nerya/core/redaction.py:46` applies blanket substitution without structured secret provenance.

### Missing Hermes-Like Behavior

Hermes has broader credential handling, URL secret blocking, environment/credential-file handling, and auth-store logic. Nerya redaction is a useful safety net but not yet a complete secret lifecycle.

### Required Alignment

- Add structured secret provenance: where a secret came from, which actor resolved it, and which tool received it.
- Add URL, file path, command-line, environment, JSON, YAML, and attachment redaction passes.
- Add false-positive-safe previews and hash fingerprints consistently across logs, prompts, tool traces, and API errors.
- Add tests for percent-encoded secrets, multiline PEM/private keys, JWTs, OAuth tokens, wallet mnemonics, Solana base58 keys, and provider-specific key formats.

## 3. Vault Has Unsafe Defaults And Weak Operational Semantics

### Nerya Evidence

- `nerya/security/secrets.py:47` to `nerya/security/secrets.py:49` opens the vault with env passphrase or the literal fallback `nerya-default-passphrase`.
- `nerya/security/secrets.py:61` to `nerya/security/secrets.py:66` silently returns empty cache on decrypt/load failure.
- `nerya/security/secrets.py:97` to `nerya/security/secrets.py:107` writes secrets but does not record rotation/revocation metadata.
- `nerya/security/secrets.py:118` to `nerya/security/secrets.py:123` only checks a required scope string.
- `nerya/security/encryption.py:41` marks fallback crypto as deterministic and not secret in test envs.
- `nerya/security/encryption.py:53` to `nerya/security/encryption.py:56` can write `XOR:` fallback ciphertext when AESGCM is unavailable.

### Hermes Evidence

- `hermes-agent/hermes_cli/auth.py:4` to `hermes-agent/hermes_cli/auth.py:12` describes OAuth provider auth, auth.json, cross-process locking, runtime credential resolution, and token refresh.
- `hermes-agent/hermes_cli/auth.py:651` references lock timeout for the auth store.
- `hermes-agent/hermes_cli/auth.py:751` to `hermes-agent/hermes_cli/auth.py:775` manages credential pools.
- `hermes-agent/hermes_cli/auth.py:838` to `hermes-agent/hermes_cli/auth.py:839` gates auto-discovery of external credentials.

### Required Alignment

- Refuse non-test vault use with the default passphrase.
- Refuse XOR fallback outside explicit unit-test mode.
- Surface decrypt failures as health/security errors instead of silently returning an empty vault.
- Add cross-process vault locking, rotation history, revocation, last-used audit, actor scopes, and per-secret allowed tool/action lists.
- Add OAuth/token-store support for model providers and gateways instead of only vault refs/env secrets.

## 4. Approval Gate Is Trade-Only And Not Session-Interactive Enough

### Nerya Evidence

- `nerya/trading/approval.py:43` to `nerya/trading/approval.py:59` creates a `trade_intent` approval and appends a compact pending JSONL record.
- `nerya/trading/approval.py:61` to `nerya/trading/approval.py:71` approves/rejects by id and appends minimal rows.
- `nerya/trading/approval.py:73` to `nerya/trading/approval.py:82` expires pending approvals by calling `reject(row["id"], "expired")` during list.
- Approval records do not carry gateway callback identity, user identity, original message id, proposed diff, command string, or per-session approval mode.

### Hermes Evidence

- `hermes-agent/tools/approval.py:1` to `hermes-agent/tools/approval.py:9` is a single source for dangerous command detection, per-session state, prompting, smart approval, and permanent allowlist persistence.
- `hermes-agent/tools/approval.py:23` to `hermes-agent/tools/approval.py:55` uses context-local session identity for concurrent gateway sessions.
- `hermes-agent/tools/approval.py:76` to `hermes-agent/tools/approval.py:114` defines dangerous command patterns for terminal/file/system actions.
- `hermes-agent/tools/approval.py:645` to `hermes-agent/tools/approval.py:681` handles gateway/CLI approval choices and session/permanent approval.
- `hermes-agent/tools/approval.py:728` to `hermes-agent/tools/approval.py:757` handles YOLO/off/cron approval modes.

### Required Alignment

- Generalize approvals beyond trades: terminal command, file write/patch, gateway callback, config edit, skill install, MCP tool, wallet signing, service restart, and prompt patch.
- Add per-session and per-actor approval modes: ask, deny, yolo/session, yolo/tool, cron deny/approve.
- Add approval payload schemas and rich rendering for dashboard/gateway/ACP/TUI.
- Add callback-safe approval ids tied to session/user/message and expiring replay tokens.
- Add audit trail with approver, denial reason, source platform, and resulting action execution.

## 5. Risk Gate Is Trading-Specific And Encodes Several Simplifying Assumptions

### Nerya Evidence

- `nerya/trading/risk.py:36` defines a single `RiskGate` for trade intents.
- `nerya/trading/risk.py:56` to `nerya/trading/risk.py:59` checks kill switch.
- `nerya/trading/risk.py:75` to `nerya/trading/risk.py:81` checks runtime/account live flags.
- `nerya/trading/risk.py:93` to `nerya/trading/risk.py:97` checks strategy market allow-list.
- `nerya/trading/risk.py:99` to `nerya/trading/risk.py:105` estimates notional from size/price with simple rules.
- `nerya/trading/risk.py:147` to `nerya/trading/risk.py:153` dedupes by `strategy_id:market:side:rounded_notional`.
- `nerya/trading/risk.py:155` to `nerya/trading/risk.py:161` only escalates based on approval threshold.

### Missing Hermes-Like Behavior

Hermes risk/approval is not trading risk, but operator-agent safety covers filesystem, shell, network, sandbox, credentials, service lifecycle, and gateway callbacks. Nerya's risk model is still mostly order-risk, not general action-risk.

### Required Alignment

- Add a general `ActionRiskPolicy` for all tools/actions, separate from trading risk.
- Make dedupe keys include time bucket, order type, account, source message/turn, strategy version, and client order id.
- Add slippage/liquidity/orderbook freshness checks as first-class risk checks, not advisory text.
- Add wallet signing risk checks: spender, chain id, calldata decode, token allowance, receiver, gas, bridge, and MEV/slippage.
- Add risk-check explanations consumable by UI/gateway approval prompts.

## 6. Data Providers Still Contain Hardcoded Sources And Thin Parsers

### Nerya Evidence

- `nerya/data/news.py:28` to `nerya/data/news.py:35` hardcodes CoinDesk, Cointelegraph, and Bitcoin Magazine RSS URLs.
- `nerya/data/news.py:37` to `nerya/data/news.py:40` hardcodes regex ticker/item/tag parsing.
- `nerya/data/news.py:68` to `nerya/data/news.py:72` hardcodes ticker stopwords.
- `nerya/data/news.py:113` uses `UrllibHttp(rate_limit_per_sec=4.0)` and `nerya/data/news.py:119` uses timeout `15.0`.
- `nerya/data/social.py:25` hardcodes subreddits `CryptoCurrency`, `Bitcoin`, `ethereum`, and `solana`.
- `nerya/data/social.py:64` uses `UrllibHttp(rate_limit_per_sec=1.5)`.
- `nerya/data/defi.py:25` hardcodes DefiLlama base URL.
- `nerya/data/onchain_price.py:44` hardcodes Dexscreener token API template.
- `nerya/data/onchain_klines.py:124` to `nerya/data/onchain_klines.py:127` hardcodes GeckoTerminal URL shape and caps limit at 1000.

### Required Alignment

- Move sources, base URLs, rate limits, parser strategy, and fallback policy into data-provider manifests/config.
- Add provider health, cache metadata, last-success timestamp, source attribution, and content licensing/terms metadata.
- Add pluggable parsers instead of regex-only RSS extraction.
- Let skills select data providers by capability and freshness rather than fixed module globals.
- Add strict-mode tests that no mock or empty degraded data is mistaken for live alpha.

## 7. Wallet Provider Catalog Is Hardcoded And Partly Documentation-Driven

### Nerya Evidence

- `nerya/wallet/registry.py:33` defines a static `PROVIDERS` dict.
- `nerya/wallet/registry.py:34` to `nerya/wallet/registry.py:50` hardcodes self-custody labels, install hints, GOAT docs URL, and config keys.
- `nerya/wallet/registry.py:51` to `nerya/wallet/registry.py:68` hardcodes OKX OS docs/config/runtime.
- `nerya/wallet/registry.py:69` to `nerya/wallet/registry.py:103` hardcodes Bitget/Binance Node skill clone/install instructions.
- `nerya/wallet/registry.py:104` onward hardcodes Coinbase CDP install/auth instructions.
- `nerya/wallet/providers/okx_os.py:132` to `nerya/wallet/providers/okx_os.py:136` maps chain names through `_CHAIN_IDS` and rejects unsupported chains.
- `nerya/wallet/providers/okx_os.py:158` to `nerya/wallet/providers/okx_os.py:181` assumes decimals defaults and slippage math.
- `nerya/wallet/providers/okx_os.py:184` to `nerya/wallet/providers/okx_os.py:220` returns an unsigned transaction note rather than full signer/broadcast workflow.

### Required Alignment

- Move wallet provider metadata to provider manifests with versioned capability tests.
- Detect provider SDK/API version dynamically and validate install hints.
- Add chain registry with chain id, native token, RPC policy, gas model, explorer, and supported provider actions.
- Add transaction simulation/decode before approval.
- Split quote/swap/sign/broadcast/confirm into explicit staged actions with approval at the right boundary.

## 8. Exchange Provider Hot-Loading Needs Stronger Trust Boundaries

### Nerya Evidence

- `nerya/connectors/provider_spec.py:15` to `nerya/connectors/provider_spec.py:20` says workspace providers are hot-imported from `workspace/providers/<id>/provider.py`.
- `nerya/connectors/provider_spec.py:27` imports `importlib.util` and `sys` for dynamic imports.
- `nerya/connectors/provider_spec.py:91` to `nerya/connectors/provider_spec.py:95` lets newly registered specs replace an existing spec with the same id.
- `nerya/connectors/provider_spec.py:111` to `nerya/connectors/provider_spec.py:127` scans provider directories and loads `provider.py` files.
- `nerya/connectors/registry.py:84` defaults unknown/missing venue to `mock`.
- `nerya/connectors/registry.py:118` to `nerya/connectors/registry.py:130` silently returns `None` on vault/secret resolution errors.

### Required Alignment

- Sandbox or statically validate user-authored provider code before import.
- Require signed/approved provider manifests before a user provider can shadow builtins.
- Expose provider load errors and secret resolution errors instead of silent fallback to missing creds/mock.
- Add capability tests per provider before route selection uses it.
- Add rollback and quarantine for failing provider plugins.

## 9. Provider Auth Is Mostly Vault/Env, Not Hermes-Style Multi-Provider Login

### Nerya Evidence

- Nerya resolves provider keys mainly through tier config, vault refs, or env vars.
- `nerya/security/secrets.py:118` to `nerya/security/secrets.py:123` enforces only a simple required scope.
- `nerya/connectors/registry.py:100` to `nerya/connectors/registry.py:108` resolves exchange secrets from vault refs.

### Hermes Evidence

- `hermes-agent/hermes_cli/auth.py:93` onward defines provider configs for multiple inference providers.
- `hermes-agent/hermes_cli/auth.py:751` to `hermes-agent/hermes_cli/auth.py:775` manages credential pools.
- `hermes-agent/hermes_cli/auth.py:971` onward describes active provider resolution and fallback order.
- `hermes-agent/agent/anthropic_adapter.py` contains OAuth credential refresh and Claude Code credential detection paths.

### Required Alignment

- Add provider auth manager for LLM/gateway/data/wallet/exchange credentials.
- Support OAuth/device flows where providers require them.
- Add credential pool selection, suppression, active provider switching, refresh, expiry, and health checks.
- Separate “configured secret exists” from “runtime credential is valid right now”.

## 10. Sandbox And Environment Security Are Still Not Comparable To Hermes

### Nerya Evidence

- Nerya has skill runtime/tool runner, but no Hermes-like general terminal environment abstraction in the inspected surfaces.
- `nerya/harness/tool_runner.py` controls timeout/retry/budget, but does not provide OS sandboxing, container execution, resource isolation, or command security scanning.

### Hermes Evidence

- `hermes-agent/tools/environments/docker.py:236` onward implements hardened Docker execution with resource limits and persistence.
- `hermes-agent/tools/environments/docker.py:150` to `hermes-agent/tools/environments/docker.py:159` documents dropped capabilities, no privilege escalation, PID limits, and tmpfs limits.
- `hermes-agent/tools/environments/docker.py:362` to `hermes-agent/tools/environments/docker.py:410` handles credential/skill/cache mounts.
- `hermes-agent/tools/approval.py:715` to `hermes-agent/tools/approval.py:723` combines pre-exec security checks before command execution.

### Required Alignment

- Add environment backends: local restricted, Docker, remote SSH, modal/daytona-style remote, and no-shell mode.
- Add command security scanning, approval, and audit before any terminal/shell/file mutation tool exists.
- Add resource limits, env allowlist/blocklist, credential mounts, workspace mount policy, and cleanup.
- Add tool-result storage for large outputs and sandbox files.

## 11. Observability Exists As Journals, But Not As Queryable Operator Telemetry

### Nerya Evidence

- Nerya writes JSONL journals for approvals, LLM, security, errors, and agent turns in several modules.
- `nerya/security/audit.py:12` to `nerya/security/audit.py:16` appends redacted audit payloads.
- `scripts/run_truth_gate.sh:21` onward defines truth-gate test groups, but this is a test runner rather than a live observability system.

### Hermes Evidence

- Hermes stores sessions in SQLite with search and token accounting.
- Hermes TUI/gateway surfaces stream tool progress, approvals, and session state live.
- Hermes has broad gateway regression tests for platform-specific edge cases.

### Required Alignment

- Add structured event schema across agent, tools, gateway, approvals, data, wallet, connectors, and LLM.
- Add query API for events by actor/session/turn/tool/platform/approval/provider.
- Add metrics: latency, retries, rate limits, token/cost, data freshness, provider health, approval wait time, gateway delivery success, and interrupt rate.
- Add dashboard pages for live events and historical traces.

## 12. Missing Negative/Adversarial Test Matrix

### Required Tests To Add

- Prompt injection: benign mention vs hostile instruction, multilingual, encoded, attachment, quoted reply, tool output.
- Redaction: provider keys, OAuth tokens, wallet keys, mnemonics, PEM blocks, JWT, Telegram/Discord tokens, URL-embedded secrets.
- Vault: default passphrase refused, XOR fallback refused, bad passphrase surfaces error, rotation/revocation works.
- Approval: concurrent sessions cannot approve each other, expired callback rejected, denial prevents replay, cron/no-user mode denies dangerous actions.
- Risk: duplicate false positives/negatives, stale data, missing price, cross-account exposure, wallet calldata decode.
- Data providers: hardcoded sources disabled, provider failure, mock not authorized, stale cache not treated live.
- Connectors: user provider shadowing builtin blocked unless approved/signed, import errors quarantined, secret resolution failure surfaced.
- Sandbox: command injection, env leak, oversized output, long-running process, cleanup after interrupt.

## Highest-Impact New Work Items

1. Harden vault defaults immediately: no default passphrase or XOR fallback outside tests.
2. Generalize approval/risk from trading-only to all actions/tools.
3. Convert prompt firewall/redaction to versioned security policy with tests.
4. Move data-provider and wallet-provider metadata to manifests with truth tests.
5. Add provider auth manager with OAuth/credential pools/refresh.
6. Add sandbox/environment abstraction before adding general coding/terminal tools.
7. Add live observability/event query layer across all runtime subsystems.