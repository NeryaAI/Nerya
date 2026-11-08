"""Loaded Skill contract, not model-compliance or historical-replay proof."""
import json
from pathlib import Path

import pytest

from test_dex_exact_scope_routing import read_skill

pytestmark = pytest.mark.smoke


def test_script_driven_agent_is_not_silently_replaced_by_a_pure_script():
    text = ' '.join(read_skill(skill='strategy_author', file='SKILL.md').split())
    assert 'script-driven Agent' in text
    assert 'references/trend_follow_subagent.md' in text
    assert 'pure deterministic script does not satisfy' in text
    assert 'model decision' in text
    assert 'current-model retrospective' in text
    assert 'ordinary EMA backtest' in text


def test_script_agent_delivery_uses_contract_workflow_not_request_title():
    text = ' '.join(read_skill(skill='strategy_author', file='SKILL.md').split())
    contract = json.loads(
        (Path(__file__).resolve().parents[1]
         / 'nerya/skills/builtin/strategy_author/references/delivery-contract.json')
        .read_text()
    )
    assert 'strategy_authoring' in contract
    entry = text.split('For a script-driven Agent request', 1)[1].split(
        'For a plain paper CEX', 1
    )[0]
    for phrase in (
        'references/delivery-contract.json',
        'strategy_authoring',
        'binance_btcusdt_trend_paper_backtest',
        'task, market, model, or backtest title',
        'must not be passed as `workflow_id`',
        'strategy_replay',
    ):
        assert phrase in entry


def test_creation_checks_agent_replay_authorization_before_writes(monkeypatch):
    from types import SimpleNamespace
    from nerya.core.errors import TradingError
    from nerya.skills.builtin.backtest.scripts import authorized_replay

    monkeypatch.setattr(authorized_replay.time, 'time', lambda: 1793400000)
    policy = dict(enabled=True, authorization_id='acceptance-20261030-btc-agent-replay-bounded-7',
                  deadline=1793374988, max_calls=20, max_tokens=400000,
                  provider='fixture', model='fixture', key_ref='vault://must-not-be-returned')
    with pytest.raises(TradingError, match='^invalid_or_expired_retrospective_authorization$'):
        authorized_replay.authorized_policy(SimpleNamespace(get=lambda key: policy))

    author = read_skill(skill='strategy_author', file='SKILL.md')
    reference = read_skill(skill='backtest', file='references/replay-capabilities.md')
    entry = author.split('For a script-driven Agent request', 1)[1].split('For a plain paper CEX', 1)[0]
    assert 'before package writes' in entry
    assert 'references/replay-capabilities.md' in entry
    assert 'draft-only' in entry
    gate = reference.split('## Script-gated Agent decisions', 1)[1].split('## LP history', 1)[0]
    for instruction in ('deadline later than the current time', 'before package writes',
                        'invalid_or_expired_retrospective_authorization', 'draft-only',
                        'exact', 'delivery_update', 'blocked', 'end this turn',
                        'Create no candidate package', 'ordinary historical backtests separate'):
        assert instruction in gate


def test_script_agent_draft_defaults_to_the_replay_confirmation_role():
    from nerya.tools.native.strategy_runtime import _request_from_args

    request = _request_from_args({
        'strategy_id': 'btc_ema_trend',
        'strategy_class': 'trend',
        'execution_mode': 'script',
        'mode': 'paper',
        'markets': ['BINANCE:BTCUSDT'],
        'accounts': ['binance_paper'],
        'prompt': '创建一个脚本驱动 Agent 趋势策略，回测最近7天，不启用交易。',
    })
    assert request.subagents == ('market_analyst',)


def test_plain_script_draft_does_not_gain_an_agent_role():
    from nerya.tools.native.strategy_runtime import _request_from_args

    request = _request_from_args({
        'strategy_id': 'btc_ema_trend',
        'strategy_class': 'trend',
        'execution_mode': 'script',
        'mode': 'paper',
        'markets': ['BINANCE:BTCUSDT'],
        'accounts': ['binance_paper'],
        'prompt': '创建一个普通 EMA 趋势策略，回测最近7天。',
    })
    assert request.subagents == ()
