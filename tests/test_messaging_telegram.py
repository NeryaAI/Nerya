from __future__ import annotations

from nerya.messaging import telegram
from nerya.messaging.markdown_telegram import render_markdown_for_telegram


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.multipart_calls: list[dict] = []
        self.byte_responses: list[tuple[int, bytes, dict[str, str]]] = []
        self.byte_calls: list[dict] = []

    def post(self, url, *, headers, body, timeout):  # noqa: ANN001
        self.calls.append({
            "url": url,
            "headers": headers,
            "body": dict(body),
            "timeout": timeout,
        })
        if self.responses:
            return self.responses.pop(0)
        return 200, {
            "ok": True,
            "result": {"message_id": len(self.calls)},
        }

    def post_multipart(self, url, *, headers, fields, files, timeout):  # noqa: ANN001
        self.multipart_calls.append({
            "url": url,
            "headers": headers,
            "fields": dict(fields),
            "files": dict(files),
            "timeout": timeout,
        })
        if self.responses:
            return self.responses.pop(0)
        return 200, {
            "ok": True,
            "result": {"message_id": len(self.calls) + len(self.multipart_calls)},
        }

    def get_bytes(self, url, *, headers, timeout):  # noqa: ANN001
        self.byte_calls.append({
            "url": url,
            "headers": headers,
            "timeout": timeout,
        })
        if self.byte_responses:
            return self.byte_responses.pop(0)
        return 200, b"downloaded-bytes", {"Content-Type": "image/jpeg"}


def _resolver(ref: str) -> str | None:
    if ref == "vault://telegram_bot_token":
        return "123456:test-token"
    return None


def test_markdown_renderer_does_not_apply_italic_inside_code_tags():
    text = (
        "- `portfolio_summary` 快照时间：**2026-05-07 18:25 UTC** 附近\n"
        "- `strategy_history` / `strategy_run_history`：主要看了 "
        "**btc_scalp_1m_pure / btc_trend_15m**"
    )

    rendered = render_markdown_for_telegram(text)

    assert "<code>portfolio_summary</code>" in rendered
    assert "<code>strategy_history</code>" in rendered
    assert "<code>strategy_run_history</code>" in rendered
    assert "<i>summary</code>" not in rendered
    assert "</i>history</code>" not in rendered


def test_markdown_renderer_preserves_identifier_underscores():
    rendered = render_markdown_for_telegram(
        "Strategy: btc_scalp_1m_pure\nField: strategy_run_history"
    )

    assert "btc_scalp_1m_pure" in rendered
    assert "strategy_run_history" in rendered
    assert "<i>scalp</i>" not in rendered
    assert "<i>run</i>" not in rendered


def test_telegram_send_falls_back_to_plain_text_on_parse_error(tmp_path):
    tx = FakeTransport([
        (
            400,
            {
                "ok": False,
                "description": "Bad Request: can't parse entities",
            },
        ),
        (200, {"ok": True, "result": {"message_id": 42}}),
    ])
    msg = {
        "message_id": "msg-test",
        "text": "**hello** `portfolio_summary`",
    }

    telegram.send(
        tmp_path,
        msg,
        channel_cfg={
            "bot_token_ref": "vault://telegram_bot_token",
            "chat_id": "7457389323",
            "parse_mode": "HTML",
        },
        resolve_secret=_resolver,
        transport=tx,
    )

    assert msg["delivered"] is True
    assert msg["telegram_message_id"] == 42
    assert "plain text fallback: parse failed" in msg["delivery_note"]
    assert tx.calls[0]["body"]["parse_mode"] == "HTML"
    assert "parse_mode" not in tx.calls[1]["body"]
    assert tx.calls[1]["body"]["text"] == msg["text"]


