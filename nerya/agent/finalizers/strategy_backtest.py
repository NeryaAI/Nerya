"""Strategy backtest final-answer formatting."""

from __future__ import annotations

from typing import Any


def _parse_display_number(value: Any) -> float | None:
    """Parse a metrics_display string (``'0.0000%'`` / ``'-5.90%'`` / ``'1,234'``)."""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "none", "nan", "-"}:
        return None
    text = text.replace("%", "").replace(",", "").replace("$", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _verdict_plain(verdict: str) -> str:
    """Plain-language reading of a backtest verdict code."""

    code = str(verdict or "").strip().upper()
    return {
        "PASS": "通过",
        "WARN": "可用，但有需要注意的地方",
        "FAIL": "不通过",
    }.get(code, str(verdict or "").strip())


def _interpret_backtest_metrics(display: dict[str, Any]) -> list[str]:
    """Turn headline backtest metrics into plain-language good/bad bullets."""

    if not isinstance(display, dict) or not display:
        return []

    def disp(key: str) -> str:
        return str(display.get(key) or "").strip()

    total_return = _parse_display_number(display.get("total_return_pct"))
    alpha = _parse_display_number(display.get("alpha_vs_benchmark_pct"))
    max_dd = _parse_display_number(display.get("max_drawdown_pct"))
    trades = _parse_display_number(display.get("total_trades"))
    profit_factor = _parse_display_number(display.get("profit_factor"))
    sharpe = _parse_display_number(display.get("sharpe_ratio"))
    has_trades = trades is None or trades > 0

    lines: list[str] = []

    if trades is not None:
        if trades <= 0:
            lines.append(
                f"成交 {disp('total_trades')} 笔：整段回测几乎没有真正下单，"
                "所以下面的收益/回撤数字参考价值很低——多半是信号太少没触发，"
                "而不是策略本身好或坏。"
            )
        elif trades < 10:
            lines.append(
                f"成交 {disp('total_trades')} 笔：样本太少，统计意义不足，"
                "结论还不稳；建议拉长回测区间或放宽信号条件后再看。"
            )
        else:
            lines.append(f"成交 {disp('total_trades')} 笔：样本量基本够用。")

    if total_return is not None:
        if total_return > 0.5:
            takeaway = "整体是赚钱的"
        elif total_return < -0.5:
            takeaway = "整体是亏钱的"
        else:
            takeaway = "基本不赚不亏"
        lines.append(f"总收益 {disp('total_return_pct')}：{takeaway}。")

    bench_disp = disp("benchmark_buy_hold_return_pct")
    if bench_disp:
        tail = ""
        if alpha is not None:
            if alpha > 0.5:
                tail = f"，策略比直接买入持有多赚约 {disp('alpha_vs_benchmark_pct')}（跑赢大盘）"
            elif alpha < -0.5:
                tail = f"，策略比直接买入持有少赚约 {disp('alpha_vs_benchmark_pct')}（跑输大盘）"
            else:
                tail = "，和直接买入持有差不多"
        lines.append(f"同期“买入持有”基准 {bench_disp}{tail}。")

    if max_dd is not None:
        abs_dd = abs(max_dd)
        if abs_dd < 1e-9:
            risk = "期间几乎没有回撤，但这通常也是因为交易太少"
        elif abs_dd <= 10:
            risk = "回撤较小，风险控制不错"
        elif abs_dd <= 25:
            risk = "回撤中等，属于可接受范围"
        else:
            risk = "回撤偏大，需要重点关注风险"
        lines.append(f"最大回撤 {disp('max_drawdown_pct')}：{risk}。")

    if has_trades and disp("win_rate_pct"):
        lines.append(f"胜率 {disp('win_rate_pct')}。")
    if has_trades and profit_factor is not None:
        if profit_factor >= 1.5:
            pf = "盈亏比健康（大于 1.5）"
        elif profit_factor >= 1.0:
            pf = "勉强盈利（略大于 1）"
        else:
            pf = "亏多赚少（小于 1）"
        lines.append(f"盈亏比 {disp('profit_factor')}：{pf}。")
    if has_trades and sharpe is not None:
        if sharpe >= 1.0:
            sh = "风险调整后的收益不错（大于等于 1）"
        elif sharpe >= 0:
            sh = "风险调整后的收益偏弱"
        else:
            sh = "风险调整后是负的"
        lines.append(f"夏普比率 {disp('sharpe_ratio')}：{sh}。")
    if disp("exposure_pct"):
        lines.append(f"仓位暴露 {disp('exposure_pct')}（资金真正在场内的时间占比）。")
    return lines


def _requested_english_final(user_text: str | None) -> bool:
    text = str(user_text or "").lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "final answer language: english",
            "answer language: english",
            "output language: english",
            "respond in english",
            "answer in english",
            "english only",
        )
    )


def _verdict_plain_en(verdict: str) -> str:
    code = str(verdict or "").strip().upper()
    return {
        "PASS": "passed",
        "WARN": "usable, but needs attention",
        "FAIL": "failed",
    }.get(code, str(verdict or "").strip())


