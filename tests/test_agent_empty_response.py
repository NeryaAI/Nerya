"""Generic empty-provider-response recovery; no strategy-specific routing."""
import pytest
from test_agent_loop_final_summary import _Gateway, _response, _tool_use, _descriptor, _loop, _json_result
from nerya.tools.types import RiskLevel

pytestmark = pytest.mark.smoke

def test_empty_after_read_continues_in_same_turn_without_replaying_writes():
    seen = []
    def handler(call):
        seen.append(call.name)
        return _json_result(call, {'ok':True})
    gateway = _Gateway(
        _response(_tool_use('read_reference',call_id='read'),stop_reason='tool_use'),
        _response(),
        _response(_tool_use('write_candidate',call_id='write'),stop_reason='tool_use'),
        _response(),
        _response({'type':'text','text':'Created and verified.'}),
    )
    loop = _loop(gateway, [_descriptor('read_reference',handler), _descriptor('write_candidate',handler,risk=RiskLevel.WRITE,read_only=False)], max_iterations=8)
    out = loop.run(system='system',user_message='Create the requested artifact.')
    assert not out.aborted and out.final_text == 'Created and verified.'
    assert seen == ['read_reference','write_candidate']
    assert len(gateway.calls) == 5

def test_persistently_empty_provider_is_bounded_and_not_success():
    gateway = _Gateway(_response(),_response(),_response())
    out = _loop(gateway,[],max_iterations=30).run(system='system',user_message='Do the requested work.')
    assert out.aborted and out.abort_reason == 'empty_model_response'
    assert len(gateway.calls) == 3

def test_empty_response_respects_small_iteration_limit():
    gateway = _Gateway(_response())
    out = _loop(gateway,[],max_iterations=1).run(system='system',user_message='Do the requested work.')
    assert out.aborted and len(gateway.calls) == 1


@pytest.mark.parametrize('provider_stop', ['max_tokens', 'length'])
def test_repeated_truncated_reply_is_not_reported_as_success(provider_stop):
    gateway = _Gateway(
        _response({'type': 'text', 'text': 'Partial response.'}, stop_reason=provider_stop),
        _response({'type': 'text', 'text': 'Still incomplete.'}, stop_reason=provider_stop),
    )
    result = _loop(gateway, [], max_iterations=8).run(system='fixture', user_message='Explain in full.')
    assert result.aborted and result.abort_reason == 'model_output_truncated'
    assert '模型输出达到长度限制' in result.final_text
    assert len(gateway.calls) == 2


def test_content_filtered_reply_fails_with_actionable_reason():
    gateway = _Gateway(_response(stop_reason='content_filter'))
    result = _loop(gateway, [], max_iterations=4).run(system='fixture', user_message='Please answer.')
    assert result.aborted and result.abort_reason == 'provider_content_filtered'
    assert '模型服务过滤了本轮输出' in result.final_text
    assert len(gateway.calls) == 1