def test_telegram_send_splits_long_agent_reply_as_plain_text(tmp_path):
    tx = FakeTransport()
    text = "\n".join(f"- line {i}: `strategy_history` **status**" for i in range(220))
    msg = {"message_id": "msg-long", "text": text}

    telegram.send(
        tmp_path,
        msg,
        channel_cfg={
            "bot_token_ref": "vault://telegram_bot_token",
            "chat_id": "7457389323",
            "parse_mode": "HTML",
        },
        resolve_secret=_resolver,
        transport=tx,
    )

    assert msg["delivered"] is True
    assert len(tx.calls) > 1
    assert len(msg["telegram_message_ids"]) == len(tx.calls)
    assert "message split" in msg["delivery_note"]
    assert all("parse_mode" not in call["body"] for call in tx.calls)
    assert all(len(call["body"]["text"]) <= 3800 for call in tx.calls)
    sent = "\n".join(call["body"]["text"] for call in tx.calls)
    assert "- line 0:" in sent
    assert "- line 219:" in sent


def test_telegram_send_sends_image_attachment_url_as_photo(tmp_path):
    tx = FakeTransport()
    msg = {
        "message_id": "msg-image",
        "text": "Chart ready",
        "attachments": [
            {
                "name": "chart.png",
                "kind": "image",
                "mime_type": "image/png",
                "url": "https://example.test/chart.png",
            }
        ],
    }

    telegram.send(
        tmp_path,
        msg,
        channel_cfg={
            "bot_token_ref": "vault://telegram_bot_token",
            "chat_id": "7457389323",
        },
        resolve_secret=_resolver,
        transport=tx,
    )

    assert msg["delivered"] is True
    assert len(tx.calls) == 2
    assert tx.calls[0]["url"].endswith("/sendMessage")
    assert tx.calls[1]["url"].endswith("/sendPhoto")
    assert tx.calls[1]["body"]["photo"] == "https://example.test/chart.png"
    assert msg["telegram_message_ids"] == [1, 2]
    assert "1 attachment(s)" in msg["delivery_note"]


def test_telegram_send_uploads_artifact_document_as_multipart(tmp_path):
    outbox = tmp_path / "outbox" / "messages"
    artifact = tmp_path / "artifacts" / "attachments" / "turn-1" / "report.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"%PDF-1.4\nfixture")
    tx = FakeTransport()
    msg = {
        "message_id": "msg-doc",
        "text": "",
        "attachments": [
            {
                "name": "report.pdf",
                "kind": "document",
                "mime_type": "application/pdf",
                "artifact_uri": "nerya://artifact/attachments/turn-1/report.pdf",
            }
        ],
    }

    telegram.send(
        outbox,
        msg,
        channel_cfg={
            "bot_token_ref": "vault://telegram_bot_token",
            "chat_id": "7457389323",
        },
        resolve_secret=_resolver,
        transport=tx,
    )

    assert msg["delivered"] is True
    assert tx.calls == []
    assert len(tx.multipart_calls) == 1
    call = tx.multipart_calls[0]
    assert call["url"].endswith("/sendDocument")
    assert call["fields"]["chat_id"] == "7457389323"
    filename, data, mime_type = call["files"]["document"]
    assert filename == "report.pdf"
    assert data == b"%PDF-1.4\nfixture"
    assert mime_type == "application/pdf"
    assert msg["telegram_message_ids"] == [1]


def test_telegram_download_inbound_file_resolves_file_id_to_bytes():
    tx = FakeTransport([
        (
            200,
            {
                "ok": True,
                "result": {
                    "file_id": "photo-file",
                    "file_unique_id": "unique-photo",
                    "file_path": "photos/file_1.jpg",
                    "file_size": 16,
                },
            },
        )
    ])
    tx.byte_responses.append((200, b"image-bytes", {"Content-Type": "image/jpeg"}))

    result = telegram.download_inbound_file(
        channel_cfg={"bot_token_ref": "vault://telegram_bot_token"},
        file_id="photo-file",
        resolve_secret=_resolver,
        transport=tx,
    )

    assert result["ok"] is True
    assert result["file_path"] == "photos/file_1.jpg"
    assert result["content_type"] == "image/jpeg"
    assert result["data"]
    assert tx.calls[0]["url"].endswith("/getFile")
    assert tx.calls[0]["body"] == {"file_id": "photo-file"}
    assert tx.byte_calls[0]["url"].endswith("/photos/file_1.jpg")