def _interpret_backtest_metrics_en(display: dict[str, Any]) -> list[str]:
    if not isinstance(display, dict) or not display:
        return []

    def disp(key: str) -> str:
        return str(display.get(key) or "").strip()

    total_return = _parse_display_number(display.get("total_return_pct"))
    alpha = _parse_display_number(display.get("alpha_vs_benchmark_pct"))
    max_dd = _parse_display_number(display.get("max_drawdown_pct"))
    trades = _parse_display_number(display.get("total_trades"))
    profit_factor = _parse_display_number(display.get("profit_factor"))
    sharpe = _parse_display_number(display.get("sharpe_ratio"))
    has_trades = trades is None or trades > 0

    lines: list[str] = []
    if trades is not None:
        if trades <= 0:
            lines.append(
                f"Trades {disp('total_trades')}: almost no orders were filled, "
                "so return and drawdown metrics have low evidential value."
            )
        elif trades < 10:
            lines.append(
                f"Trades {disp('total_trades')}: the sample is small; extend "
                "history or loosen signals before relying on the statistics."
            )
        else:
            lines.append(f"Trades {disp('total_trades')}: the sample is usable.")

    if total_return is not None:
        if total_return > 0.5:
            takeaway = "profitable over the loaded window"
        elif total_return < -0.5:
            takeaway = "loss-making over the loaded window"
        else:
            takeaway = "roughly flat"
        lines.append(f"Total return {disp('total_return_pct')}: {takeaway}.")

    bench_disp = disp("benchmark_buy_hold_return_pct")
    if bench_disp:
        tail = ""
        if alpha is not None:
            if alpha > 0.5:
                tail = (
                    f"; the strategy outperformed buy-and-hold by about "
                    f"{disp('alpha_vs_benchmark_pct')}"
                )
            elif alpha < -0.5:
                tail = (
                    f"; the strategy underperformed buy-and-hold by about "
                    f"{disp('alpha_vs_benchmark_pct')}"
                )
            else:
                tail = "; roughly in line with buy-and-hold"
        lines.append(f"Buy-and-hold benchmark {bench_disp}{tail}.")

    if max_dd is not None:
        abs_dd = abs(max_dd)
        if abs_dd < 1e-9:
            risk = "almost no drawdown, often because exposure was low"
        elif abs_dd <= 10:
            risk = "low drawdown"
        elif abs_dd <= 25:
            risk = "moderate drawdown"
        else:
            risk = "large drawdown; risk needs attention"
        lines.append(f"Max drawdown {disp('max_drawdown_pct')}: {risk}.")

    if has_trades and disp("win_rate_pct"):
        lines.append(f"Win rate {disp('win_rate_pct')}.")
    if has_trades and profit_factor is not None:
        if profit_factor >= 1.5:
            pf = "healthy"
        elif profit_factor >= 1.0:
            pf = "barely above breakeven"
        else:
            pf = "below breakeven"
        lines.append(f"Profit factor {disp('profit_factor')}: {pf}.")
    if has_trades and sharpe is not None:
        if sharpe >= 1.0:
            sh = "solid risk-adjusted return"
        elif sharpe >= 0:
            sh = "weak risk-adjusted return"
        else:
            sh = "negative risk-adjusted return"
        lines.append(f"Sharpe ratio {disp('sharpe_ratio')}: {sh}.")
    if disp("exposure_pct"):
        lines.append(f"Exposure {disp('exposure_pct')}: capital was active for this share of the window.")
    return lines


