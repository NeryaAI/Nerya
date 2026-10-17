# Resolve the strategy evidence path

Before authoring non-candle inputs or a new data provider, load
`Skill(skill="backtest", file="references/replay-capabilities.md")`.
Use its capability-based native/custom replay decision, provider proposal lane
and same-candidate recovery contract. Preserve the requested chain and strategy.

The strategy bundle must name its input sources, expected historical coverage,
execution model and unsupported capabilities in `strategy.md`. Include an
intentionally authored supported custom replay when the native engine cannot
represent those inputs; a disabled script plus a promise to add replay is not
completion. Verify SDK signatures with real contract types in bounded tests.
Keep domain methodology in these lazy references and workspace adapters, not
in always-on prompts or venue-specific agent-loop branches.

Custom script configuration is separate from native strategy.yml::backtest.
The latter accepts only documented BacktestConfig fields; `fee_bps` and
`slippage_bps` are scalar shorthand for the native venue maps, while explicit
`fee_bps_by_venue` / `slip_bps_by_venue` entries override them. Engine,
window_hours, execution_model, fee_source and assumed_slippage_bps are not
native settings.
Keep custom assumptions in params/custom script and its report. Use the
replay-only nerya.strategies.research facade from the custom replay reference,
not internal config/data imports or environment reads. Do not weaken validation.
