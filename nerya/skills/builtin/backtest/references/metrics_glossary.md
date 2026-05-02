# Metrics Glossary

The metrics file contains capital, risk, risk-adjusted, trade, cost, drawdown,
missed-profit, capture, and metadata fields.

- `total_return_pct`: `(final_equity - initial_capital) / initial_capital * 100`.
- `annualized_return_pct`: CAGR over the replay window.
- `benchmark_buy_hold_return_pct`: equal-weight buy-and-hold over configured markets.
- `alpha_vs_benchmark_pct`: strategy return minus benchmark return.
- `max_drawdown_pct/usd`: largest peak-to-trough equity decline.
- `max_drawdown_duration_days`: longest drawdown episode duration.
- `volatility_annualized_pct`: standard deviation of daily returns annualized.
- `sharpe_ratio`: average excess return divided by return stdev.
- `sortino_ratio`: average excess return divided by downside stdev.
- `calmar_ratio`: annualized return divided by max drawdown.
- `total_trades`, `win_trades`, `loss_trades`, `win_rate_pct`: closed-trade counts.
- `avg_win_pct`, `avg_loss_pct`, `profit_factor`, `expectancy_pct`: trade expectancy.
- `avg_trade_duration_hours`, `max_consecutive_wins/losses`: execution profile.
- `total_fees_usd`, `total_slippage_usd`, `exposure_pct`, `turnover_ratio`: cost and exposure.
- `drawdown_episodes`: drawdowns above the configured threshold.
- `missed_profit_episodes`: benchmark rally windows where strategy lagged.
- `total_missed_profit_pct`: sum of missed-profit episode deltas.
- `upside_capture_ratio`, `downside_capture_ratio`, `capture_asymmetry`: benchmark capture behavior.
- `backtest_days`, `bars_total`, `bars_traded`, `markets`, `tf`, `start_utc`, `end_utc`, `engine_version`, `per_market`: run metadata.

