from __future__ import annotations

import http.client

import pytest

from nerya.core.errors import LLMError
from nerya.llm.adapters._base import UrllibTransport
from nerya.llm.retry import is_retryable_status, retry_call


pytestmark = pytest.mark.smoke


def test_urllib_transport_wraps_remote_disconnect(monkeypatch):
    def fail(*args, **kwargs):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr("urllib.request.urlopen", fail)

    with pytest.raises(LLMError, match="network error calling provider"):
        UrllibTransport().post_json(
            "https://example.invalid/v1/chat/completions",
            headers={},
            body={"messages": []},
            timeout=1,
        )


def test_provider_peak_busy_status_is_retryable() -> None:
    calls = 0
    sleeps: list[float] = []

    def request() -> tuple[int, dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 529, {"error": {"message": "server temporarily busy"}}
        return 200, {"ok": True}

    status, body = retry_call(
        request,
        max_attempts=2,
        base_delay=0,
        max_delay=0,
        sleep=sleeps.append,
    )

    assert is_retryable_status(529) is True
    assert calls == 2
    assert sleeps == [0]
    assert status == 200
    assert body == {"ok": True}
