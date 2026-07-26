"""Attachment normalisation for chat and gateway turns.

The agent loop speaks Anthropic-shaped content blocks internally. This
module accepts dashboard/gateway upload envelopes and turns them into
that canonical shape while keeping large bytes out of journals and
session rows.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..workspace.artifact_store import ArtifactStore


_TEXT_TYPES = {
    "application/json",
    "application/javascript",
    "application/typescript",
    "application/xml",
    "application/yaml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
    "text/yaml",
}
_FALLBACK_MULTIMODAL_PROVIDERS = {
    "anthropic",
    "claude",
    "gemini",
    "google",
    "openai",
    "openrouter",
    "bedrock",
}
_FALLBACK_PDF_PROVIDERS = {"anthropic", "claude", "gemini", "google", "bedrock"}


@dataclass
class PreparedAttachments:
    message: str | list[dict[str, Any]]
    attachments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def upload_chat_attachments(
    raw_attachments: Any,
    *,
    paths: Any,
    upload_id: str = "",
    max_files: int = 8,
    max_file_bytes: int = 8 * 1024 * 1024,
    max_total_bytes: int = 20 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Persist dashboard-selected attachments and return public metadata."""

    items = raw_attachments if isinstance(raw_attachments, list) else []
    if not items:
        return []
    upload_id = _safe_filename(upload_id or f"upload_{uuid.uuid4().hex[:12]}")
    metas: list[dict[str, Any]] = []
    total_bytes = 0

    for raw in items[:max_files]:
        if not isinstance(raw, dict):
            continue
        att = _coerce_attachment(raw, paths=paths, load_artifact=False)
        if att is None:
            continue
        raw_bytes = att.pop("_bytes", None)
        size = int(att.get("size") or 0)
        total_bytes += size
        if raw_bytes is None:
            att["uploaded"] = False
            att["reason"] = "missing_attachment_data"
            metas.append(_public_meta(att))
            continue
        if size > max_file_bytes:
            att["uploaded"] = False
            att["reason"] = "file_too_large"
            metas.append(_public_meta(att))
            continue
        if total_bytes > max_total_bytes:
            att["uploaded"] = False
            att["reason"] = "total_attachment_limit"
            metas.append(_public_meta(att))
            continue
        att["artifact_uri"] = _persist_attachment(
            paths=paths,
            turn_id=f"uploads/{upload_id}",
            name=str(att["name"]),
            data=raw_bytes,
        )
        att["uploaded"] = bool(att.get("artifact_uri"))
        if not att["uploaded"]:
            att["reason"] = "persist_failed"
        metas.append(_public_meta(att))

    return metas


def prepare_user_message(
    text: str,
    raw_attachments: Any,
    *,
    paths: Any,
    turn_id: str,
    provider: str = "",
    model_metadata: Any = None,
    max_files: int = 8,
    max_file_bytes: int = 8 * 1024 * 1024,
    max_total_bytes: int = 20 * 1024 * 1024,
) -> PreparedAttachments:
    """Return a user message suitable for ``WorkspaceNativeAgentLoop``.

    ``raw_attachments`` is expected to be a list of objects containing
    ``name``/``mime_type`` plus either ``data_url`` / base64 ``data`` /
    ``url`` / text ``content``. Unknown or unsupported files are kept as
    metadata but not sent to the LLM.
    """

    clean_text = str(text or "")
    items = raw_attachments if isinstance(raw_attachments, list) else []
    if not items:
        return PreparedAttachments(message=clean_text)

    modalities = set(getattr(model_metadata, "input_modalities", ()) or ())
    meta_source = str(getattr(model_metadata, "source", "") or "")
    provider = (provider or getattr(model_metadata, "provider", "") or "").lower()
    blocks: list[dict[str, Any]] = []
    metas: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_bytes = 0

    for raw in items[:max_files]:
        if not isinstance(raw, dict):
            continue
        att = _coerce_attachment(raw, paths=paths)
        if att is None:
            continue
        total_bytes += int(att.get("size") or 0)
        if int(att.get("size") or 0) > max_file_bytes:
            att["model_sent"] = False
            att["reason"] = "file_too_large"
            warnings.append(f"{att['name']} was not sent to the model: file too large.")
            metas.append(_public_meta(att))
            continue
        if total_bytes > max_total_bytes:
            att["model_sent"] = False
            att["reason"] = "total_attachment_limit"
            warnings.append("Attachment limit reached; remaining files were not sent.")
            metas.append(_public_meta(att))
            continue

        raw_bytes = att.pop("_bytes", None)
        if raw_bytes is not None and not att.get("artifact_uri"):
            att["artifact_uri"] = _persist_attachment(
                paths=paths,
                turn_id=turn_id,
                name=str(att["name"]),
                data=raw_bytes,
            )

        block = _content_block_for_attachment(
            att,
            modalities=modalities,
            meta_source=meta_source,
            provider=provider,
        )
        if block is None:
            att["model_sent"] = False
            att.setdefault("reason", "unsupported_by_selected_model")
            warnings.append(
                f"{att['name']} was attached but not sent to the model "
                f"({att['reason']})."
            )
        else:
            att["model_sent"] = True
            blocks.append(block)
        metas.append(_public_meta(att))

    if len(items) > max_files:
        warnings.append(f"Only the first {max_files} attachments were processed.")

    text_parts: list[str] = []
    if clean_text.strip():
        text_parts.append(clean_text)
    if warnings:
        text_parts.append("[attachment warnings]\n" + "\n".join(f"- {w}" for w in warnings))
    if text_parts:
        blocks.append({"type": "text", "text": "\n\n".join(text_parts)})
    if not blocks:
        blocks.append({"type": "text", "text": "(attachments received; no text prompt provided)"})
    return PreparedAttachments(message=blocks, attachments=metas, warnings=warnings)


