"""Failure prose is a conclusion; machine evidence stays in the trace."""
import pytest

from nerya.agent.loop import (
    _build_deterministic_final_summary, _build_llm_safety_final_synthesis_fallback,
    _build_llm_initial_safety_rejection_text, _build_llm_safety_required_tool_fallback,
    _build_required_action_provider_exhausted_text, _wall_time_llm_timeout_text,
    _build_llm_timeout_evidence_fallback, _build_tool_evidence_final_text,
    _required_action_repeated_error_blocked_final_text,
    _preserve_pre_tool_answer_after_optional_gap,
)
from nerya.tools.types import ToolResult, ToolError
from nerya.agent.outcome_summary import render_outcome_summary

pytestmark = pytest.mark.smoke
RAW = 'rows_sample {"provider":"example","secret":"PRIVATE_SENTINEL"} <tool_use_error>raw error</tool_use_error>'
TRANSCRIPT = [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call',
                                         'content': [{'type': 'text', 'text': RAW}], 'is_error': True}]}]

def assert_public(text):
    for value in ['PRIVATE_SENTINEL', 'rows_sample', 'tool_use_error', 'provider_error', '已有证据', '原始请求', 'Technical detail', '{', '}']:
        assert value not in text
    assert len(text) < 650

@pytest.mark.parametrize('reason', ['cancelled:operator_cancel', 'repeated_tool_call', 'model_output_truncated',
    'timeout', 'stream_interrupted', 'empty_model_response', 'cancelled:command_control_unavailable'])
def test_abort_does_not_dump_tool_payload_or_provider_error(reason):
    text = _build_deterministic_final_summary(iterations=36, tool_calls=40, error_count=3,
        had_model_text=True, evidence_snippets=[RAW], abort_reason=reason, failure_detail=RAW,
        original_user_text='完成这个任务 ' + RAW)
    assert_public(text)
    assert '执行详情' in text

@pytest.mark.parametrize('builder', [
    lambda: _build_llm_safety_final_synthesis_fallback(transcript=TRANSCRIPT, original_user_text=RAW, error=ValueError(RAW)),
    lambda: _build_llm_initial_safety_rejection_text(original_user_text=RAW, error=ValueError(RAW)),
    lambda: _build_llm_safety_required_tool_fallback(transcript=TRANSCRIPT, original_user_text=RAW, pending_required_tool_names=('raw_internal_tool',), error=ValueError(RAW)),
    lambda: _build_required_action_provider_exhausted_text(transcript=TRANSCRIPT, original_user_text=RAW, pending_required_tool_names=('raw_internal_tool',), error=ValueError(RAW)),
    lambda: _wall_time_llm_timeout_text(ValueError(RAW), original_user_text=RAW),
    lambda: _build_llm_timeout_evidence_fallback(transcript=TRANSCRIPT, original_user_text=RAW),
    lambda: _build_tool_evidence_final_text(original_user_text=RAW, results=[ToolResult.from_json(tool_use_id='x', name='any_tool', data={'raw': RAW})]),
    lambda: _required_action_repeated_error_blocked_final_text({'any_tool'}, [
        ToolResult.from_error(tool_use_id='x', name='any_tool', error=ToolError(message=RAW))]),
])
def test_all_host_failure_fallbacks_separate_diagnostics(builder):
    assert_public(builder())

def test_no_diagnostic_is_mistaken_for_a_success_or_side_effect_claim():
    for language in ('en', 'zh'):
        text = render_outcome_summary(reason='unknown:PRIVATE_SENTINEL', language=language, tool_calls=2)
        assert_public(text)
        assert 'not establish' in text if language == 'en' else '不足以确认' in text
        assert 'no changes' not in text and '未修改' not in text


def test_reasoning_repetition_has_precise_public_copy_without_diagnostics():
    for language, title, recovery in (
        ('zh', '模型推理陷入重复，本轮已停止，任务尚未完成。', '调整模型设置或选择其他模型'),
        ('en', 'The model reasoning became repetitive. This run stopped before completing the task.',
         'Adjust the model settings or select another model'),
    ):
        text = render_outcome_summary(reason='model_reasoning_repetition',
            language=language, tool_calls=24, error_count=2)
        assert_public(text)
        assert text.startswith(title) and recovery in text
        assert 'model_reasoning_repetition' not in text
        assert '241' not in text and 'revision=' not in text
        interrupted = render_outcome_summary(reason='stream_interrupted', language=language)
        assert not interrupted.startswith(title)


def test_interrupted_turn_keeps_tool_facts_out_of_public_fallback():
    result = ToolResult.from_json(tool_use_id='search-1', name='data_api', data={
        'provider': 'onchainos', 'chain': 'Solana', 'market': 'Jupiter SOL/USDC',
        'revision': 'REDACTED', 'path': '/private/fixture/SKILL.md',
    })
    text = _build_deterministic_final_summary(
        iterations=3, tool_calls=1, error_count=0, had_model_text=False,
        abort_reason='stream_interrupted', original_user_text='搜索 Solana 链上数据',
        completed_tool_results=[result],
    )
    assert_public(text)
    assert text == render_outcome_summary(reason='stream_interrupted',
        user_text='搜索 Solana 链上数据', tool_calls=1)
    assert 'Jupiter SOL/USDC' in result.text()