def _build_strategy_backtest_done_final_text_en(items: list[dict[str, Any]]) -> str:
    has_data_gap = any(item.get("completion_kind") == "data_gap" for item in items)
    has_fail_verdict = any(
        str(item.get("verdict") or "").strip().upper() == "FAIL"
        for item in items
    )
    if has_data_gap:
        lines = [
            "The strategy proposal was created and a real-data backtest was attempted, "
            "but there is not enough historical market data to complete the standard replay.",
            "The strategy is not live and has not been promoted/applied.",
            "",
        ]
    elif has_fail_verdict:
        lines = [
            "The strategy proposal was created and the real-data backtest is complete, "
            "but the verdict is FAIL.",
            "The strategy is not live; fix the issue and rerun validation before approve/promote.",
            "",
        ]
    else:
        lines = [
            "The strategy proposal has been created and the backtest is complete. "
            "It is not live yet; review it before approve/promote.",
            "",
        ]

    for item in items:
        strategy_id = str(item.get("strategy_id") or "").strip()
        proposal_id = str(item.get("proposal_id") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        head_bits: list[str] = []
        if strategy_id:
            head_bits.append(f"strategy {strategy_id}")
        if proposal_id:
            head_bits.append(f"proposal {proposal_id}")
        if verdict:
            head_bits.append(f"backtest verdict {verdict} ({_verdict_plain_en(verdict)})")
        if head_bits:
            lines.append("- " + ", ".join(head_bits) + ".")

        if item.get("completion_kind") == "data_gap":
            coverage = str(item.get("coverage_message") or "").strip()
            if coverage:
                lines.append(f"  Data gap: {coverage}")
            next_action = str(item.get("next_required_action_message") or "").strip()
            if next_action:
                lines.append(f"  Note: {next_action}")
            continue

        display = item.get("metrics_display")
        for bullet in _interpret_backtest_metrics_en(
            display if isinstance(display, dict) else {}
        ):
            lines.append(f"  - {bullet}")

        coverage = str(item.get("coverage_message") or "").strip()
        if coverage:
            lines.append(f"  - Coverage: {coverage}")
        report_path = str(item.get("report_path") or "").strip()
        if report_path:
            lines.append(f"  Full chart and trade log: {_markdown_code_span(report_path)}")
        review_gate = item.get("review_gate")
        if (
            isinstance(review_gate, dict)
            and review_gate.get("paper_review_allowed") is not None
        ):
            allowed = bool(review_gate.get("paper_review_allowed"))
            lines.append(
                "  Paper review: " + ("allowed." if allowed else "not recommended yet; add more evidence first.")
            )

    lines.append("")
    if has_data_gap:
        lines.append(
            "Next step: configure a real historical data source for this market, "
            "or choose a market with existing durable history, then rerun the backtest."
        )
    elif has_fail_verdict:
        lines.append(
            "Next step: inspect the report, adjust parameters or missing assumptions, "
            "and rerun before approving promotion."
        )
    else:
        lines.append(
            "Next step: review the signal triggers, position sizing, and account/data binding; "
            "approve/promote only when they match expectations."
        )
    return "\n".join(lines)


def _build_strategy_backtest_done_final_text(
    items: list[dict[str, Any]],
    *,
    user_text: str | None = None,
) -> str:
    if _requested_english_final(user_text):
        return _build_strategy_backtest_done_final_text_en(items)
    has_data_gap = any(item.get("completion_kind") == "data_gap" for item in items)
    has_fail_verdict = any(
        str(item.get("verdict") or "").strip().upper() == "FAIL"
        for item in items
    )
    if has_data_gap:
        lines = [
            "策略提案已经创建，也尝试跑了真实回测，但目前缺少足够的历史行情数据，"
            "没办法完成标准回测。",
            "策略还没有上线（没有 promote/apply 到 live workspace）。",
            "",
        ]
    elif has_fail_verdict:
        lines = [
            "策略提案已经创建并跑完了真实回测，但回测结论是 FAIL（不通过）。",
            "策略还没有上线，需要先排查原因、调参后重新回测，确认通过前不要 approve/promote。",
            "",
        ]
    else:
        lines = [
            "策略提案已经创建并跑完了回测。结果可以参考，但还没有上线——"
            "需要你先看一下再决定是否 promote/apply。",
            "",
        ]

    for item in items:
        strategy_id = str(item.get("strategy_id") or "").strip()
        proposal_id = str(item.get("proposal_id") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        head_bits: list[str] = []
        if strategy_id:
            head_bits.append(f"策略 {strategy_id}")
        if proposal_id:
            head_bits.append(f"提案 {proposal_id}")
        if verdict:
            head_bits.append(f"回测结论 {verdict}（{_verdict_plain(verdict)}）")
        if head_bits:
            lines.append("· " + "，".join(head_bits) + "。")

        if item.get("completion_kind") == "data_gap":
            coverage = str(item.get("coverage_message") or "").strip()
            if coverage:
                lines.append(f"  数据缺口：{coverage}")
            next_action = str(item.get("next_required_action_message") or "").strip()
            if next_action:
                lines.append(f"  说明：{next_action}")
            continue

        display = item.get("metrics_display")
        for bullet in _interpret_backtest_metrics(
            display if isinstance(display, dict) else {}
        ):
            lines.append(f"  - {bullet}")

        report_path = str(item.get("report_path") or "").strip()
        if report_path:
            lines.append(
                f"  完整图表和逐笔记录见报告：{_markdown_code_span(report_path)}"
            )
        review_gate = item.get("review_gate")
        if (
            isinstance(review_gate, dict)
            and review_gate.get("paper_review_allowed") is not None
        ):
            allowed = bool(review_gate.get("paper_review_allowed"))
            lines.append(
                "  纸面（paper）复盘："
                + ("可以进行。" if allowed else "暂不建议，先补充证据再说。")
            )

    lines.append("")
    if has_data_gap:
        lines.append(
            "下一步：给对应市场配置/补齐历史数据源，或换一个已有真实历史数据的市场，"
            "再重新运行回测。"
        )
    elif has_fail_verdict:
        lines.append(
            "下一步：先看报告里的失败原因，调参或补上缺失的策略假设后重新回测；"
            "确认通过前不要 approve/promote。"
        )
    else:
        lines.append(
            "下一步：看一下回测报告，确认信号触发、仓位和账户/数据源绑定都符合预期；"
            "满意后再走 approve/promote 上线。"
        )
    return "\n".join(lines)


def _markdown_code_span(value: str) -> str:
    """Render path-like values without Markdown eating Windows backslashes."""

    text = str(value)
    longest = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    fence = "`" * (longest + 1)
    return f"{fence}{text}{fence}"