def public_attachment_blocks_from_envelopes(
    envelopes: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Extract public attachment metadata from committed turn blocks."""

    out: list[dict[str, Any]] = []
    for env in envelopes or []:
        if not isinstance(env, dict):
            continue
        block = env.get("block") if isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or block.get("type") or "")
        if kind == "attachment":
            out.append(_public_meta(block))
            continue
        if kind == "tool_result":
            result = block.get("result")
            out.extend(_attachments_from_value(result))
    return _dedupe_public_attachments(out)


def assistant_attachment_block(block: dict[str, Any]) -> dict[str, Any]:
    """Convert a provider content block into the dashboard block shape."""

    mime = str(
        block.get("mime_type")
        or block.get("media_type")
        or _source(block).get("media_type")
        or ""
    )
    data = str(block.get("data") or _source(block).get("data") or "")
    url = str(block.get("url") or _source(block).get("url") or "")
    data_url = str(block.get("data_url") or "")
    if data and mime and not data_url:
        data_url = f"data:{mime};base64,{data}"
    attachment_kind = str(block.get("attachment_kind") or block.get("kind") or "")
    if not attachment_kind or attachment_kind in {
        "attachment",
        "image",
        "document",
        "file",
        "video",
        "audio",
    }:
        attachment_kind = _kind_for_mime(mime)
    return {
        "kind": "attachment",
        "attachment_kind": attachment_kind,
        "name": str(block.get("name") or block.get("title") or "attachment"),
        "mime_type": mime or "application/octet-stream",
        "size": int(block.get("size") or 0),
        "data_url": data_url,
        "url": url,
        "text": str(block.get("text") or ""),
        "source": str(block.get("source_kind") or "model"),
    }


def _coerce_attachment(
    raw: dict[str, Any],
    *,
    paths: Any = None,
    load_artifact: bool = True,
) -> dict[str, Any] | None:
    name = str(
        raw.get("name")
        or raw.get("filename")
        or raw.get("file_name")
        or raw.get("title")
        or "attachment"
    )
    name = _safe_filename(name)
    mime = str(
        raw.get("mime_type")
        or raw.get("media_type")
        or raw.get("content_type")
        or ""
    ).strip()
    if not mime or "/" not in mime:
        guessed, _ = mimetypes.guess_type(name)
        mime = guessed or "application/octet-stream"

    data_url = str(raw.get("data_url") or raw.get("data_uri") or "")
    encoded = str(
        raw.get("data")
        or raw.get("base64")
        or raw.get("content_b64")
        or raw.get("bytes_b64")
        or ""
    )
    url = str(raw.get("url") or raw.get("download_url") or raw.get("file_url") or "")
    artifact_uri = str(raw.get("artifact_uri") or "")
    text = raw.get("text") if isinstance(raw.get("text"), str) else raw.get("content")
    if isinstance(text, dict):
        text = ""

    raw_bytes: bytes | None = None
    if data_url:
        parsed_mime, encoded = _split_data_url(data_url)
        if parsed_mime:
            mime = parsed_mime
    if encoded:
        try:
            raw_bytes = base64.b64decode(encoded, validate=False)
        except Exception:
            raw_bytes = None
    elif artifact_uri and load_artifact and paths is not None:
        raw_bytes = _read_artifact_bytes(paths, artifact_uri)
        if raw_bytes is not None:
            encoded = base64.b64encode(raw_bytes).decode("ascii")
            if not isinstance(text, str) or not text:
                if mime.startswith("text/") or mime in _TEXT_TYPES:
                    text = raw_bytes.decode("utf-8", errors="replace")
    elif isinstance(text, str) and text:
        raw_bytes = text.encode("utf-8")

    size = int(raw.get("size") or (len(raw_bytes) if raw_bytes is not None else 0) or 0)
    if raw_bytes is None and not url and not artifact_uri:
        return None
    return {
        "id": str(raw.get("id") or f"att_{uuid.uuid4().hex[:12]}"),
        "name": name,
        "mime_type": mime,
        "size": size,
        "kind": _kind_for_mime(mime),
        "data": encoded if raw_bytes is not None else "",
        "url": url,
        "artifact_uri": artifact_uri,
        "text": text if isinstance(text, str) else "",
        "_bytes": raw_bytes,
    }


def _content_block_for_attachment(
    att: dict[str, Any],
    *,
    modalities: set[str],
    meta_source: str,
    provider: str,
) -> dict[str, Any] | None:
    mime = str(att.get("mime_type") or "application/octet-stream")
    kind = str(att.get("kind") or _kind_for_mime(mime))
    data = str(att.get("data") or "")
    url = str(att.get("url") or "")
    text = str(att.get("text") or "")
    name = str(att.get("name") or "attachment")

    if kind == "image":
        if not _supports_image(modalities, meta_source=meta_source, provider=provider):
            att["reason"] = "model_has_no_image_input"
            return None
        if data:
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
                "name": name,
            }
        if url:
            return {
                "type": "image",
                "source": {"type": "url", "url": url},
                "name": name,
            }
        return None

    if mime == "application/pdf":
        if not _supports_pdf(modalities, meta_source=meta_source, provider=provider):
            att["reason"] = "model_has_no_pdf_input"
            return None
        if data:
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data,
                },
                "title": name,
            }
        if url:
            return {
                "type": "document",
                "source": {"type": "url", "url": url},
                "title": name,
            }
        return None

    if text or mime.startswith("text/") or mime in _TEXT_TYPES:
        doc_text = text
        if not doc_text and data:
            try:
                doc_text = base64.b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                doc_text = ""
        if doc_text:
            return {
                "type": "text",
                "text": (
                    f"<attached_document name=\"{name}\" mime=\"{mime}\">\n"
                    f"{doc_text[:256_000]}\n</attached_document>"
                ),
            }

    att["reason"] = "binary_document_not_supported"
    return None


def _supports_image(modalities: set[str], *, meta_source: str, provider: str) -> bool:
    if "image" in modalities:
        return True
    return meta_source == "unknown" and provider in _FALLBACK_MULTIMODAL_PROVIDERS


def _supports_pdf(modalities: set[str], *, meta_source: str, provider: str) -> bool:
    if {"pdf", "document", "file"} & modalities:
        return True
    return meta_source == "unknown" and provider in _FALLBACK_PDF_PROVIDERS


def _persist_attachment(*, paths: Any, turn_id: str, name: str, data: bytes) -> str:
    safe_name = _safe_filename(name)
    prefix = "/".join(
        _safe_filename(part)
        for part in str(turn_id or "turn").replace("\\", "/").split("/")
        if part.strip()
    )
    artifact_name = f"{prefix or 'turn'}/{safe_name}"
    try:
        ArtifactStore(paths).put_bytes("attachments", artifact_name, data)
    except Exception:
        return ""
    return f"nerya://artifact/attachments/{artifact_name.replace(chr(92), '/')}"


def _read_artifact_bytes(paths: Any, uri: str) -> bytes | None:
    prefix = "nerya://artifact/"
    if not uri.startswith(prefix):
        return None
    rel = uri[len(prefix):].replace("\\", "/").strip("/")
    if not rel:
        return None
    parts = [part for part in rel.split("/") if part not in {"", ".", ".."}]
    if len(parts) < 2:
        return None
    target = (paths.artifacts / Path(*parts)).resolve()
    root = paths.artifacts.resolve()
    try:
        if not target.is_relative_to(root):
            return None
    except AttributeError:  # pragma: no cover - old Python fallback
        if root not in target.parents and target != root:
            return None
    try:
        return target.read_bytes()
    except Exception:
        return None


def _split_data_url(value: str) -> tuple[str, str]:
    if not value.startswith("data:"):
        return "", value
    header, _, data = value.partition(",")
    mime = "application/octet-stream"
    if ";" in header:
        mime = header[5:].split(";", 1)[0] or mime
    return mime, data


def _source(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    return source if isinstance(source, dict) else {}


def _kind_for_mime(mime: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf" or mime.startswith("text/") or mime in _TEXT_TYPES:
        return "document"
    return "file"


def _attachments_from_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        raw = value.get("attachments")
        if isinstance(raw, list):
            return [_public_meta(v) for v in raw if isinstance(v, dict)]
        if value.get("kind") in {
            "attachment",
            "image",
            "document",
            "file",
            "video",
            "audio",
        }:
            return [_public_meta(value)]
    return []


def _dedupe_public_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("id") or item.get("artifact_uri") or item.get("url") or item.get("name"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _public_meta(att: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "id": att.get("id"),
            "name": att.get("name") or att.get("title"),
            "mime_type": att.get("mime_type") or att.get("media_type"),
            "size": att.get("size"),
            "kind": att.get("kind") or att.get("attachment_kind"),
            "artifact_uri": att.get("artifact_uri"),
            "url": att.get("url"),
            "data_url": att.get("data_url"),
            "text": att.get("text"),
            "model_sent": att.get("model_sent"),
            "uploaded": att.get("uploaded"),
            "reason": att.get("reason"),
            "source": att.get("source"),
        }.items()
        if value not in (None, "")
    }


def _safe_filename(value: str) -> str:
    name = Path(value or "attachment").name.strip() or "attachment"
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:120] or "attachment"
