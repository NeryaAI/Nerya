import json
from datetime import date, timedelta

import pytest

from nerya.data_api.defillama import defillama_specs
from nerya.data_api.registry import DataApiRegistry
from nerya.llm.tool_compaction import compact_tool_result
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall
from test_defillama_data_api import Fake, PID, POOL, ROWS

pytestmark = pytest.mark.smoke


def handler_output(action, fake, args):
    registry = DataApiRegistry()
    for spec in defillama_specs(fake):
        registry.register_action(spec)
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'defillama', 'action': action, 'args': args,
    }), registry=registry)
    assert not result.is_error, result.text()
    return result.content[0].data


@pytest.mark.parametrize('action,count', [('yield_pools', 16), ('yield_pools', 31), ('yield_history', 2), ('yield_history', 31)])
def test_real_handler_to_compactor_retains_contract(action, count, monkeypatch):
    monkeypatch.setattr('socket.socket.connect', lambda *a: pytest.fail('network forbidden'))
    pools = [{**POOL, 'pool': f'{i:08x}-4b52-42bf-a116-608b370f9501',
              'project': 'horizon' if i < 5 else 'aave-v3'} for i in range(count)]
    history_rows = ROWS * 2 if count == 2 else [
        {**ROWS[0], 'timestamp': (date(2026, 9, 22) + timedelta(days=i)).isoformat() + 'T00:00:00Z'}
        for i in range(count)
    ]
    fake = Fake(pools=pools) if action == 'yield_pools' else Fake(rows=history_rows)
    args = {} if action == 'yield_pools' else {
        'pool_id': PID, 'start': '2026-10-20' if count == 2 else '2026-09-22', 'end': '2026-10-22'}
    out = handler_output(action, fake, args)
    result = compact_tool_result('data_api', out, size_threshold=0, raw_ref='raw:test')
    kept = result.kept
    assert kept['_envelope'] == out['_envelope']
    assert kept['source_url'] == out['source_url']
    assert kept['truncated'] is False
    assert kept['rows_sample'] == out['rows']
    assert kept['sample_truncated'] is False
    assert kept['sample_omitted_rows'] == 0
    assert len(json.dumps(kept).encode()) < 26000
    if action == 'yield_pools':
        assert len(kept['rows_sample']) == count
        assert kept['rows_sample'][5]['project'] == 'aave-v3'
        assert result.raw_ref == 'raw:test'
    else:
        assert kept['_envelope']['coverage']['missing_utc_days'] == (['2026-10-21'] if count == 2 else [])
        assert len(kept['rows_sample']) == count
        assert kept['rows_sample'][0]['timestamp'] == out['rows'][0]['timestamp']
        assert kept['rows_sample'][0]['deposit_base_apy_percent'] == 3.1
        assert kept['next_required_action'] == out['next_required_action']


def test_generic_contract_bounded_and_unknown_body_excluded():
    out = handler_output('yield_pools', Fake(), {})
    out['provider'] = 'arbitrary_source'
    out['action'] = 'arbitrary_table'
    out['_envelope']['raw_body'] = 'secret-body' * 10000
    out['_envelope']['coverage'] = {'missing_utc_days': ['2026-01-01'] * 10000}
    out['rows'][0]['padding'] = 'x' * 100000
    kept = compact_tool_result('data_api', out, size_threshold=0).kept
    assert kept['_envelope']['rows_sha256'] == out['_envelope']['rows_sha256']
    assert 'raw_body' not in kept['_envelope']
    assert kept['rows_sample'] == []
    assert kept['sample_truncated'] is True
    assert kept['sample_omitted_rows'] == 1
    assert kept['next_required_action']['arguments']['op'] == 'schema'
    assert 'unchanged' in kept['next_required_action']['hint']
    assert 'read_raw' not in json.dumps(kept['next_required_action'])
    assert len(json.dumps(kept).encode()) < 16000
    assert kept['metadata_truncated'] is True


def test_row_cap_is_separate_from_upstream_truncation():
    out = handler_output('yield_pools', Fake(), {})
    out['rows'] *= 70
    out['row_count'] = 70
    kept = compact_tool_result('data_api', out, size_threshold=0).kept
    assert kept['truncated'] is False
    assert kept['sample_truncated'] is True
    assert kept['sample_omitted_rows'] == 70 - len(kept['rows_sample'])
    assert 0 < len(kept['rows_sample']) <= 50
    assert all(row == out['rows'][0] for row in kept['rows_sample'])


def test_actual_loop_render_and_event_projection_keep_complete_small_history(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    from nerya.agent.tool_projection import project_tool_results
    from nerya.tools.result_contracts import parse_compacted_kept_jsonish
    from nerya.tools.types import ToolResult
    import nerya.llm.tool_raw_store as raw_store
    monkeypatch.setattr(raw_store, 'write_default', lambda **kw: 'raw:test-only')
    monkeypatch.setattr('socket.socket.connect', lambda *a: pytest.fail('network forbidden'))
    rows = [{**ROWS[0], 'timestamp': (date(2026, 9, 22) + timedelta(days=i)).isoformat() + 'T00:00:00Z'} for i in range(31)]
    out = handler_output('yield_history', Fake(rows=rows), {'pool_id': PID, 'start': '2026-09-22', 'end': '2026-10-22'})
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root=str(tmp_path))
    result = ToolResult.from_json(tool_use_id='history', name='data_api', data=out)
    projected = project_tool_results([result], render_tool_result=loop._render_tool_result,
        rendered_tool_result_text=loop._rendered_tool_result_text)
    text = projected.transcript_blocks[0]['content'][0]['text']
    kept = parse_compacted_kept_jsonish(text)
    assert kept['rows_sample'] == out['rows']
    assert kept['_envelope'] == out['_envelope']
    assert kept['sample_truncated'] is False
    assert projected.event_blocks[0]['result'] == text
