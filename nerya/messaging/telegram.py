"""Telegram channel — real delivery via the Bot API.

Config shape in ``workspace/messages/channels.yml``:

    channels:
      alerts:
        kind: telegram
        bot_token_ref: vault://telegram_bot_token   # required for live delivery
        chat_id: "123456789"                          # required
        parse_mode: HTML                              # optional
        disable_web_page_preview: true                # optional

If ``bot_token_ref`` (or the resolved secret) is missing the message is
still written to the outbox as a record, but marked ``delivered: false``.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Callable

from . import dashboard
from .markdown_telegram import render_markdown_for_telegram
from .transport import MessagingTransport, UrllibMessagingTransport


_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEND_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
_SEND_DOCUMENT_API = "https://api.telegram.org/bot{token}/sendDocument"
_GET_FILE_API = "https://api.telegram.org/bot{token}/getFile"
_FILE_DOWNLOAD_API = "https://api.telegram.org/file/bot{token}/{file_path}"
_SET_COMMANDS_API = "https://api.telegram.org/bot{token}/setMyCommands"
_GET_UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
_CHAT_ACTION_API = "https://api.telegram.org/bot{token}/sendChatAction"
_ANSWER_CALLBACK_API = "https://api.telegram.org/bot{token}/answerCallbackQuery"
_EDIT_REPLY_MARKUP_API = "https://api.telegram.org/bot{token}/editMessageReplyMarkup"
_GET_ME_API = "https://api.telegram.org/bot{token}/getMe"
_GET_CHAT_API = "https://api.telegram.org/bot{token}/getChat"
_MAX_MESSAGE_LENGTH = 4096
_SAFE_CHUNK_LENGTH = 3800
_MAX_TELEGRAM_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_TELEGRAM_DOCUMENT_BYTES = 50 * 1024 * 1024
_MAX_TELEGRAM_INBOUND_BYTES = 20 * 1024 * 1024
_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


def send(outbox_messages: Path, message: dict[str, Any], *,
         channel_cfg: dict[str, Any] | None = None,
         resolve_secret: Callable[[str], str | None] | None = None,
         transport: MessagingTransport | None = None) -> Path:
    message["channel"] = "telegram"
    cfg = channel_cfg or {}
    token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
    chat_id = cfg.get("chat_id")
    token = _resolve(token_ref, resolve_secret) if token_ref else None

    if not token or not chat_id:
        message["delivered"] = False
        message["delivery_note"] = "telegram: missing bot_token_ref or chat_id"
        return dashboard.send(outbox_messages, message)

    tx = transport or UrllibMessagingTransport()
    raw_text = message.get("text") or ""
    attachments = (
        message.get("attachments")
        if isinstance(message.get("attachments"), list)
        else []
    )
    # Render markdown as Telegram-friendly HTML by default. Operators can opt
    # out by setting
    # ``parse_mode: ""`` (force plain text), or pin a different mode
    # (e.g. ``Markdown`` / ``MarkdownV2``) in the channel config and
    # disable the converter via ``markdown: false``.
    parse_mode = cfg.get("parse_mode", "HTML")
    use_markdown_converter = bool(cfg.get("markdown", True)) and (
        str(parse_mode or "").upper() == "HTML"
    )
    text = (
        render_markdown_for_telegram(raw_text)
        if use_markdown_converter else raw_text
    )
    effective_parse_mode = parse_mode
    fallback_reason = ""
    if effective_parse_mode and len(text) > _MAX_MESSAGE_LENGTH:
        # Telegram parse modes cannot be split safely after HTML/Markdown
        # rendering without tracking open tags/entities. When a formatted
        # reply is too long, fall back to plain-text chunks so every chunk
        # stays valid and the full answer still gets delivered.
        text = raw_text
        effective_parse_mode = None
        fallback_reason = "plain text fallback: message split"

    base_body: dict[str, Any] = {"chat_id": str(chat_id)}
    if "disable_web_page_preview" in cfg:
        base_body["disable_web_page_preview"] = bool(cfg["disable_web_page_preview"])
    # inline approval keyboards. Callers can attach a
    # ``reply_markup`` directly on the message envelope; the messaging
    # pipeline will set it via ``approval_prompts.ApprovalPrompt``.
    reply_markup = message.get("reply_markup")
    if isinstance(reply_markup, dict) and reply_markup:
        base_body["reply_markup"] = reply_markup
    reply_to = message.get("reply_to_message_id")
    if reply_to is not None:
        base_body["reply_to_message_id"] = int(reply_to)

    text_sent = bool(raw_text.strip())
    if text_sent:
        ok, status, resp, message_ids = _post_message_chunks(
            tx=tx,
            token=token,
            base_body=base_body,
            text=text,
            parse_mode=effective_parse_mode,
        )
        if (
            not ok
            and effective_parse_mode
            and _looks_like_parse_error(status, resp)
        ):
            fallback_reason = "plain text fallback: parse failed"
            ok, status, resp, message_ids = _post_message_chunks(
                tx=tx,
                token=token,
                base_body=base_body,
                text=raw_text,
                parse_mode=None,
            )
    else:
        ok, status, resp, message_ids = True, 200, {"ok": True, "skipped": "empty_text"}, []

    attachment_response: dict[str, Any] | None = None
    if ok and attachments:
        media_ok, media_status, attachment_response, media_ids = _post_attachments(
            tx=tx,
            token=token,
            base_body=base_body,
            attachments=attachments,
            outbox_messages=outbox_messages,
            text_sent=text_sent,
        )
        ok = ok and media_ok
        status = media_status
        message_ids.extend(media_ids)
        if text_sent:
            resp = {
                "ok": ok,
                "message": resp,
                "attachments": attachment_response,
            }
        else:
            resp = attachment_response

    message["delivered"] = ok
    message["status"] = status
    message["platform_response"] = resp
    if message_ids:
        message["telegram_message_id"] = message_ids[0]
        message["telegram_message_ids"] = message_ids
    if ok:
        details = []
        if fallback_reason:
            details.append(fallback_reason)
        if attachment_response:
            count = int(attachment_response.get("sent_count") or 0)
            details.append(f"{count} attachment(s)")
        message["delivery_note"] = (
            f"telegram sent ({'; '.join(details)})"
            if details else "telegram sent"
        )
    else:
        message["delivery_note"] = f"telegram failed: {resp}"
    return dashboard.send(outbox_messages, message)


def _post_message_chunks(
    *,
    tx: MessagingTransport,
    token: str,
    base_body: dict[str, Any],
    text: str,
    parse_mode: Any,
) -> tuple[bool, int, dict[str, Any], list[Any]]:
    chunks = _split_text_chunks(text)
    responses: list[dict[str, Any]] = []
    statuses: list[int] = []
    message_ids: list[Any] = []
    for index, chunk in enumerate(chunks):
        body = dict(base_body)
        body["text"] = chunk
        if parse_mode:
            body["parse_mode"] = parse_mode
        if index > 0:
            body.pop("reply_markup", None)
            body.pop("reply_to_message_id", None)
        status, resp = tx.post(
            _API.format(token=token),
            headers={},
            body=body,
            timeout=10.0,
        )
        statuses.append(status)
        responses.append(resp)
        ok = 200 <= status < 300 and bool(resp.get("ok", True))
        if not ok:
            return False, status, _response_envelope(responses, statuses), message_ids
        result = resp.get("result")
        if isinstance(result, dict) and result.get("message_id") is not None:
            message_ids.append(result.get("message_id"))
    return True, (statuses[-1] if statuses else 0), _response_envelope(responses, statuses), message_ids


def _post_attachments(
    *,
    tx: MessagingTransport,
    token: str,
    base_body: dict[str, Any],
    attachments: list[dict[str, Any]],
    outbox_messages: Path,
    text_sent: bool,
) -> tuple[bool, int, dict[str, Any], list[Any]]:
    responses: list[dict[str, Any]] = []
    statuses: list[int] = []
    message_ids: list[Any] = []
    sent_count = 0
    for index, raw in enumerate(attachments):
        prepared = _telegram_attachment_payload(raw, outbox_messages=outbox_messages)
        if prepared.get("error"):
            responses.append({
                "ok": False,
                "attachment": _attachment_name(raw),
                "error": prepared["error"],
            })
            statuses.append(0)
            return False, 0, _attachment_response(responses, statuses, sent_count), message_ids

        body = dict(base_body)
        body.pop("reply_markup", None)
        if text_sent or index > 0:
            body.pop("reply_to_message_id", None)

        field = str(prepared["field"])
        body[field] = prepared["media"]
        api = str(prepared["api"]).format(token=token)
        file_payload = prepared.get("file")
        if file_payload is not None:
            body.pop(field, None)
            post_multipart = getattr(tx, "post_multipart", None)
            if not callable(post_multipart):
                responses.append({
                    "ok": False,
                    "attachment": prepared["name"],
                    "error": "transport_does_not_support_multipart",
                })
                statuses.append(0)
                return False, 0, _attachment_response(responses, statuses, sent_count), message_ids
            status, resp = post_multipart(
                api,
                headers={},
                fields=body,
                files={
                    field: (
                        str(prepared["name"]),
                        file_payload,
                        str(prepared["mime_type"]),
                    )
                },
                timeout=30.0,
            )
        else:
            status, resp = tx.post(api, headers={}, body=body, timeout=10.0)

        statuses.append(status)
        responses.append(resp)
        ok = 200 <= status < 300 and bool(resp.get("ok", True))
        if not ok:
            return False, status, _attachment_response(responses, statuses, sent_count), message_ids
        sent_count += 1
        result = resp.get("result")
        if isinstance(result, dict) and result.get("message_id") is not None:
            message_ids.append(result.get("message_id"))

    return True, (statuses[-1] if statuses else 200), _attachment_response(responses, statuses, sent_count), message_ids


def _telegram_attachment_payload(raw: dict[str, Any], *, outbox_messages: Path) -> dict[str, Any]:
    mime_type = str(
        raw.get("mime_type")
        or raw.get("media_type")
        or raw.get("content_type")
        or ""
    ).strip()
    name = _attachment_name(raw)
    if not mime_type or "/" not in mime_type:
        guessed, _ = mimetypes.guess_type(name)
        mime_type = guessed or "application/octet-stream"

    api, field, max_bytes = _telegram_media_target(raw, mime_type)
    url = str(raw.get("url") or raw.get("download_url") or raw.get("file_url") or "")
    if url.startswith(("http://", "https://")):
        return {
            "api": api,
            "field": field,
            "media": url,
            "name": name,
            "mime_type": mime_type,
        }

    data = _attachment_bytes(raw, outbox_messages=outbox_messages)
    if data is None:
        return {"error": "attachment_has_no_sendable_url_or_bytes"}
    if len(data) > max_bytes:
        return {"error": f"attachment_too_large_for_telegram_{field}"}
    return {
        "api": api,
        "field": field,
        "media": "attach://file",
        "file": data,
        "name": name,
        "mime_type": mime_type,
    }


def _telegram_media_target(raw: dict[str, Any], mime_type: str) -> tuple[str, str, int]:
    kind = str(raw.get("kind") or raw.get("attachment_kind") or "").lower()
    normalized_mime = mime_type.lower()
    if normalized_mime in _PHOTO_TYPES and kind in {"", "image"}:
        return _SEND_PHOTO_API, "photo", _MAX_TELEGRAM_PHOTO_BYTES
    return _SEND_DOCUMENT_API, "document", _MAX_TELEGRAM_DOCUMENT_BYTES


def _attachment_bytes(raw: dict[str, Any], *, outbox_messages: Path) -> bytes | None:
    data_url = str(raw.get("data_url") or raw.get("data_uri") or "")
    encoded = str(
        raw.get("data")
        or raw.get("base64")
        or raw.get("content_b64")
        or raw.get("bytes_b64")
        or ""
    )
    if data_url:
        _, _, encoded = data_url.partition(",")
    if encoded:
        try:
            return base64.b64decode(encoded, validate=False)
        except Exception:
            return None
    artifact_uri = str(raw.get("artifact_uri") or "")
    if artifact_uri:
        return _read_artifact_bytes(outbox_messages, artifact_uri)
    return None


def _read_artifact_bytes(outbox_messages: Path, uri: str) -> bytes | None:
    prefix = "nerya://artifact/"
    if not uri.startswith(prefix):
        return None
    rel = uri[len(prefix):].replace("\\", "/").strip("/")
    if not rel:
        return None
    parts = [part for part in rel.split("/") if part not in {"", ".", ".."}]
    if not parts:
        return None
    root = _workspace_root_from_outbox(outbox_messages)
    target = (root / "artifacts" / Path(*parts)).resolve()
    artifact_root = (root / "artifacts").resolve()
    try:
        if not target.is_relative_to(artifact_root):
            return None
    except AttributeError:  # pragma: no cover - Python < 3.9 fallback
        if artifact_root not in target.parents and target != artifact_root:
            return None
    try:
        return target.read_bytes()
    except Exception:
        return None


def _workspace_root_from_outbox(outbox_messages: Path) -> Path:
    path = Path(outbox_messages)
    if path.name == "messages" and path.parent.name == "outbox":
        return path.parent.parent
    return path


def _attachment_response(
    responses: list[dict[str, Any]],
    statuses: list[int],
    sent_count: int,
) -> dict[str, Any]:
    envelope = _response_envelope(responses, statuses) if responses else {"ok": True}
    envelope["sent_count"] = sent_count
    return envelope


def _attachment_name(raw: dict[str, Any]) -> str:
    return Path(
        str(
            raw.get("name")
            or raw.get("filename")
            or raw.get("file_name")
            or raw.get("title")
            or "attachment"
        )
    ).name or "attachment"


def _response_envelope(
    responses: list[dict[str, Any]],
    statuses: list[int],
) -> dict[str, Any]:
    if len(responses) == 1:
        return responses[0]
    return {
        "ok": all(200 <= status < 300 and bool(resp.get("ok", True))
                  for status, resp in zip(statuses, responses)),
        "statuses": statuses,
        "responses": responses,
    }


def _looks_like_parse_error(status: int, resp: dict[str, Any]) -> bool:
    if status != 400:
        return False
    detail = " ".join(
        str(resp.get(key) or "")
        for key in ("description", "error", "raw")
    ).lower()
    return "parse" in detail or "entity" in detail or "markdown" in detail


def _split_text_chunks(text: str, limit: int = _SAFE_CHUNK_LENGTH) -> list[str]:
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < 1:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _resolve(ref: str | None, resolver: Callable[[str], str | None] | None) -> str | None:
    if not ref:
        return None
    if resolver is None:
        return None
    return resolver(ref)


def _token(cfg: dict[str, Any], resolver: Callable[[str], str | None] | None) -> str | None:
    token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
    return _resolve(token_ref, resolver) if token_ref else None


def set_commands(*, channel_cfg: dict[str, Any] | None = None,
                 commands: list[dict[str, str]] | None = None,
                 resolve_secret: Callable[[str], str | None] | None = None,
                 transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    body = {"commands": commands or []}
    status, resp = tx.post(_SET_COMMANDS_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}


def get_updates(*, channel_cfg: dict[str, Any] | None = None,
                offset: int | None = None,
                limit: int = 10,
                timeout: int = 0,
                resolve_secret: Callable[[str], str | None] | None = None,
                transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref", "updates": []}
    body: dict[str, Any] = {"limit": int(limit), "timeout": int(timeout)}
    if offset is not None:
        body["offset"] = int(offset)
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(_GET_UPDATES_API.format(token=token), headers={}, body=body, timeout=max(10.0, float(timeout) + 5.0))
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "updates": list(resp.get("result") or []), "response": resp}


def download_inbound_file(*, channel_cfg: dict[str, Any] | None = None,
                          file_id: str,
                          resolve_secret: Callable[[str], str | None] | None = None,
                          transport: MessagingTransport | None = None,
                          max_bytes: int = _MAX_TELEGRAM_INBOUND_BYTES) -> dict[str, Any]:
    """Resolve a Telegram ``file_id`` and download the bytes for the agent."""

    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    file_id = str(file_id or "").strip()
    if not file_id:
        return {"ok": False, "error": "telegram: missing file_id"}
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(
        _GET_FILE_API.format(token=token),
        headers={},
        body={"file_id": file_id},
        timeout=10.0,
    )
    if not (200 <= status < 300 and bool(resp.get("ok", True))):
        return {"ok": False, "status": status, "response": resp, "error": "telegram getFile failed"}
    result = resp.get("result") if isinstance(resp, dict) else {}
    if not isinstance(result, dict):
        return {"ok": False, "status": status, "response": resp, "error": "telegram getFile returned no file"}
    file_size = int(result.get("file_size") or 0)
    if file_size > max_bytes:
        return {
            "ok": False,
            "status": status,
            "file_size": file_size,
            "error": "telegram file too large",
        }
    file_path = str(result.get("file_path") or "")
    if not file_path:
        return {"ok": False, "status": status, "response": resp, "error": "telegram file_path missing"}
    get_bytes = getattr(tx, "get_bytes", None)
    if not callable(get_bytes):
        return {"ok": False, "error": "transport_does_not_support_binary_get"}
    download_url = _FILE_DOWNLOAD_API.format(token=token, file_path=file_path)
    download_status, data, headers = get_bytes(
        download_url,
        headers={},
        timeout=30.0,
    )
    if not 200 <= download_status < 300:
        return {
            "ok": False,
            "status": download_status,
            "response": {"raw": data.decode("utf-8", errors="replace")},
            "error": "telegram file download failed",
        }
    if len(data) > max_bytes:
        return {
            "ok": False,
            "status": download_status,
            "file_size": len(data),
            "error": "telegram file too large",
        }
    return {
        "ok": True,
        "status": download_status,
        "file_id": file_id,
        "file_unique_id": result.get("file_unique_id"),
        "file_path": file_path,
        "file_size": file_size or len(data),
        "data": base64.b64encode(data).decode("ascii"),
        "content_type": headers.get("Content-Type") or headers.get("content-type"),
    }


def send_chat_action(*, channel_cfg: dict[str, Any] | None = None,
                     chat_id: str | None = None,
                     action: str = "typing",
                     resolve_secret: Callable[[str], str | None] | None = None,
                     transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    effective_chat_id = chat_id or cfg.get("chat_id")
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    if not effective_chat_id:
        return {"ok": False, "error": "telegram: missing chat_id"}
    tx = transport or UrllibMessagingTransport()
    body = {"chat_id": str(effective_chat_id), "action": action or "typing"}
    status, resp = tx.post(_CHAT_ACTION_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}


def answer_callback_query(*, channel_cfg: dict[str, Any] | None = None,
                          callback_query_id: str,
                          text: str | None = None,
                          show_alert: bool = False,
                          resolve_secret: Callable[[str], str | None] | None = None,
                          transport: MessagingTransport | None = None) -> dict[str, Any]:
    """acknowledge an inline-keyboard button press.

    Telegram requires every callback query to be answered within ~15s
    or the spinner stays on the user's screen. We expose a small helper
    that the gateway pipeline uses after dispatching ``approve`` /
    ``reject`` through :class:`ApprovalGate`.
    """
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    body: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
    if text:
        body["text"] = str(text)
    if show_alert:
        body["show_alert"] = True
    status, resp = tx.post(_ANSWER_CALLBACK_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}


def get_me(*, channel_cfg: dict[str, Any] | None = None,
           resolve_secret: Callable[[str], str | None] | None = None,
           transport: MessagingTransport | None = None) -> dict[str, Any]:
    """Probe the bot identity by calling Telegram's `getMe`.

    Operator-facing: a 200 response with ``ok=true`` proves the
    ``bot_token`` is valid and the bot is reachable. A 401/404 means the
    token is wrong or revoked.
    """
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(_GET_ME_API.format(token=token), headers={}, body={}, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    out = {"ok": ok, "status": status}
    result = resp.get("result") if isinstance(resp, dict) else None
    if isinstance(result, dict):
        out["bot"] = {
            "id": result.get("id"),
            "username": result.get("username"),
            "first_name": result.get("first_name"),
            "can_join_groups": result.get("can_join_groups"),
            "can_read_all_group_messages": result.get("can_read_all_group_messages"),
            "supports_inline_queries": result.get("supports_inline_queries"),
        }
    if not ok and isinstance(resp, dict):
        out["error"] = resp.get("description") or resp.get("error")
    return out


def get_chat(*, channel_cfg: dict[str, Any] | None = None,
             chat_id: str | None = None,
             resolve_secret: Callable[[str], str | None] | None = None,
             transport: MessagingTransport | None = None) -> dict[str, Any]:
    """Probe a chat the bot can see via Telegram's `getChat`.

    A 200 response proves the bot is a member of (or has DM history
    with) that ``chat_id``. A 400 with `chat not found` typically means
    the chat_id is wrong or the bot was never added.
    """
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    effective_chat_id = chat_id or cfg.get("chat_id")
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    if not effective_chat_id:
        return {"ok": False, "error": "telegram: missing chat_id"}
    tx = transport or UrllibMessagingTransport()
    body = {"chat_id": str(effective_chat_id)}
    status, resp = tx.post(_GET_CHAT_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    out: dict[str, Any] = {"ok": ok, "status": status}
    result = resp.get("result") if isinstance(resp, dict) else None
    if isinstance(result, dict):
        out["chat"] = {
            "id": result.get("id"),
            "type": result.get("type"),
            "title": result.get("title"),
            "username": result.get("username"),
            "first_name": result.get("first_name"),
            "last_name": result.get("last_name"),
        }
    if not ok and isinstance(resp, dict):
        out["error"] = resp.get("description") or resp.get("error")
    return out


def clear_reply_markup(*, channel_cfg: dict[str, Any] | None = None,
                       chat_id: str | int,
                       message_id: str | int,
                       resolve_secret: Callable[[str], str | None] | None = None,
                       transport: MessagingTransport | None = None) -> dict[str, Any]:
    """Remove inline buttons from a Telegram message."""
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(
        _EDIT_REPLY_MARKUP_API.format(token=token),
        headers={},
        body={
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "reply_markup": {"inline_keyboard": []},
        },
        timeout=10.0,
    )
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}
