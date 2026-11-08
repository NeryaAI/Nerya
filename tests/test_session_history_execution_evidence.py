"""Historical assistant prose must not masquerade as current execution."""
import json

from nerya.agent.session_compaction import compact_session_history, SessionCompactionPolicy


def row(index, *, count=0, meta=True):
    return {'role': 'assistant', 'content': 'The tool returned: success.',
            'message_id': f'm{index}', 'message_seq': index, 'turn_id': f't{index}',
            'meta_json': json.dumps({'turn': {'budget': {'tool_calls': count}}}) if meta else '{}'}


def test_recent_zero_tool_claim_has_host_evidence_boundary_without_deleting_text():
    rows = [row(1)]
    result = compact_session_history(rows)
    text = result.messages[0]['content']
    assert 'recorded_tool_calls=0' in text
    assert 'not an execution receipt' in text
    assert result.messages[0]['role'] == 'user'
    assert 'Internal context, not a user request' in text
    assert 'do not quote this context as reply text' in text
    assert result.messages[1] == {'role': 'assistant', 'content': rows[0]['content']}
    assert rows[0]['content'] == 'The tool returned: success.'


def test_compacted_zero_tool_claim_retains_evidence_boundary():
    rows = [row(i) for i in range(1, 9)]
    result = compact_session_history(rows, policy=SessionCompactionPolicy(keep_recent_pairs=1, trigger_pairs=1))
    assert 'recorded_tool_calls=0' in result.checkpoint['rendered']
    assert 'The tool returned: success.' in result.checkpoint['digest']['assistant_results']
    assert 'Internal context, not a user request' in result.checkpoint['rendered']
    assert all('recorded_tool_calls=0' in m['content'] for m in result.messages[1:] if m['role'] == 'user')
    assert [m['content'] for m in result.messages if m['role'] == 'assistant'] == [r['content'] for r in rows[-2:]]


def test_runtime_metadata_is_separate_from_exact_historical_assistant_speech():
    record = row(1)
    record['content'] = '  The tool returned: success.\n\nOriginal wording.  '
    messages = compact_session_history([record]).messages
    assert messages[1:] == [{'role': 'assistant', 'content': record['content']}]
    assert messages[0]['role'] == 'user'
    assert 'recorded_tool_calls=0' in messages[0]['content']
    assert all('Historical runtime evidence' not in m['content'] for m in messages if m['role'] == 'assistant')


def test_legacy_missing_evidence_is_not_invented_as_zero():
    assert 'recorded_tool_calls=0' not in compact_session_history([row(1, meta=False)]).messages[0]['content']


def test_positive_count_does_not_claim_success():
    text = compact_session_history([row(1, count=2)]).messages[0]['content']
    assert 'recorded_tool_calls=2' in text
    assert 'not proof of successful' in text


def test_kernel_reloads_host_counter_from_durable_transcript(tmp_path):
    from copy import deepcopy
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.db.sqlite import connect
    from nerya.db.repositories import AgentSessionRepository
    from nerya.agent.kernel import AgentKernel
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    con = connect(config.paths.db)
    try:
        repo = AgentSessionRepository(con)
        repo.upsert_session(session_id='evidence', title='Evidence')
        record = row(1)
        repo.record_message(message_id='a1', session_id='evidence', turn_id='t1',
                            role='assistant', content=record['content'], ts=1,
                            meta=json.loads(record['meta_json']))
        prior = AgentKernel(config=config, skills=None)._load_prior_chat_messages(session_id='evidence')
        assert 'recorded_tool_calls=0' in prior[0]['content']
        assert prior[0]['role'] == 'user'
        assert prior[1] == {'role': 'assistant', 'content': record['content']}
        assert repo.transcript('evidence')[0]['content'] == record['content']
    finally:
        con.close()
