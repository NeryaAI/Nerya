"""Cancellable WebSocket reads for reviewed strategy feeds."""
from __future__ import annotations

import json
import socket
import time
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..security.web_safety import WebPolicy, evaluate_url, _host_in_list, _is_private_host
from .agent_task import StrategyAgentTask


def _connect(config: Any, spec: dict[str, Any]):
    from websockets.sync.client import connect
    url = str(spec["url"])
    parts = urlsplit(url)
    policy = WebPolicy.load_from_file(config.paths.root / "security" / "web_policy.yml")
    mapped = urlunsplit(("https" if parts.scheme == "wss" else "http", parts.netloc, parts.path, parts.query, ""))
    decision = evaluate_url(mapped, policy=policy)
    if not decision.is_allowed():
        raise PermissionError(f"stream URL denied: {decision.reason}")
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    private_allowed = policy.allow_private_hosts or _host_in_list(parts.hostname or "", policy.allow_hosts)
    for _, _, _, _, address in addresses:
        ip = address[0]
        check = evaluate_url(f"http://[{ip}]/" if ":" in ip else f"http://{ip}/", policy=replace(policy, allow_hosts=[], allowed_schemes=["http"], allow_private_hosts=private_allowed))
        if not check.is_allowed() or (_is_private_host(ip)[0] and not private_allowed):
            raise PermissionError("stream DNS address denied by network policy")
    if not addresses:
        raise OSError("stream DNS returned no addresses")
    sock = socket.create_connection(addresses[0][4][:2], timeout=5)
    try:
        return connect(url, sock=sock, proxy=None, open_timeout=5, close_timeout=1,
                       ping_interval=20, ping_timeout=20, compression=None,
                       max_size=int(spec.get("max_message_bytes", 262144)), max_queue=16)
    except BaseException:
        sock.close()
        raise


class StrategyStream:
    """Stop-aware SDK; events are queued separately from model execution."""
    def __init__(self, *, config: Any, settings: Any, token: Any, enqueue: Any, update: Any, inputs: Any):
        self._config, self._settings, self._token = config, settings, token
        self._enqueue, self._update, self._inputs = enqueue, update, inputs

    @property
    def stopping(self) -> bool:
        return self._token.is_set

    def wait(self, seconds: float) -> bool:
        return self._token.wait(max(0.0, min(float(seconds), 3600)))

    def dispatch(self, task: StrategyAgentTask, *, event_id: str, observed_at: float) -> dict[str, Any]:
        self._token.raise_if_cancelled()
        return self._enqueue(task, event_id, observed_at, self._inputs.snapshot())

    def websocket(self, source_id: str):
        if source_id not in self._settings.streams:
            raise ValueError("stream must name a reviewed runtime.streams entry")
        spec = self._settings.streams[source_id]
        subscriptions = [json.dumps(v, ensure_ascii=False, allow_nan=False) for v in spec.get("subscribe", [])]
        if sum(len(v.encode()) for v in subscriptions) > 65536:
            raise ValueError("stream subscription exceeds 64 KiB")
        delay = float(spec.get("reconnect_seconds", 2))
        failures = 0
        while not self.stopping:
            self._update(connection="connecting" if not failures else "reconnecting")
            try:
                with _connect(self._config, spec) as ws:
                    if self.stopping:
                        return
                    for message in subscriptions:
                        self._token.raise_if_cancelled()
                        ws.send(message)
                    self._update(connection="connected", last_connection_at=time.time())
                    while not self.stopping:
                        try:
                            value = ws.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        failures = 0
                        received = time.time()
                        self._update(last_message_at=received)
                        try:
                            value = json.loads(value)
                        except (ValueError, UnicodeError, TypeError):
                            if isinstance(value, bytes):
                                value = value.decode("utf-8", errors="replace")
                        yield {"data": value, "received_at": received}
            except (PermissionError, ImportError):
                raise
            except Exception as exc:
                if self.stopping:
                    return
                failures += 1
                self._update(connection="reconnecting", connection_error=type(exc).__name__)
                if self._token.wait(min(60.0, delay * 2 ** min(failures - 1, 6))):
                    return
        self._update(connection="disconnected")
