import pytest

from tools.extract_cases import parse_plan


pytestmark = pytest.mark.smoke


def test_parse_plan_preserves_prompt_context_outside_backticks():
    text = """
### J2. Telegram 用户主动发消息触发 agent

| 项 | 内容 |
|----|------|
| **Prompt** | （在 Telegram 群里）`/ask 帮我看一下 BTC 趋势` |

### L7. 超长上下文截断

| 项 | 内容 |
|----|------|
| **Prompt** | 发 50 个超长文档 → `给我总结` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["J2"]["prompt"] == "（在 Telegram 群里）/ask 帮我看一下 BTC 趋势"
    assert cases["L7"]["prompt"] == "发 50 个超长文档 → 给我总结"


def test_financial_datasets_cases_encode_readiness_expectations():
    text = """
### E6. Financial Datasets key 缺失降级

| 项 | 内容 |
|----|------|
| **Prompt** | （未配置 FD key）`深度研究 AMD` |

### H9. Financial Datasets keys

| 项 | 内容 |
|----|------|
| **Prompt** | `配置 Financial Datasets 的 key：[KEY]` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E6"]["must_contain"] == (
        "Financial Datasets|key|ready|status|Settings|Integrations|"
        "配置|凭证|未配置|缺失|not configured|missing"
    )
    assert cases["E6"]["api_check"] == "financial_datasets_status=false"
    assert cases["H9"]["api_check"] == "financial_datasets_status=false"


def test_team_concurrency_case_uses_team_run_api_evidence():
    text = """
### E7. 并发限制

| 项 | 内容 |
|----|------|
| **Prompt** | `同时分析这 8 只科技股：AAPL/MSFT/NVDA/AMD/GOOGL/META/AMZN/TSLA` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E7"]["api_check"] == "team_run_exists=true"
    assert "AgentTeam" in cases["E7"]["prompt"]


def test_strategy_team_case_requires_team_and_strategy_artifact_evidence():
    text = """
### E3. strategy_design_team 起策略

| 项 | 内容 |
|----|------|
| **Prompt** | `召集团队给我设计一个 SOL 短期策略` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E3"]["api_check"] == (
        "team_run_exists=true:strategy_proposal_kind=strategy_package_proposal:sol"
    )


def test_deep_equity_research_case_requires_team_run_api_evidence():
    text = """
### E5. Equity research 全链路（美股深度）

| 项 | 内容 |
|----|------|
| **Prompt** | `深度研究 NVDA：基本面 + DCF + SEC 最新 10-K + 投资大师视角` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E5"]["must_contain"] == ""
    assert cases["E5"]["api_check"] == "team_run_exists=true"


def test_team_cross_language_case_requires_team_run_and_output_language_contract():
    text = """
### E12. 团队跨语言

| 项 | 内容 |
|----|------|
| **Prompt** | `中文分析、英文报告` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E12"]["must_contain"] == ""
    assert cases["E12"]["api_check"] == (
        "team_run_exists=true:team_output_language=English:team_analysis_language=Chinese"
    )
    assert "AgentTeam" in cases["E12"]["prompt"]
    assert "中文分析" in cases["E12"]["prompt"]
    assert "英文报告" in cases["E12"]["prompt"]


def test_small_team_run_case_uses_api_evidence_not_text_assertion():
    text = """
### E13. team_run 同 turn 并行（小用例）

| 项 | 内容 |
|----|------|
| **Prompt** | `让 3 个分析师同时看一眼 ETH，5 秒内给我观点` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E13"]["must_contain"] == ""
    assert cases["E13"]["api_check"] == "team_run_exists=true:team_roles_total=3"


def test_team_edge_cases_do_not_inherit_text_only_team_assertion():
    text = """
### E8. 团队中途失败 → 仍出 partial report

| 项 | 内容 |
|----|------|
| **Prompt** | `（模拟 technical_analyst 失败：用一个不存在的 ticker ZZZZ）分析 ZZZZ` |

### E10. 与策略生成联动

| 项 | 内容 |
|----|------|
| **Prompt** | `根据刚才 TSLA 分析的结论，做一个对应的策略` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E8"]["must_contain"] != "team"
    assert "ZZZZ" in cases["E8"]["must_contain"]
    assert cases["E10"]["must_contain"] == ""
    assert cases["E10"]["api_check"] == (
        "strategy_proposal_kind=strategy_package_proposal:tsla"
    )


def test_non_team_edge_cases_have_domain_specific_text_contracts():
    text = """
### E9. 存档上一份报告

| 项 | 内容 |
|----|------|
| **Prompt** | `（E1 完成）把刚才的 TSLA 报告存档` |

### E14. 大师视角

| 项 | 内容 |
|----|------|
| **Prompt** | `如果是巴菲特和芒格，他们怎么看 PDD？` |

### G9. Polymarket odds

| 项 | 内容 |
|----|------|
| **Prompt** | `让我能看 Polymarket 的事件赔率` |

### H6. NVDA 财报关键指标

| 项 | 内容 |
|----|------|
| **Prompt** | `NVDA 财报关键指标` |

### L4. 不存在标的

| 项 | 内容 |
|----|------|
| **Prompt** | `分析 XYZNONEXIST` |

### L11. promote 幂等红线

| 项 | 内容 |
|----|------|
| **Prompt** | `两次同 ID promote` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["E9"]["must_contain"] != "team"
    assert "TSLA" in cases["E9"]["must_contain"]
    assert cases["E14"]["must_contain"] != "team"
    assert "PDD" in cases["E14"]["must_contain"]
    assert "Polymarket" in cases["G9"]["must_contain"]
    assert "股价" in cases["H6"]["must_contain"]
    assert "数据真空" in cases["L4"]["must_contain"]
    assert "不是一个我能识别" in cases["L4"]["must_contain"]
    assert "not a real ticker" in cases["L4"]["must_contain"]
    assert "zero matches" in cases["L4"]["must_contain"]
    assert "Evidence gap" in cases["L4"]["must_contain"]
    assert "cannot be produced" in cases["L4"]["must_contain"]
    assert "证据不完整" in cases["L11"]["must_contain"]


def test_strategy_proposal_api_cases_do_not_require_prose_regex():
    text = """
