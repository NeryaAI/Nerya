"""Host-only, authenticated decision tape for current-model retrospective replay.

Interface: construct with trusted binding + a dedicated host key; bind() binds
one actual gated call, consume() either returns its saved envelope or raises
DecisionRequired. The host may append() a producer answer, then consume again.
dumps()/verify() retain the authenticated chain, never the key or replay cursor.
finish() rejects unused receipts and unresolved requests. Calls are serialized.

Never inject this object/key into candidate Python. The host must derive binding
hashes from immutable source/config/data/prompt/model snapshots and supply only
the current historical prefix to candidates. HMAC authenticates the host's record,
not model truth, history coverage, fills, or contemporaneous knowledge. Model
authorization, cost reservation, durable cache writes and uncertain-call recovery
belong to the host producer. This module does no I/O and invokes no producer.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any


LABEL = "current_model_retrospective_replay"
GENESIS = "0" * 64
MAX_JSON_BYTES = 128 * 1024
MAX_TAPE_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_HASH_FIELDS = {"source_revision", "config_hash", "data_hash", "role_prompt_hash", "model_settings_hash"}
_IDENTITY_FIELDS = {"candidate_id", "runtime_build_id", "sdk_build_id", "provider", "model", "model_version"}
_REQUEST_FIELDS = {"version", "label", "binding", "timestamp_ms", "market", "timeframe", "role",
                   "ordinal", "prior_receipt_hash", "payload_hash"}
_RECEIPT_FIELDS = {"version", "label", "request", "request_hash", "response", "response_hash",
                   "producer_kind", "produced_at_ms", "usage", "signature"}
_PRODUCER_FIELDS = {"request_digest", "provider", "model", "generated_at", "response_sha256",
                    "reserved_tokens", "usage", "evidence_level"}


class DecisionTapeError(ValueError):
    """A closed evidence gate; callers must not convert this into a HOLD."""
    performance_evidence = False

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # Do not echo prompts, payloads or keys in errors.


class DecisionRequired(DecisionTapeError):
    def __init__(self, request: dict):
        self.request = _copy(request)
        super().__init__("decision_required")


def canonical_json(value: Any, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    """Version-1 canonical JSON: sorted UTF-8 keys, no coercion/NaN/duplicates.

    Integer and floating representations are intentionally distinct. Bounds apply
    before hashing/signing; UTF-8 surrogates, cycles and non-JSON types fail closed.
    """
    budget = [10_000]

    def check(item, depth=0):
        budget[0] -= 1
        if depth > 20 or budget[0] < 0:
            raise DecisionTapeError("json_complexity_limit")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is int and abs(item) <= 2**53 - 1:
            return
        if kind is float and math.isfinite(item):
            return
        if kind is str:
            if len(item) > max_bytes:
                raise DecisionTapeError("json_size_limit")
            return
        if kind is list:
            for child in item:
                check(child, depth + 1)
            return
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise DecisionTapeError("json_key_type")
                check(key, depth + 1)
                check(child, depth + 1)
            return
        raise DecisionTapeError("invalid_json_value")

    check(value)
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DecisionTapeError("invalid_json") from exc
    if len(raw) > max_bytes:
        raise DecisionTapeError("json_size_limit")
    return raw


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _copy(value):
    return json.loads(canonical_json(value))


def _fields(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise DecisionTapeError("invalid_schema")


def _digest(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise DecisionTapeError("invalid_digest")


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise DecisionTapeError("invalid_identifier")


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise DecisionTapeError("invalid_integer")


def _version_label(value):
    if type(value["version"]) is not int or value["version"] != 1 or value["label"] != LABEL:
        raise DecisionTapeError("invalid_version_or_label")


def _binding(value):
    _fields(value, _HASH_FIELDS | _IDENTITY_FIELDS)
    for field in _HASH_FIELDS:
        _digest(value[field])
    for field in _IDENTITY_FIELDS:
        _text(value[field])


def _response(value):
    _fields(value, {"ok", "output"})
    if value["ok"] is not True:
        raise DecisionTapeError("unsuccessful_response")
    output = value["output"]
    if (type(output) is not dict or not {"decision", "confidence"} <= output.keys()
            or output.keys() - {"decision", "confidence", "drivers", "veto_reason"}):
        raise DecisionTapeError("invalid_response_schema")
    confidence = output["confidence"]
    if (output["decision"] not in ("ENTRY", "HOLD") or type(confidence) not in (int, float)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise DecisionTapeError("invalid_verdict")
    if "drivers" in output and (type(output["drivers"]) is not list
                                or any(type(item) is not str for item in output["drivers"])):
        raise DecisionTapeError("invalid_response_schema")
    if "veto_reason" in output and type(output["veto_reason"]) is not str:
        raise DecisionTapeError("invalid_response_schema")


def _producer_receipt(value, binding):
    """Validate host producer metadata; raw-text hash is distinct from response_hash."""
    _fields(value, _PRODUCER_FIELDS)
    for field in ("request_digest", "response_sha256"):
        _digest(value[field])
    if value["evidence_level"] != LABEL:
        raise DecisionTapeError("invalid_producer_evidence")
    for field in ("provider", "model"):
        _text(value[field])
        if value[field] != binding[field]:
            raise DecisionTapeError("producer_identity_mismatch")
    generated = value["generated_at"]
    if (type(generated) not in (int, float) or not math.isfinite(generated)
            or not 0 < generated <= (2**53 - 1) / 1000):
        raise DecisionTapeError("invalid_producer_timestamp")
    _integer(value["reserved_tokens"], 1)
    _fields(value["usage"], {"input_tokens", "output_tokens"})
    for count in value["usage"].values():
        _integer(count)
    if sum(value["usage"].values()) > value["reserved_tokens"]:
        raise DecisionTapeError("producer_usage_exceeds_reservation")
    return int(generated * 1000)


class DecisionTape:
    """Single-writer host tape. Replay always starts at ordinal zero.

    Bind one confirmation role/prompt and pinned model per tape. max_receipts is
    a resource bound, not authorization to call a model. Authenticated import
    plus exact record retries is an idempotent cache; persistence is caller-owned.
    """

    def __init__(self, binding: dict, *, host_key: bytes, max_receipts: int = 20):
        if type(host_key) is not bytes or len(host_key) < 32:
            raise DecisionTapeError("host_key_required_minimum_32_bytes")
        _integer(max_receipts, 1)
        if max_receipts > 1000:
            raise DecisionTapeError("receipt_limit")
        binding = _copy(binding)
        _binding(binding)
        self._binding = binding
        self._key = host_key
        self._limit = max_receipts
        self._receipts: list[dict] = []
        self._cursor = 0
        self._pending: dict | None = None
        self._failed = False

    def _sign(self, kind, body):
        raw = canonical_json(body, max_bytes=MAX_TAPE_BYTES if kind == "tape" else MAX_JSON_BYTES)
        return hmac.new(self._key, b"nerya.decision_tape.v1/" + kind.encode() + b"\0" + raw,
                        hashlib.sha256).hexdigest()

    def _verify(self, kind, value):
        _digest(value["signature"])
        body = {k: v for k, v in value.items() if k != "signature"}
        if not hmac.compare_digest(value["signature"], self._sign(kind, body)):
            raise DecisionTapeError("invalid_signature")

    def _active(self):
        if self._failed:
            raise DecisionTapeError("tape_failed")

    def _check_request(self, request, ordinal):
        _fields(request, _REQUEST_FIELDS)
        _version_label(request)
        _binding(request["binding"])
        if request["binding"] != self._binding:
            raise DecisionTapeError("stale_binding")
        _integer(request["ordinal"])
        _integer(request["timestamp_ms"], 1)
        if request["ordinal"] != ordinal:
            raise DecisionTapeError("ordinal_mismatch")
        for field in ("market", "timeframe", "role"):
            _text(request[field])
        for field in ("payload_hash", "prior_receipt_hash"):
            _digest(request[field])
        previous = self._receipts[ordinal - 1] if ordinal else None
        expected = canonical_hash(previous) if previous else GENESIS
        if request["prior_receipt_hash"] != expected:
            raise DecisionTapeError("chain_mismatch")
        if previous and request["timestamp_ms"] < previous["request"]["timestamp_ms"]:
            raise DecisionTapeError("chronology_mismatch")

    def bind(self, *, timestamp_ms: int, market: str, timeframe: str, role: str,
             payload: dict) -> dict:
        """Capture one actual call; the host supplies the clock and trusted identity."""
        self._active()
        if type(payload) is not dict:
            raise DecisionTapeError("invalid_payload")
        request = {"version": 1, "label": LABEL, "binding": self._binding,
                   "timestamp_ms": timestamp_ms, "market": market, "timeframe": timeframe,
                   "role": role, "ordinal": self._cursor,
                   "prior_receipt_hash": canonical_hash(self._receipts[self._cursor - 1]) if self._cursor else GENESIS,
                   "payload_hash": canonical_hash(payload)}
        try:
            self._check_request(request, self._cursor)
            if self._pending is not None and canonical_json(self._pending) != canonical_json(request):
                raise DecisionTapeError("unresolved_request_changed")
        except DecisionTapeError:
            self._failed = True
            raise
        self._pending = _copy(request)
        return _copy(request)

    def consume(self, request: dict) -> dict:
        """Exact next receipt or DecisionRequired; never a default verdict."""
        self._active()
        try:
            request = _copy(request)
            self._check_request(request, self._cursor)
            if self._pending is None or canonical_json(request) != canonical_json(self._pending):
                raise DecisionTapeError("request_not_captured")
            if self._cursor == len(self._receipts):
                raise DecisionRequired(request)
            receipt = self._receipts[self._cursor]
            if canonical_json(receipt["request"]) != canonical_json(request):
                raise DecisionTapeError("request_mismatch")
        except DecisionRequired:
            raise
        except DecisionTapeError:
            self._failed = True
            raise
        self._cursor += 1
        self._pending = None
        return _copy(receipt["response"])

    def append_produced(self, request: dict, *, result: dict,
                        expected_request_digest: str) -> dict:
        """Wrap DecisionProducer's trusted host result without calling it.

        The producer must receive binding=request (the full tape request), not
        just its static binding. expected_request_digest comes independently
        from the host's canonical producer request identity, never result itself
        or candidate output. Bind output schema, max output tokens and prompt
        policy/version into model_settings_hash before creating this tape.

        response_sha256 authenticates raw text retained by the producer; this
        module cannot recompute it from parsed output. The signed canonical
        response_hash separately protects the actual envelope replayed here.
        """
        self._active()
        result = _copy(result)
        _fields(result, _PRODUCER_FIELDS | {"output"})
        _digest(expected_request_digest)
        metadata = {key: value for key, value in result.items() if key != "output"}
        produced_at_ms = _producer_receipt(metadata, self._binding)
        if not hmac.compare_digest(result["request_digest"], expected_request_digest):
            raise DecisionTapeError("producer_request_digest_mismatch")
        return self.append(request, response={"ok": True, "output": result["output"]},
                           producer_kind="model", produced_at_ms=produced_at_ms,
                           usage=result["usage"], producer_receipt=metadata)

    def append(self, request: dict, *, response: dict, producer_kind: str,
               produced_at_ms: int, usage: dict, producer_receipt: dict | None = None) -> dict:
        """Host-only successful producer result. No invocation or financial authority.

        producer_kind is host observed ('model' or 'synthetic'), never copied from
        candidate output. All retry fields must match; conflicting answers fail.
        """
        self._active()
        request, response, usage = _copy(request), _copy(response), _copy(usage)
        _fields(request, _REQUEST_FIELDS)
        _response(response)
        _fields(usage, {"input_tokens", "output_tokens"})
        for value in usage.values():
            _integer(value)
        if producer_kind not in ("model", "synthetic"):
            raise DecisionTapeError("invalid_producer_kind")
        _integer(produced_at_ms, 1)
        ordinal = request.get("ordinal")
        _integer(ordinal)
        if ordinal > len(self._receipts):
            raise DecisionTapeError("ordinal_mismatch")
        self._check_request(request, ordinal)
        if produced_at_ms < request["timestamp_ms"]:
            raise DecisionTapeError("nonretrospective_receipt")
        body = {"version": 1, "label": LABEL, "request": request,
                "request_hash": canonical_hash(request), "response": response,
                "response_hash": canonical_hash(response), "producer_kind": producer_kind,
                "produced_at_ms": produced_at_ms, "usage": usage}
        if producer_receipt is not None:
            producer_receipt = _copy(producer_receipt)
            produced = _producer_receipt(producer_receipt, self._binding)
            if (producer_kind != "model" or produced != produced_at_ms
                    or canonical_json(producer_receipt["usage"]) != canonical_json(usage)):
                raise DecisionTapeError("producer_receipt_mismatch")
            body["producer_receipt"] = producer_receipt
        receipt = {**body, "signature": self._sign("receipt", body)}
        canonical_json(receipt)
        if ordinal < len(self._receipts):
            if canonical_json(receipt) != canonical_json(self._receipts[ordinal]):
                raise DecisionTapeError("idempotency_conflict")
            return _copy(self._receipts[ordinal])
        if self._pending is None or canonical_json(request) != canonical_json(self._pending):
            raise DecisionTapeError("request_not_captured")
        if len(self._receipts) >= self._limit:
            raise DecisionTapeError("receipt_limit")
        # Keep append atomic even when a valid row would overflow the full tape.
        self._snapshot([*self._receipts, receipt])
        self._receipts.append(receipt)
        return _copy(receipt)

    def dumps(self) -> bytes:
        """Authenticated snapshot (including count/order); key/cursor stay in host."""
        self._active()
        return self._snapshot(self._receipts)

    def _snapshot(self, receipts):
        body = {"version": 1, "label": LABEL, "binding": self._binding, "receipts": receipts}
        return canonical_json({**body, "signature": self._sign("tape", body)}, max_bytes=MAX_TAPE_BYTES)

    @classmethod
    def verify(cls, raw: bytes, *, binding: dict, host_key: bytes,
               max_receipts: int = 20) -> DecisionTape:
        """Verify every receipt and the complete tape before making any available."""
        if type(raw) is not bytes or len(raw) > MAX_TAPE_BYTES:
            raise DecisionTapeError("invalid_tape_bytes")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise DecisionTapeError("duplicate_json_key")
                result[key] = value
            return result

        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
            canonical_json(value, max_bytes=MAX_TAPE_BYTES)
        except (UnicodeError, ValueError, RecursionError) as exc:
            if isinstance(exc, DecisionTapeError):
                raise
            raise DecisionTapeError("invalid_json") from exc
        _fields(value, {"version", "label", "binding", "receipts", "signature"})
        _version_label(value)
        tape = cls(binding, host_key=host_key, max_receipts=max_receipts)
        tape._verify("tape", value)
        if value["binding"] != tape._binding:
            raise DecisionTapeError("stale_binding")
        if type(value["receipts"]) is not list or len(value["receipts"]) > max_receipts:
            raise DecisionTapeError("receipt_limit")
        for index, receipt in enumerate(value["receipts"]):
            fields = _RECEIPT_FIELDS | ({"producer_receipt"} if type(receipt) is dict and "producer_receipt" in receipt else set())
            _fields(receipt, fields)
            if "producer_receipt" in receipt:
                _producer_receipt(receipt["producer_receipt"], tape._binding)
            _version_label(receipt)
            tape._verify("receipt", receipt)
            tape._check_request(receipt["request"], index)
            for field in ("request_hash", "response_hash"):
                _digest(receipt[field])
            if (receipt["request_hash"] != canonical_hash(receipt["request"])
                    or receipt["response_hash"] != canonical_hash(receipt["response"])):
                raise DecisionTapeError("digest_mismatch")
            tape._pending = receipt["request"]
            # Reuse the host receipt schema and chronological validation.
            tape.append(receipt["request"], response=receipt["response"],
                        producer_kind=receipt["producer_kind"], produced_at_ms=receipt["produced_at_ms"],
                        usage=receipt["usage"], producer_receipt=receipt.get("producer_receipt"))
        tape._pending = None
        return tape

    def finish(self) -> dict:
        """Require exact consumption. This proof alone never certifies performance."""
        self._active()
        if self._pending is not None:
            raise DecisionTapeError("unresolved_request")
        if self._cursor != len(self._receipts):
            raise DecisionTapeError("unconsumed_receipts")
        return {"label": LABEL, "decision_receipts_verified": True,
                "model_decisions_verified": bool(self._receipts) and all(
                    row["producer_kind"] == "model" for row in self._receipts),
                "decision_count": self._cursor, "performance_evidence": False}
