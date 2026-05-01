"""Minimal HTTP transport for message channels.

Kept thin on purpose — channels do one POST each. An injectable
``Transport`` protocol lets tests assert the exact outbound request
without hitting the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
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


__all__ = ["MessagingTransport", "UrllibMessagingTransport"]
