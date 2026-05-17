"""Minimal HTTP transport for message channels.

Kept thin on purpose — channels do one POST each. An injectable
``Transport`` protocol lets tests assert the exact outbound request
without hitting the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any, Protocol


class MessagingTransport(Protocol):
    def post(self, url: str, *, headers: dict[str, str], body: dict[str, Any],
              timeout: float) -> tuple[int, dict[str, Any]]: ...


class UrllibMessagingTransport:
    """Default transport using :mod:`urllib`. Returns ``(status, json_body)``."""

    def post(self, url: str, *, headers: dict[str, str], body: dict[str, Any],
              timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8")
        hdrs = {"Content-Type": "application/json", **headers}
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return resp.status, {"raw": raw}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}
        except urllib.error.URLError as exc:
            return 0, {"error": str(exc)}

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float = 10.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except urllib.error.URLError as exc:
            return 0, str(exc).encode("utf-8"), {}

    def post_multipart(
        self,
        url: str,
        *,
        headers: dict[str, str],
        fields: dict[str, Any],
        files: dict[str, tuple[str, bytes, str]],
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, Any]]:
        boundary = f"nerya-{uuid.uuid4().hex}"
        body = _multipart_body(boundary=boundary, fields=fields, files=files)
        hdrs = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **headers,
        }
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return resp.status, {"raw": raw}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}
        except urllib.error.URLError as exc:
            return 0, {"error": str(exc)}


def _multipart_body(
    *,
    boundary: str,
    fields: dict[str, Any],
    files: dict[str, tuple[str, bytes, str]],
) -> bytes:
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{_quote_header(str(key))}"\r\n\r\n'.encode("utf-8"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    for key, (filename, data, mime_type) in files.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("utf-8"),
            (
                "Content-Disposition: form-data; "
                f'name="{_quote_header(str(key))}"; '
                f'filename="{_quote_header(filename)}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8"),
            data,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def _quote_header(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["MessagingTransport", "UrllibMessagingTransport"]
