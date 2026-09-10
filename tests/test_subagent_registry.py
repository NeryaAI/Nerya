import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.security.prompt_injection import flag_suspicious
from nerya.subagents.registry import (
    DEFAULT_SUBAGENT_PROMPTS,
    DEFAULT_SUBAGENT_SKILLS,
)
from nerya.subagents.strategy_registry import StrategySubAgentRegistry


def test_default_strategy_tuner_prompt_passes_prompt_firewall():
    prompt = DEFAULT_SUBAGENT_PROMPTS["strategy_tuner"]

    assert flag_suspicious(prompt) == []


def test_default_stock_research_roles_have_market_data_access():
    roles = [
        "technical_analyst",
        "fundamentals_analyst",
        "sentiment_analyst",
        "bull_researcher",
        "bear_researcher",
        "risk_critic",
        "research_manager",
    ]

    for role in roles:
        allowed = set(DEFAULT_SUBAGENT_SKILLS[role])
        assert "market_data" in allowed, role
        assert "market_data_routing" in allowed, role
        assert "web_search_fetch" in allowed, role


@pytest.mark.smoke
def test_strategy_registry_preserves_requested_role_with_canonical_profile(tmp_path):
    registry = StrategySubAgentRegistry(paths=WorkspacePaths(tmp_path))

    spec = registry.get("sec_filing_analyst")

    assert spec.name == "sec_filing_analyst"
    assert spec.canonical_name == "fundamentals_analyst"
    assert spec.prompt == DEFAULT_SUBAGENT_PROMPTS["fundamentals_analyst"]
    assert set(spec.allowed_skills) == set(DEFAULT_SUBAGENT_SKILLS["fundamentals_analyst"])


@pytest.mark.smoke
def test_default_stock_research_aliases_inherit_research_profile(tmp_path):
    registry = StrategySubAgentRegistry(paths=WorkspacePaths(tmp_path))

    aliases = {
        "fundamental_analyst": "fundamentals_analyst",
        "dcf_modeler": "fundamentals_analyst",
        "sec_filing_analyst": "fundamentals_analyst",
        "guru_perspective": "fundamentals_analyst",
    }

    for requested, canonical in aliases.items():
        spec = registry.get(requested)
        assert spec.name == requested
        assert spec.canonical_name == canonical
        assert "market_data" in spec.allowed_skills
        assert "web_search_fetch" in spec.allowed_skills


def test_risk_role_uses_declared_evidence_contract_not_a_fixed_tool_sequence():
    from nerya.workspace.prompt_bundles import load_bundle
    prompt = DEFAULT_SUBAGENT_PROMPTS["risk_critic"]
    assert prompt == load_bundle().subagents["risk_critic"]
    assert all(field in prompt for field in ("verdict", "invalidation", "evidence", "confidence"))
    assert "never invent" in prompt
    assert "get_candles" not in prompt


def test_all_default_roles_have_one_packaged_prompt_source():
    from nerya.workspace.prompt_bundles import load_bundle
    bundle = load_bundle()
    assert set(DEFAULT_SUBAGENT_SKILLS) <= set(bundle.subagents)
    assert DEFAULT_SUBAGENT_PROMPTS == bundle.subagents
    prompt = DEFAULT_SUBAGENT_PROMPTS["fundamentals_analyst"]
    assert all(field in prompt for field in ("valuation", "evidence", "confidence"))
    assert "native tool contracts" in prompt
    assert "get_ticker" not in prompt


def test_strategy_registry_uses_tuning_subagent_prompt_and_tier(tmp_path):
    paths = WorkspacePaths(tmp_path)
    root = paths.strategy("alpha")
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "alpha",
            "title": "Alpha strategy",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "cron", "cron": "*/5 * * * *"},
            "llm_policy": {"default_tier": "light", "allowed_tiers": ["light"]},
            "subagents": [],
            "tuning": {
                "enabled": True,
                "schedule": {"type": "cron", "cron": "0 */6 * * *"},
                "subagent": {
                    "name": "strategy_tuner",
                    "prompt_file": "subagents/strategy_tuner.agent.md",
                    "tier": "medium",
                },
            },
        },
    )
    (root / "main.py").write_text(
        "def run(ctx):\n    return ctx.result.hold(reason='test')\n",
        encoding="utf-8",
    )
    prompt_path = root / "subagents" / "strategy_tuner.agent.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("Tune alpha using live review evidence.", encoding="utf-8")

    registry = StrategySubAgentRegistry(paths=paths, strategy_id="alpha")
    spec = registry.get("strategy_tuner")

    assert spec.prompt == "Tune alpha using live review evidence."
    assert spec.prompt_path == prompt_path
    assert spec.tier == "medium"
    assert "strategy_tuner" in registry.list_names()
