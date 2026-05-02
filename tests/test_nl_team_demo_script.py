import importlib.util
from pathlib import Path


def _load_demo():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "run_nl_team_demo.py"
    spec = importlib.util.spec_from_file_location("run_nl_team_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_prompts_are_short_chinese_operator_inputs():
    demo = _load_demo()

    prompts = demo._session_prompts("demo-test")

    assert [p["label"] for p in prompts] == [
        "1_strategy_line",
        "2_risk_tightening",
        "3_trigger_gate",
        "4_demo_summary",
    ]
    for prompt in prompts:
        text = prompt["trigger"]["payload"]["text"]
        assert len(text) < 260
        assert any("\u4e00" <= ch <= "\u9fff" for ch in text)
        assert "Hey Nerya" not in text
        assert "multi-expert team memo" not in text


def test_demo_validation_requires_full_delivery_and_blocks_degraded_replies():
    demo = _load_demo()

    good = (
        "BTC 1 小时趋势突破策略：入场在突破后确认，退出使用止损。"
        "风控包含单笔风险限制，触发器按 1h 收盘运行。"
        "只做纸交易 paper。demo assumptions：没有实时行情时使用假设。"
    ) * 4
    assert demo._validate_reply("1_strategy_line", good)["ok"] is True

    missing = "BTC 1 小时趋势突破策略，只做纸交易。" * 20
    bad = demo._validate_reply("1_strategy_line", missing)
    assert bad["ok"] is False
    assert bad["missing_groups"]

    degraded = good + " The team ran but hit skill access walls."
    bad = demo._validate_reply("1_strategy_line", degraded)
    assert bad["ok"] is False
    assert bad["degraded_markers"]


def test_service_command_prefers_repo_module_without_dashboard():
    demo = _load_demo()

    commands = demo._service_cmd_candidates(
        Path("workspace"), "127.0.0.1", 18319,
    )

    assert commands[0][1:4] == ["-m", "nerya.cli.app", "serve"]
    assert "--no-dashboard" in commands[0]
