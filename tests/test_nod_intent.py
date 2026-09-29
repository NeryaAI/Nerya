"""Nod-intent receipts: camera intent only, single-use, content-bound, risk-independent."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import route_scopes, routes_approvals, routes_nod
from nerya.core import jsonl
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.security import nod_intent as nod
from nerya.vision import nod_worker

pytestmark = pytest.mark.smoke

NOD_FRAMES = ["data:image/jpeg;base64,AAAA"] * 10
TRADE_RECORD = {
    "approval_id": "trade-1",
    "kind": "trade_intent",
    "state": "pending",
    "actor_id": "operator",
    "intent": {"market": "BTC/USDT", "side": "buy", "size": 50},
    "execution_mode": "paper",
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    monkeypatch.setattr(
        nod, "_infer", lambda frames: {"nod": True, "amplitude": 0.14, "frames_used": 10}
    )
    monkeypatch.setattr(
        routes_approvals, "_publish_approval_resolution", lambda *args, **kwargs: None
    )
    return config


def _lerp(a, b, steps):
    return [a + (b - a) * i / steps for i in range(1, steps + 1)]


def _nod_cycle(peak, up_first=False):
    a, b = (0.5 + peak, 0.5 - peak) if up_first else (0.5 - peak, 0.5 + peak)
    return _lerp(0.5, a, 2) + _lerp(a, b, 2) + _lerp(b, 0.5, 2)


def test_detect_nod_accepts_single_and_continuous_nods():
    # One clear out-and-back cycle...
    assert nod_worker.detect_nod([0.5] + _nod_cycle(0.12) + [0.5, 0.5])[0] is True
    assert nod_worker.detect_nod([0.5, 0.5] + _nod_cycle(0.12, up_first=True) + [0.5])[0] is True
    # ...and continuous nodding — including bursts that end mid-motion.
    double = [0.5] + _nod_cycle(0.10) + _nod_cycle(0.10) + [0.56]
    nod, cycles, amp, end_offset = nod_worker.detect_nod(double)
    assert nod is True and cycles >= 3
    triple = [0.5] + _nod_cycle(0.08) * 3 + [0.44]
    assert nod_worker.detect_nod(triple)[0] is True


def test_detect_nod_rejects_static_drift_and_held():
    assert nod_worker.detect_nod([0.5] * 12)[0] is False
    # Monotone drift crosses the centre once and ends far from it.
    assert nod_worker.detect_nod([0.4 + 0.02 * i for i in range(12)])[0] is False
    # Nod down and stay there — no return stroke.
    assert nod_worker.detect_nod(
        [0.5, 0.44, 0.36, 0.30, 0.24, 0.18, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12]
    )[0] is False
    # Move to a new position and hold it.
    assert nod_worker.detect_nod(
        [0.5, 0.42, 0.36, 0.44, 0.52, 0.58, 0.58, 0.58, 0.58, 0.58, 0.58, 0.58]
    )[0] is False


def test_detect_nod_rejects_micro_jitter_and_short_series():
    assert nod_worker.detect_nod(
        [0.5, 0.49, 0.48, 0.47, 0.48, 0.49, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    )[0] is False
    assert nod_worker.detect_nod([0.5, 0.4, 0.5])[0] is False
    assert nod_worker.detect_nod([])[0] is False


def test_capture_mints_single_use_receipt(config):
    service = nod.NodIntentService(config)
    proof = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    assert proof["intent"] == "nod" and proof["receipt"]
    assert proof["identity_proven"] is False and proof["demo"] is True
    consumed = service.consume("operator", "trade-1", TRADE_RECORD, proof["receipt"])
    assert consumed["identity_proven"] is False
    with pytest.raises(nod.NodIntentError) as excinfo:
        service.consume("operator", "trade-1", TRADE_RECORD, proof["receipt"])
    assert str(excinfo.value) == "nod_intent_used"


def test_receipt_binds_actor_and_approval(config):
    service = nod.NodIntentService(config)
    proof = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    for actor, approval in (("attacker", "trade-1"), ("operator", "trade-2")):
        with pytest.raises(nod.NodIntentError) as excinfo:
            service.consume(actor, approval, TRADE_RECORD, proof["receipt"])
        assert str(excinfo.value) == "nod_intent_invalid"


def test_changed_request_invalidates_receipt(config):
    service = nod.NodIntentService(config)
    proof = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    changed = deepcopy(TRADE_RECORD)
    changed["intent"]["size"] = 5000
    with pytest.raises(nod.NodIntentError) as excinfo:
        service.consume("operator", "trade-1", changed, proof["receipt"])
    assert str(excinfo.value) == "nod_intent_stale"


def test_expired_receipt_rejected(config, monkeypatch):
    service = nod.NodIntentService(config)
    proof = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    real_time = time.time
    monkeypatch.setattr(nod.time, "time", lambda: real_time() + nod.RECEIPT_SECONDS + 5)
    with pytest.raises(nod.NodIntentError) as excinfo:
        service.consume("operator", "trade-1", TRADE_RECORD, proof["receipt"])
    assert str(excinfo.value) == "nod_intent_expired"


def test_capture_rejects_wrong_kind_and_missing_nod(config, monkeypatch):
    service = nod.NodIntentService(config)
    tool_record = {**TRADE_RECORD, "kind": "tool_permission_batch"}
    with pytest.raises(nod.NodIntentError) as excinfo:
        service.capture("operator", "tool-1", tool_record, NOD_FRAMES)
    assert str(excinfo.value) == "nod_kind_not_supported"
    monkeypatch.setattr(nod, "_infer", lambda frames: {"nod": False, "amplitude": 0.01})
    with pytest.raises(nod.NodIntentError) as excinfo:
        service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    assert str(excinfo.value) == "nod_not_detected"
    with pytest.raises(nod.NodIntentError):
        service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES[:3])
    with pytest.raises(nod.NodIntentError):
        service.capture("", "trade-1", TRADE_RECORD, NOD_FRAMES)


def test_receipts_encrypted_at_rest(config):
    service = nod.NodIntentService(config)
    service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    raw = service.path.read_bytes()
    assert b"trade-1" not in raw and b"amplitude" not in raw


def test_route_capture_and_scope_registration(config, monkeypatch):
    from nerya.approval_service import ApprovalService

    paths = config.paths
    paths.approvals_pending.parent.mkdir(parents=True, exist_ok=True)
    jsonl.append(paths.approvals_pending, TRADE_RECORD)

    def find(client, approval_id):
        return ApprovalService(client.config).find(approval_id)

    monkeypatch.setattr(routes_approvals, "_find_record", find)
    client = SimpleNamespace(config=config)
    result = routes_nod._capture(client, {"approval_id": "trade-1", "frames": NOD_FRAMES})
    assert result["ok"] is False and result["error"] == "trusted_actor_required"
    result = routes_nod._capture(
        client, {"_auth_actor_id": "operator", "approval_id": "missing", "frames": NOD_FRAMES}
    )
    assert result["ok"] is False and result["_status"] == 404
    result = routes_nod._capture(
        client, {"_auth_actor_id": "operator", "approval_id": "trade-1", "frames": NOD_FRAMES}
    )
    assert result["ok"] is True and result["approval_id"] == "trade-1"
    scope = route_scopes.required_scope("POST", "/security/nod/intent")
    assert scope == "approve:trade|approve:tool"


def test_callback_consumes_receipt_and_audits(config, monkeypatch):
    from nerya.approval_service import ApprovalService

    paths = config.paths
    paths.approvals_pending.parent.mkdir(parents=True, exist_ok=True)
    jsonl.append(paths.approvals_pending, deepcopy(TRADE_RECORD))

    def find(client, approval_id):
        return ApprovalService(client.config).find(approval_id)

    monkeypatch.setattr(routes_approvals, "_find_record", find)
    client = SimpleNamespace(config=config)
    proof = routes_nod._capture(
        client, {"_auth_actor_id": "operator", "approval_id": "trade-1", "frames": NOD_FRAMES}
    )
    callback = {
        path: handler
        for method, path, handler in routes_approvals.routes()
        if method == "POST"
    }["/approvals/callback"]
    result = callback(client, {
        "_auth_actor_id": "operator",
        "_auth_scopes": ["approve:trade"],
        "callback_data": "approve:trade-1",
        "nod_intent_receipt": proof["receipt"],
    })
    assert result["ok"] is True and result["state"] == "approved"
    assert result["nod_intent"]["identity_proven"] is False
    audit = jsonl.read_all(paths.approvals_pending.parent / "callbacks.jsonl")
    assert audit[-1]["nod_intent"] is True

    # Second approve with a replayed receipt fails closed...
    jsonl.append(paths.approvals_pending, {**deepcopy(TRADE_RECORD), "approval_id": "trade-2"})
    proof2 = routes_nod._capture(
        client, {"_auth_actor_id": "operator", "approval_id": "trade-2", "frames": NOD_FRAMES}
    )
    stored = ApprovalService(config).find("trade-2")
    service = nod.NodIntentService(config)
    service.consume("operator", "trade-2", stored, proof2["receipt"])
    result = callback(client, {
        "_auth_actor_id": "operator",
        "_auth_scopes": ["approve:trade"],
        "callback_data": "approve:trade-2",
        "nod_intent_receipt": proof2["receipt"],
    })
    assert result["ok"] is False and result["error"] == "nod_intent_used"
    # ...while the normal path without a receipt still resolves the approval.
    result = callback(client, {
        "_auth_actor_id": "operator",
        "_auth_scopes": ["approve:trade"],
        "callback_data": "approve:trade-2",
    })
    assert result["ok"] is True and result["state"] == "approved"
    assert "nod_intent" not in result


def test_approval_payload_untouched_by_nod(config, monkeypatch):
    """The nod receipt must never inject state into the frozen request payload."""
    from nerya.approval_service import ApprovalService

    paths = config.paths
    paths.approvals_pending.parent.mkdir(parents=True, exist_ok=True)
    record = deepcopy(TRADE_RECORD)
    jsonl.append(paths.approvals_pending, record)
    before = json.dumps(ApprovalService(config).find("trade-1")["intent"], sort_keys=True)

    def find(client, approval_id):
        return ApprovalService(client.config).find(approval_id)

    monkeypatch.setattr(routes_approvals, "_find_record", find)
    client = SimpleNamespace(config=config)
    proof = routes_nod._capture(
        client, {"_auth_actor_id": "operator", "approval_id": "trade-1", "frames": NOD_FRAMES}
    )
    callback = {
        path: handler
        for method, path, handler in routes_approvals.routes()
        if method == "POST"
    }["/approvals/callback"]
    result = callback(client, {
        "_auth_actor_id": "operator",
        "_auth_scopes": ["approve:trade"],
        "callback_data": "approve:trade-1",
        "nod_intent_receipt": proof["receipt"],
    })
    assert result["ok"] is True
    moved = next(
        row
        for row in jsonl.read_all(paths.approvals / "approved.jsonl")
        if row.get("approval_id") == "trade-1"
    )
    assert json.dumps(moved["intent"], sort_keys=True) == before
    assert moved["state"] == "approved"


class _FakeServeProc:
    """In-memory stand-in for the persistent worker process."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list = []
        self.stdin = self
        self.stdout = self
        self._alive = True

    def write(self, line):
        self.requests.append(json.loads(line))
        return len(line)

    def flush(self):
        return None

    def readline(self):
        if len(self.requests) > len(self.responses):
            return ""
        return json.dumps(self.responses[len(self.requests) - 1]) + "\n"

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self._alive = False

    def close(self):
        return None


