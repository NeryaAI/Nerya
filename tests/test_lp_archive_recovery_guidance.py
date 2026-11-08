"""Offline real Skill loading; no model-compliance or historical-replay claim."""
import hashlib
import socket
from pathlib import Path

import pytest

from nerya.skills.registry import SkillRegistry, load_entry
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import register_skill_tool
from nerya.tools.permissions import PermissionContext, PermissionEngine
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
SKILL = Path(__file__).resolve().parents[1] / 'nerya/skills/builtin/strategy_author'
REFERENCE = 'references/lp-sampled-research.md'


@pytest.fixture
def read_skill(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Skill guidance loading must remain offline')

    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    catalog = SkillRegistry()
    catalog.register(load_entry(SKILL / 'SKILL.md', source='builtin'))
    tools = ToolRegistry()
    register_skill_tool(tools, skill_index=SkillIndex([], registry_provider=lambda: catalog))
    executor = NativeToolExecutor(registry=tools, permission_engine=PermissionEngine(),
                                  permission_context=PermissionContext())

    def read(file):
        result = executor.execute(ToolCall(name='Skill', arguments={
            'skill': 'strategy_author', 'file': file, 'limit': 400}))
        assert not result.is_error, result.text()
        data = tool_json_data(result)
        assert data['next_offset'] is None, 'Keep this recovery guide in one bounded page'
        assert data['revision'] == hashlib.sha256((SKILL / file).read_bytes()).hexdigest()
        return ' '.join(result.text().split())

    return read


@pytest.mark.parametrize('phrases', [
    (
        'Every request requires `pool_address`, `tick_lower`, `tick_upper`, `step_blocks`',
        'explicit research assumption', 'not observed pool state',
        'verified tick spacing', 'includes both endpoints', 'ceil(D / S) + 1',
        'D=10, S=6 needs 3 samples, not 2', 'max_samples` alone does not reduce it',
        'preserving pool and window', '`run_shell` is not a prerequisite or recovery route',
    ),
    (
        '`safe_to_retry:false` applies to the failed operation',
        'autonomously research or select another supported public read-only source',
        'Preserve any user-pinned data provider',
    ),
    (
        'partial/incomplete', 'create a disabled scoped candidate', 'SAME candidate',
        'Keep trading and review schedules disabled', 'does not complete replay',
        'delivery blocked/incomplete', 'unavailable inputs',
        'exact requested chain, protocol, ordered token contracts, pool and entire window',
        'dynamic strategy retains its dynamic scope', 'fixed-range assets cannot substitute',
    ),
    (
        '`params.lp_source`', '`params.lp_replay`', '`params.lp_sampled_policy`',
        'host owns source/replay/policy configuration from the saved manifest',
        '`replay_sampled_lp()` with no arguments', 'obtains the saved manifest',
        'Do not pass replacement source/replay/policy arguments or inspect internal',
        '`lp_replay` / `lp_accounting` implementation as a prerequisite',
        '"source_asset"', '"expected_revision"', 'actual returned candidate path',
        'performance_evidence:false', 'requested_window_complete:false',
    ),
])
def test_recovery_guidance_is_available_through_registered_skill(read_skill, phrases):
    text = read_skill(REFERENCE)
    for phrase in phrases:
        assert phrase in text


@pytest.mark.parametrize('asset,target', [
    ('lp_sampled_strategy.py.j2', 'main.py'),
    ('lp_sampled_policy.py.j2', 'lp_sampled_policy.py'),
    ('lp_sampled_replay.py.j2', 'scripts/custom_replay.py'),
])
def test_documented_copy_paths_resolve_to_revision_bound_skill_assets(read_skill, asset, target):
    guide = read_skill(REFERENCE)
    assert f'`templates/{asset}` -> `{target}`' in guide
    loaded = read_skill('templates/' + asset)
    if target == 'scripts/custom_replay.py':
        assert 'from nerya.strategies.research import replay_sampled_lp' in loaded
        assert 'result = replay_sampled_lp()' in loaded
