"""Current sampled-LP continuation at the offline scope-extraction seam."""
from copy import deepcopy
import json

import pytest

from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, env, spans_output  # noqa: F401


@pytest.mark.smoke
def test_sampled_lp_corrects_nonidentity_pool_once_without_candidate_authority(env):
    # Admitted texts from command e8bfe5ad's persisted scope snapshot. Neither
    # the producer-selected pool/window nor model-selected ticks occur here.
    texts = [
        '创建一个 Ethereum Uniswap V3 WETH/USDC LP 策略，复用已有历史数据做 paper 回测并生成报告，不交易。',
        '0.05%（500）',
        '允许固定 tick 区间采样研究回测',
        '继续完成当前任务，不交易。',
        '主机已更新，请按原池和已确认的研究范围恢复，继续完成任务，不交易。',
        '用户未指定的固定区间研究参数由你提出合理假设；需要确认时给出可点击选项。继续原任务，不交易。',
        '继续完成当前 LP 任务：已有同池历史采样后，按固定区间研究路径创建候选、验证并生成受限研究报告；保留部分覆盖限制，不交易。',
    ]
    documents = [dict(command_id=f'lp-{i}', text=text, created_at=1793921442 + i,
                      request_hash=f'request-{i}') for i, text in enumerate(texts)]
    snapshot = {'binding': {'command_id': 'lp-6', 'original_command_id': 'lp-0',
        'session_id': 'lp-session', 'actor_id': 'owner', 'documents': documents},
        'task_context': True, 'scope_answers': ['lp-1', 'lp-2'],
        'request': {'model_provider': 'openai', 'model_id': 'fixture-model'}}
    snapshot['input_hash'] = scope._hash(snapshot['binding'])
    initial = spans_output(documents[0], chain='Ethereum', protocol='Uniswap V3',
                           base='WETH', quote='USDC')
    prior = scope._chain([initial], scope._prefix(snapshot, 1))
    before = deepcopy((snapshot, prior))
    # The retained event locates value_for's pool-identifier rejection; the
    # original model span was not retained. Recreate that exact host branch
    # with an admitted fee answer, not an invented historical model response.
    malformed = spans_output(documents[1], pool=texts[1])['fields']['pool']
    candidate_pool = '0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640'

    for correction in ('unchanged_scope', 'still_invalid', 'candidate_address'):
        def choose(docs):
            evidence = deepcopy(initial)
            if len(gateway.calls) == 1 or correction == 'still_invalid':
                evidence['fields']['pool'] = deepcopy(malformed)
            elif correction == 'candidate_address':
                evidence['fields']['pool'] = {**malformed, 'text': candidate_pool}
            return {'extractions': [dict(command_id=doc['command_id'], evidence=deepcopy(evidence))
                                    for doc in docs[1:]]}

        gateway = FakeGateway(choose)
        def extract():
            return scope._extract(env, snapshot, gateway=gateway, original=prior,
                                  previous=prior, task_tail=[initial])

        if correction == 'unchanged_scope':
            result = extract()
            assert result['constraints'] == prior['constraints']
            assert len(result['extractions']) == len(documents)
            assert result['binding'] == snapshot['binding']
            assert 'pool' not in result['constraints']
            assert 'start_utc' not in result['constraints']
            assert all(item['fields']['pool'] is None for item in result['extractions'])
        else:
            with pytest.raises(scope.ScopeError) as rejected:
                extract()
            if correction == 'still_invalid':
                assert rejected.value.diagnostic_metadata == {
                    'field': 'pool', 'reason': 'invalid_pool_identity',
                    'source_document_index': 1}
            else:
                assert rejected.value.diagnostic_code == 'invalid_span'

        assert len(gateway.calls) == 2
        first, second = gateway.calls
        assert first['messages'] == second['messages']
        assert first['tools'] == second['tools']
        assert first['model_id'] == second['model_id']
        assert json.loads(second['messages'][0]['content']) == documents
        assert 'invalid_pool_identity' in second['system']
        assert 'fee tier' in second['system']
        assert candidate_pool not in second['system']
        assert (snapshot, prior) == before
        assert not env.paths.proposals.exists()
