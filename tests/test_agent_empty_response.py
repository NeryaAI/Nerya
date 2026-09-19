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