def test_optional_tool_gap_preserves_answer_without_appending_raw_diagnostics():
    text = _preserve_pre_tool_answer_after_optional_gap(prior_text='已经获得的结论。',
        current_text='补充查询未完成。', gap_notes=[RAW])
    assert_public(text)
    assert '已经获得的结论。' in text and '补充查询未完成。' in text

def test_actual_loop_failure_summary_preserves_raw_tool_event():
    from test_agent_loop_final_summary import _Gateway, _response, _tool_use, _descriptor, _loop
    from nerya.harness.cancellation import CancelledError
    class Failing(_Gateway):
        def call_messages(self, **kwargs):
            if self.responses:
                return super().call_messages(**kwargs)
            raise CancelledError('cancelled:operator_cancel')
    gateway = Failing(_response(_tool_use('any_tool'), stop_reason='tool_use'))
    loop = _loop(gateway, [_descriptor('any_tool', lambda call: ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data={'raw_diagnostic': RAW}))])
    outcome = loop.run(system='Test', user_message='帮我完成任务')
    assert_public(outcome.final_text)
    assert 'PRIVATE_SENTINEL' in str(outcome.blocks)


@pytest.mark.parametrize('required', [False, True])
def test_repeated_error_stops_once_without_dumping_tool_error(required):
    from test_agent_loop_final_summary import _Gateway, _response, _tool_use, _descriptor, _loop
    calls = []

    def fail(call):
        calls.append(call.id)
        return ToolResult.from_error(tool_use_id=call.id, name=call.name,
            error=ToolError(message=RAW, retryable=False))

    gateway = _Gateway(*[_response(_tool_use('any_tool', call_id=f'call-{i}'),
        stop_reason='tool_use') for i in range(8)])
    loop = _loop(gateway, [_descriptor('any_tool', fail, read_only=not required)], max_iterations=8,
        required_artifacts=({'kind': 'tool_result', 'tool': 'any_tool'},) if required else ())
    outcome = loop.run(system='Test', user_message='完成任务')
    assert len(calls) == 1
    assert outcome.abort_reason == 'repeated_tool_call' or outcome.transition_reason == 'required_action_repeated_error_blocked'
    assert_public(outcome.final_text)
    assert '无进展' in outcome.final_text
    assert 'PRIVATE_SENTINEL' in str(outcome.blocks)


def test_kernel_failure_persists_safe_conclusion_and_redacted_diagnostic(monkeypatch):
    from types import SimpleNamespace
    from nerya.agent import kernel
    from nerya.llm.messages import _make_llm_error

    records, events, turns = [], [], []
    monkeypatch.setattr(kernel.jsonl, 'append', lambda *args: records.append(args[1]))
    monkeypatch.setattr(kernel, 'get_default_bus', lambda: SimpleNamespace(
        publish=lambda *args, **kwargs: events.append(kwargs)))
    stub = SimpleNamespace(
        config=SimpleNamespace(paths=SimpleNamespace(journal=lambda _: 'unused')),
        _record_session_db_message=lambda **kwargs: None,
        _record_session_db_turn=lambda **kwargs: turns.append(kwargs),
        _sessions=SimpleNamespace(append_turn=lambda *args, **kwargs: None),
    )
    secret = 'sk-' + 'abcdefgh1234567890' * 3
    error = _make_llm_error(provider='fixture', status=429,
        doc={'error': {'message': 'diagnostic-marker api_key=' + secret}}, resp_headers={})
    kernel.AgentKernel._record_failed_turn(stub, turn_id='turn',
        trigger={'payload': {'text': '完成任务'}}, trigger_event_id=None,
        strategy_id=None, session_id='session', exc=error)

    assert len(turns) == 1 and turns[0]['aborted'] is True
    for record in [*records, *events, *turns]:
        assert_public(record['final_text'])
        assert '限流' in record['final_text']
        assert secret not in str(record)
    assert 'diagnostic-marker' in turns[0]['abort_reason']
    assert 'REDACTED' in turns[0]['abort_reason']


def test_partial_stream_replaced_by_summary_keeps_diagnostics_in_trace():
    from test_agent_loop_final_summary import _loop
    from nerya.core.errors import LLMError

    class Interrupted:
        def call_messages(self, **kwargs):
            error = LLMError('transport-diagnostic-marker api_key=sk-' + 'abc123' * 8)
            error.stream_interrupted = True
            error.stream_output_committed = True
            raise error

    result = _loop(Interrupted(), []).run(system='Test', user_message='完成任务')
    assert result.aborted and result.abort_reason == 'stream_interrupted'
    assert_public(result.final_text)
    transport = [env.block for env in result.blocks if env.block.get('kind') == 'model_transport']
    assert len(transport) == 1 and transport[0]['retryable'] is False
    assert 'transport-diagnostic-marker' in transport[0]['detail']
    assert 'abc123' not in transport[0]['detail']