### C7. [Agent Team] NVIDIA 投研策略

| 项 | 内容 |
|----|------|
| **Prompt** | `用 AgentTeam 长期分析 NVDA，每天开盘前给我评分和仓位建议` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["C7"]["api_check"].startswith("strategy_proposal_kind=")
    assert cases["C7"]["must_contain"] == ""
    assert cases["C7"]["must_not_contain"] == r"\[harness\] Wall-clock budget"


def test_cross_venue_arbitrage_requires_strategy_proposal_api_evidence():
    text = """
### GX14. 跨 venue 套利策略（衍生品 + 现货）

| 项 | 内容 |
|----|------|
| **Prompt** | `做一个 Binance 现货 + Aster 永续的 cash-and-carry 套利策略，回测 30 天` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["GX14"]["api_check"] == (
        "strategy_proposal_kind=strategy_package_proposal:cash:carry:aster:binance"
    )
    assert "cash" in cases["GX14"]["must_contain"]


def test_telegram_connect_case_requires_honest_blocked_connection_text():
    text = """
### J1. Telegram 双向

| 项 | 内容 |
|----|------|
| **Prompt** | `把 Telegram bot 连上：token=vault://TG_BOT_TOKEN chat_id=vault://TG_CHAT_ID` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["J1"]["api_check"] == (
        "proposal_kind=core_config_patch:"
        "proposal_after_has=channels.telegram.bot_token_ref|channels.telegram.chat_id_ref"
    )
    assert "not established" in cases["J1"]["must_contain"]
    assert "successfully connected" in cases["J1"]["must_not_contain"]


def test_message_routing_case_requires_config_proposal_evidence():
    text = """
### J5. 多渠道分级路由

| 项 | 内容 |
|----|------|
| **Prompt** | `info → Telegram，critical → Telegram + Discord，silent → 不推送` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["J5"]["api_check"] == (
        "proposal_kind=core_config_patch:proposal_after_has=severity_routes"
    )
    assert "severity" in cases["J5"]["must_contain"]


def test_telegram_diagnose_case_requires_gateway_diagnose_tool_evidence():
    text = """
### J6. Telegram 诊断

| 项 | 内容 |
|----|------|
| **Prompt** | `Telegram 怎么连不上？帮我诊断` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["J6"]["api_check"] == "tool_used=gateway_diagnose"
    assert "bot_token" in cases["J6"]["must_contain"]


def test_webhook_config_cases_require_config_proposal_evidence():
    text = """
### J3. Discord webhook 出站

| 项 | 内容 |
|----|------|
| **Prompt** | `配置 Discord webhook，把所有 critical 风险事件推送过去` |

### J4. 通用 webhook

| 项 | 内容 |
|----|------|
| **Prompt** | `把策略每次成交事件 POST 到 https://webhook.site/<id>` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["J3"]["api_check"] == "proposal_kind=core_config_patch"
    assert cases["J4"]["api_check"] == "proposal_kind=core_config_patch"
    assert "webhook" in cases["J3"]["must_contain"]
    assert "webhook" in cases["J4"]["must_contain"]


def test_risk_event_case_requires_monitoring_schedule_evidence():
    text = """
### K4. 风险事件应急

| 项 | 内容 |
|----|------|
| **Prompt** | `如果策略当日亏损超 5%，停掉策略并通知我` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["K4"]["api_check"] == (
        "tool_used=task_create:schedule_session_kind=agent:"
        "tool_not_used=kill_switch_set"
    )
    assert "task" in cases["K4"]["must_contain"]


def test_custom_slack_team_case_requires_durable_workflow_evidence():
    text = """
### K7. 自定义新闻 + 自定义 venue + 团队

| 项 | 内容 |
|----|------|
| **Prompt** | `把我公司内部的 Slack feed (API: ...) 加进来，每小时让团队基于它判断 ETH 仓位` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["K7"]["api_check"] == (
        "proposal_kind=skill_proposal:team_run_exists=true:"
        "tool_used=task_create:schedule_session_kind=agent"
    )
    assert "Slack" in cases["K7"]["must_contain"]


def test_direct_trade_risk_case_requires_tool_evidence():
    text = """
### L3. 风控限额超过

| 项 | 内容 |
|----|------|
| **Prompt** | `（单笔限额 10% NAV）帮我 all-in BTC` |
"""

    cases = {case["id"]: case for case in parse_plan(text)}

    assert cases["L3"]["api_check"] == (
        "tool_used=risk_check:tool_result_status=risk_check.rejected:"
        "tool_not_used=strategy_generate_proposal"
    )
    assert "风险" in cases["L3"]["must_contain"]