def test_camera_session_reuses_warm_worker_and_releases_on_close(config, monkeypatch):
    spawned = []

    def fake_spawn():
        proc = _FakeServeProc([
            {"ok": True, "nod": False, "amplitude": 0.0, "frames_used": 12},   # warmup (empty frames)
            {"ok": True, "nod": True, "amplitude": 0.2, "frames_used": 12},    # capture 1
            {"ok": True, "nod": True, "amplitude": 0.2, "frames_used": 12},    # capture 2 (same worker)
        ])
        spawned.append(proc)
        return proc

    monkeypatch.setattr(nod, "_spawn_serve", fake_spawn)
    monkeypatch.setattr(
        nod, "_infer",
        lambda frames: (_ for _ in ()).throw(AssertionError("one-shot must not run while session is warm")),
    )

    service = nod.NodIntentService(config)
    opened = nod.open_session("operator")
    assert opened["ok"] is True
    for _ in range(40):
        if spawned and len(spawned[0].requests) >= 1:
            break
        time.sleep(0.05)

    proof1 = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    proof2 = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    assert proof1["receipt"] and proof2["receipt"]
    assert len(spawned) == 1, "one worker for the whole camera session"
    assert len(spawned[0].requests) == 3, "warmup + 2 captures through the same pipe"
    consumed = service.consume("operator", "trade-1", TRADE_RECORD, proof2["receipt"])
    assert consumed["identity_proven"] is False

    closed = nod.close_session("operator")
    assert closed["closed"] is True
    assert spawned[0]._alive is False

    # After close, capture falls back to the one-shot worker.
    monkeypatch.setattr(
        nod, "_infer",
        lambda frames: {"nod": True, "amplitude": 0.14, "frames_used": 12},
    )
    proof3 = service.capture("operator", "trade-1", TRADE_RECORD, NOD_FRAMES)
    assert proof3["receipt"]


def test_session_route_open_close_and_guards(config, monkeypatch):
    from nerya.api import routes_nod

    calls = []
    # The route module binds these names at import time; patch there.
    monkeypatch.setattr(routes_nod, "open_session", lambda actor: calls.append(("open", actor)) or {"ok": True})
    monkeypatch.setattr(routes_nod, "close_session", lambda actor: calls.append(("close", actor)) or {"ok": True, "closed": True})
    handler = {p: h for m, p, h in routes_nod.routes() if m == "POST"}["/security/nod/session"]
    client = SimpleNamespace(config=config)

    result = handler(client, {"_auth_actor_id": "operator", "action": "open"})
    assert result == {"ok": True}
    result = handler(client, {"_auth_actor_id": "operator", "action": "close"})
    assert result["closed"] is True
    result = handler(client, {"_auth_actor_id": "operator", "action": "wat"})
    assert result["ok"] is False and result["error"] == "unknown_action"
    untrusted = handler(client, {"action": "open"})
    assert untrusted["ok"] is False and untrusted["error"] == "trusted_actor_required"
    assert [c[0] for c in calls] == ["open", "close"]
