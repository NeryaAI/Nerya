"""Regression coverage for OpenAI-compatible provider reasoning delimiters."""

from nerya.llm.messages import MessagesRequest, _openai_parse_response
import pytest

from test_managed_io_polish import StreamTransport, backend, delta, finish

pytestmark = pytest.mark.smoke


def test_stream_moves_split_think_block_to_thought_channel():
    events = []
    response = backend(StreamTransport([
        delta(content="<thi"),
        delta(content="nk>先看"),
        delta(content="账户。</thi"),
        delta(content="nk>确定绑定。"),
        finish(),
    ]))(MessagesRequest(system="fixture", messages=[], stream=True, on_event=events.append))

    assert response.text() == "确定绑定。"
    assert response.content == [
        {"type": "thinking", "thinking": "先看账户。"},
        {"type": "text", "text": "确定绑定。"},
    ]
    assert "".join(e["text"] for e in events if e["type"] == "thinking_delta") == "先看账户。"
    assert "</think>" not in "".join(e["text"] for e in events if e["type"] == "text_delta")


def test_provider_reasoning_content_closing_delimiter_stays_out_of_stream_body():
    events = []
    response = backend(StreamTransport([
        delta(reasoning_content="确定 EVM 账户的 wallet_id 绑定。"),
        delta(content="</thi"),
        delta(content="nk>让我检查连接器。"),
        finish(),
    ]))(MessagesRequest(system="fixture", messages=[], stream=True, on_event=events.append))

    assert response.text() == "让我检查连接器。"
    assert response.content[0] == {
        "type": "thinking",
        "thinking": "确定 EVM 账户的 wallet_id 绑定。",
    }
    assert "</think>" not in "".join(e["text"] for e in events if e["type"] == "text_delta")


def test_provider_reasoning_and_leading_inline_thought_keep_stream_and_final_consistent():
    events = []
    response = backend(StreamTransport([
        delta(reasoning_content="独立思考。"),
        delta(content="<thi"),
        delta(content="nk>补充分析。</thi"),
        delta(content="nk>公开结论。"),
        finish(),
    ]))(MessagesRequest(system="fixture", messages=[], stream=True, on_event=events.append))

    assert response.text() == "公开结论。"
    assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "公开结论。"
    assert "补充分析。" in "".join(e["text"] for e in events if e["type"] == "thinking_delta")


def test_collected_provider_reasoning_content_closing_delimiter_stays_out_of_body():
    blocks, stop_reason = _openai_parse_response({
        "choices": [{
            "message": {
                "reasoning_content": "先确定 wallet_id。",
                "content": "</think>检查连接器。",
            },
            "finish_reason": "stop",
        }]
    })

    assert stop_reason == "end_turn"
    assert blocks == [
        {"type": "thinking", "thinking": "先确定 wallet_id。"},
        {"type": "text", "text": "检查连接器。"},
    ]


def test_interrupted_stream_keeps_received_thought_and_never_promotes_delimiter():
    events = []
    transport = StreamTransport([
        delta(reasoning_content="已检查账户绑定。"),
        delta(content="</think>未完成的公开摘要"),
    ], fail=True)

    try:
        backend(transport)(MessagesRequest(
            system="fixture", messages=[], stream=True, on_event=events.append,
        ))
    except Exception as exc:
        assert getattr(exc, "stream_interrupted", False) is True
    else:
        raise AssertionError("fixture stream must interrupt")

    assert "已检查账户绑定。" == "".join(
        e["text"] for e in events if e["type"] == "thinking_delta"
    )
    public = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert public == "未完成的公开摘要"
    assert "</think>" not in public


def test_think_markup_without_provider_reasoning_stays_public():
    blocks, _ = _openai_parse_response({
        "choices": [{
            "message": {"content": '代码字符串 "<think>quoted</think>" 应保持原样。'},
            "finish_reason": "stop",
        }]
    })

    assert blocks == [{
        "type": "text",
        "text": '代码字符串 "<think>quoted</think>" 应保持原样。',
    }]
