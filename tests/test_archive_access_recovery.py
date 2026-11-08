"""Real provider error -> native tool -> transcript event, no network."""
from nerya.agent.tool_projection import project_tool_results
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.uniswap_history import uniswap_specs
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.types import ToolCall


def test_archive_access_reason_survives_actual_tool_projection():
    class Denied:
        def request(self, method,url,**kwargs):
            return 403, {'jsonrpc':'2.0','id':kwargs['body']['id'],
                'error':{'code':-32602,'message':'raw private provider body'}}
    registry=DataApiRegistry()
    for spec in uniswap_specs(Denied()):
        registry.register_action(spec)
    result=data_api_handler(ToolCall(id='archive-call',name='data_api',arguments={
        'op':'call','provider':'uniswap','action':'pool_history','args':{
            'pool_address':'0x'+'11'*20,'start_block':1,'end_block':2,
            'step_blocks':1,'max_samples':2,'tick_lower':-120,'tick_upper':120}}),registry=registry)
    assert result.is_error and result.error.retryable is False
    projection=project_tool_results([result],
        render_tool_result=lambda item:{'type':'tool_result','content':item.text()},
        rendered_tool_result_text=lambda item:item.get('content'))
    event=projection.event_blocks[0]
    assert event['recovery']['reason']=='historical_rpc_authorization_required'
    assert event['recovery']['safe_to_retry'] is False
    assert event['recovery']['preserve_requested_constraints'] is True
    assert 'raw private' not in str(projection)
