from __future__ import annotations

import base64

import pytest

from nerya.agent.attachments import prepare_user_message, upload_chat_attachments
from nerya.core.paths import WorkspacePaths
from nerya.llm.messages import (
    _gemini_parse_response,
    _gemini_render_contents,
    _openai_parse_response,
    _openai_render_messages,
)
from nerya.llm.model_registry import ModelMetadata


pytestmark = pytest.mark.smoke


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def test_prepare_user_message_sends_image_for_vision_model(tmp_path):
    metadata = ModelMetadata(
        id="vision-model",
        provider="openai",
        input_modalities=("text", "image"),
    )

    prepared = prepare_user_message(
        "describe this",
        [
            {
                "id": "img_1",
                "name": "chart.png",
                "mime_type": "image/png",
                "data_url": _data_url("image/png", b"fake-png"),
            }
        ],
        paths=WorkspacePaths(tmp_path),
        turn_id="turn-1",
        provider="openai",
        model_metadata=metadata,
    )

    assert isinstance(prepared.message, list)
    image_block = prepared.message[0]
    assert image_block["type"] == "image"
    assert image_block["source"]["media_type"] == "image/png"
    assert prepared.attachments[0]["model_sent"] is True
    assert prepared.attachments[0]["artifact_uri"].startswith(
        "nerya://artifact/attachments/turn-1/chart.png"
    )


def test_prepare_user_message_does_not_send_pdf_to_text_only_model(tmp_path):
    metadata = ModelMetadata(
        id="text-model",
        provider="mock",
        input_modalities=("text",),
        source="builtin",
    )

    prepared = prepare_user_message(
        "read this",
        [
            {
                "name": "report.pdf",
                "mime_type": "application/pdf",
                "data_url": _data_url("application/pdf", b"%PDF-1.4"),
            }
        ],
        paths=WorkspacePaths(tmp_path),
        turn_id="turn-2",
        provider="mock",
        model_metadata=metadata,
    )

    assert isinstance(prepared.message, list)
    assert all(block.get("type") != "document" for block in prepared.message)
    assert prepared.attachments[0]["model_sent"] is False
    assert prepared.attachments[0]["reason"] == "model_has_no_pdf_input"


def test_uploaded_artifact_metadata_can_be_sent_to_model(tmp_path):
    paths = WorkspacePaths(tmp_path)
    uploaded = upload_chat_attachments(
        [
            {
                "id": "img_upload",
                "name": "chart.png",
                "mime_type": "image/png",
                "data_url": _data_url("image/png", b"fake-png"),
            }
        ],
        paths=paths,
        upload_id="chat-1",
    )
    assert uploaded[0]["uploaded"] is True
    assert uploaded[0]["artifact_uri"].startswith(
        "nerya://artifact/attachments/uploads/chat-1/chart.png"
    )
    assert "data_url" not in uploaded[0]

    prepared = prepare_user_message(
        "describe this",
        uploaded,
        paths=paths,
        turn_id="turn-4",
        provider="openai",
        model_metadata=ModelMetadata(
            id="vision-model",
            provider="openai",
            input_modalities=("text", "image"),
        ),
    )

    assert isinstance(prepared.message, list)
    image_block = prepared.message[0]
    assert image_block["type"] == "image"
    assert image_block["source"]["data"] == base64.b64encode(b"fake-png").decode("ascii")
    assert prepared.attachments[0]["artifact_uri"] == uploaded[0]["artifact_uri"]


def test_openai_and_gemini_render_user_image_parts(tmp_path):
    metadata = ModelMetadata(
        id="vision-model",
        provider="openai",
        input_modalities=("text", "image"),
    )
    prepared = prepare_user_message(
        "what is this?",
        [
            {
                "name": "chart.png",
                "mime_type": "image/png",
                "data_url": _data_url("image/png", b"fake-png"),
            }
        ],
        paths=WorkspacePaths(tmp_path),
        turn_id="turn-3",
        provider="openai",
        model_metadata=metadata,
    )
    message = {"role": "user", "content": prepared.message}

    openai_messages = _openai_render_messages(system="", messages=[message])
    openai_content = openai_messages[0]["content"]
    assert isinstance(openai_content, list)
    assert any(part.get("type") == "image_url" for part in openai_content)

    gemini_messages = _gemini_render_contents([message])
    gemini_parts = gemini_messages[0]["parts"]
    assert any("inlineData" in part for part in gemini_parts)


def test_model_returned_images_parse_as_attachment_blocks():
    openai_blocks, _ = _openai_parse_response(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [
                            {
                                "type": "output_image",
                                "image_url": {
                                    "url": "data:image/png;base64,ZmFrZQ==",
                                },
                            }
                        ]
                    },
                }
            ]
        }
    )
    assert openai_blocks[0]["type"] == "attachment"
    assert openai_blocks[0]["attachment_kind"] == "image"

    gemini_blocks, _ = _gemini_parse_response(
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": "ZmFrZQ==",
                                }
                            }
                        ]
                    },
                }
            ]
        }
    )
    assert gemini_blocks[0]["type"] == "attachment"
    assert gemini_blocks[0]["data_url"] == "data:image/png;base64,ZmFrZQ=="
