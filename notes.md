# Notes: Nerya 2026-04-24 Refresh

## Re-verified closed blockers

- TypeScript SDK now targets `127.0.0.1:8787` and the current `/trading/*` and `/strategy/*` HTTP routes.
- Python SDK examples no longer fail with `ModuleNotFoundError`; `_bootstrap.py` makes repo-root execution work.
- `docs/script-system.md` now matches the narrow approved-script sandbox model.
- `evolution_skill` now exposes `generate_skill_scaffold`, and `script_generator.py` no longer emits `Client.local()`.
- Trigger and schedule CRUD are real in both API and dashboard.
- `TopHeader` no longer ships the old cosmetic global strategy selector.
- `LlmOpsPanel` now documents the real `workspace/llm/provider_routing.json` path.
- `routes_market.py` now derives public venues from the connector registry instead of a fixed venue list.

## Current blockers

- Documentation truth drift:
  - `docs/trading-sdk.md` still says `POST /trading/intent`
  - `nerya/skills/builtin/onchain_skill/skill.yml` still says "Uses mock chain by default"
  - `docs/reference-capability-map.md` still overstates Hermes cron parity and the onchain skill surface
  - `docs/llm-gateway.md` still mixes internal SDK and public SDK surfaces
- Public SDK fragmentation:
  - Python public SDK, TypeScript SDK, internal in-process SDK, and HTTP API still do not expose one canonical public surface
- Wallet/on-chain partials:
  - `self_custody.quote()` and `self_custody.swap()` are still stub/partial
  - TS wallet template still returns stub outputs
- Harmful hardcoding:
  - keyword-driven escalation in `nerya/core/config.py`
  - static venue union in `dashboard/lib/settings.ts`
  - demo/bootstrap worldview in `nerya/workspace/manager.py`
- Structural parity gaps:
  - Hermes cron/session parity remains open
  - Claude Code-style verification/session parity remains open

## Important nuance

- The runtime kernel question is closed: Nerya is real.
- The remaining work is now mainly truthfulness, contract alignment, and execution-path completeness.
- Hermes parity and Claude Code-inspired parity are not the same thing as production readiness; they should be tracked separately.

## Verification

- `python -m pytest tests/test_trigger_sdk.py tests/test_script_context.py tests/test_strategy_driver_schema.py tests/test_strategy_skill.py tests/test_strategy_lifecycle_phase7.py tests/test_llm_ops_surfaces.py tests/test_subagent_runtime_phase3.py tests/test_indicators_talib.py tests/test_features_indicator_fusion.py tests/test_agent_loop.py tests/test_scenario_replay.py tests/test_attribution_phase8.py tests/test_self_improvement_evidence.py tests/test_certification_gates.py tests/test_trading_sdk.py tests/test_direct_order_sdk_risk_gate.py tests/test_strategy_version_compare.py tests/test_skill_scaffold.py tests/test_evolution_scaffold_phase3.py -q`
  - `143 passed, 1 skipped in 264.34s`
- `python -m pytest tests/test_sdk_smoke.py tests/test_production_gate_phase6.py -q`
  - `30 passed in 3.10s`
- `npx tsc --noEmit` in `dashboard`
  - passed
- `npx tsc --noEmit` in `sdk/typescript`
  - passed
- `python sdk/python/examples/direct_order_strategy.py`
  - boot path fixed; current runtime rejects because the active workspace lacks strategy `btc_momentum`