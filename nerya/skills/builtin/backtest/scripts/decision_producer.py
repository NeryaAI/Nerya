"""Host-only, bounded retrospective model calls; no tool or trade authority."""

from __future__ import annotations

import hashlib
from contextlib import closing
from enum import Enum
import json
import math
from pathlib import Path
import sqlite3
import time

from .....core.errors import LLMError
from .....llm.messages import (
    MessagesRequest, OpenAIMessagesBackend, _model_reasoning_wire, normalise_reasoning_effort,
)
from .....harness.cancellation import CancelledError, raise_if_cancelled
from .....llm.attempt_budget import AttemptBudget, attempt_budget_scope


# Covers twenty ~18.7k reservations with headroom; per-call and call-count
# limits still apply independently. This is a ceiling, not a default grant.
MAX_RETROSPECTIVE_REPLAY_TOKENS = 500_000


class DecisionProducerError(ValueError):
    pass


class ResponseFailure(str, Enum):
    PENDING = "decision_response_validation_pending"
    REJECTED = "decision_request_failed_response"
    IDENTITY = "decision_response_identity_mismatch"
    TOOLS = "decision_response_tool_calls_forbidden"
    TRUNCATED = "decision_response_truncated"
    COMPLETION = "decision_response_completion_invalid"
    TEXT = "decision_response_text_invalid"
    SIZE = "decision_response_too_large"
    JSON = "decision_response_json_invalid"
    OBJECT = "decision_response_not_object"
    SCHEMA = "decision_response_schema_invalid"
    USAGE = "decision_response_usage_invalid"
    OVERRUN = "decision_provider_exceeded_budget"


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DecisionProducerError("duplicate model result field")
        result[key] = value
    return result


class DecisionProducer:
    """Reserve before sending; unknown results never trigger a second request.

    The controller owns this database outside the candidate sandbox. Budget is
    conservative UTF-8 byte upper bounds plus output limit, not estimated usage.
    A fixed authorization ID cannot reset its limits or extend its deadline.
    The forced record tool is a data envelope, never dispatched or executed.
    Compatibility: zero tools with end_turn may return a strict plain JSON
    object. No prose extraction, fences, repair, or second request is allowed.
    """

    policy_version = 2

    def output_policy(self, output_schema: dict) -> dict:
        """Public wire contract to include in the caller's model_settings_hash.

        Includes the schema, forced choice, validation compatibility and policy
        version; callers must bind this whole value, not only the version.
        """
        return {
            "policy_version": self.policy_version,
            "reasoning_effort": self.reasoning_effort,
            "tools": [{
                "name": "record_replay_decision",
                "description": "Record one retrospective decision as data only; never execute tools or trades.",
                "input_schema": output_schema,
            }],
            "tool_choice": {"type": "tool", "name": "record_replay_decision"},
            "plain_json_fallback": "strict_object_end_turn_no_tools",
        }

    def __init__(
        self,
        path: Path,
        *,
        authorization_id: str,
        backend,
        provider: str,
        model: str,
        max_calls: int,
        max_tokens: int,
        deadline: float,
        cancel_token=None,
        reasoning_effort=None,
    ):
        if (
            not authorization_id
            or not provider
            or not model
            or type(max_calls) is not int
            or not 1 <= max_calls <= 20
            or type(max_tokens) is not int
            or not 1 <= max_tokens <= MAX_RETROSPECTIVE_REPLAY_TOKENS
            or not math.isfinite(deadline)
            or deadline > time.time() + 1801
        ):
            raise DecisionProducerError("invalid retrospective replay authorization")
        self.path, self.authorization_id, self.backend = (
            Path(path),
            authorization_id,
            backend,
        )
        self.provider, self.model = provider, model
        self.cancel_token = cancel_token
        if reasoning_effort not in {None, "none", "minimal", "low", "medium", "high"}:
            raise DecisionProducerError("invalid decision reasoning policy")
        self.reasoning_effort = reasoning_effort
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise DecisionProducerError("unsafe decision ledger")
        with closing(self._db()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget (id TEXT PRIMARY KEY, policy TEXT NOT NULL, calls INTEGER NOT NULL, reserved INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (owner TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT, PRIMARY KEY(owner,digest))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS response_diagnostics (owner TEXT NOT NULL, digest TEXT NOT NULL, reason_code TEXT NOT NULL, diagnostics TEXT NOT NULL, PRIMARY KEY(owner,digest))"
            )
            policy = _json(
                dict(
                    provider=provider,
                    model=model,
                    max_calls=max_calls,
                    max_tokens=max_tokens,
                    deadline=deadline,
                )
            )
            db.execute(
                "INSERT OR IGNORE INTO budget VALUES (?,?,0,0)",
                (authorization_id, policy),
            )
            if (
                db.execute(
                    "SELECT policy FROM budget WHERE id=?", (authorization_id,)
                ).fetchone()[0]
                != policy
            ):
                raise DecisionProducerError("authorization policy changed")
        self.path.chmod(0o600)
        self.max_calls, self.max_tokens, self.deadline = max_calls, max_tokens, deadline

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def _response_failure(self, digest, reason):
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE response_diagnostics SET reason_code=? WHERE owner=? AND digest=?",
                       (reason.value, self.authorization_id, digest))
            if reason is ResponseFailure.OVERRUN:
                db.execute("UPDATE budget SET reserved=? WHERE id=?", (self.max_tokens, self.authorization_id))
            diagnostic = json.loads(db.execute(
                "SELECT diagnostics FROM response_diagnostics WHERE owner=? AND digest=?",
                (self.authorization_id, digest),
            ).fetchone()[0])
        error = DecisionProducerError(reason.value)
        error.receipt = {
            **diagnostic,
            "reason": reason.value,
            "label": "current_model_retrospective_replay", "performance_evidence": False,
            "authorization_id": self.authorization_id, "request_digest": digest,
            "status": "failed_response", "failure_stage": "model_response_validation",
            "request_sent": True, "reservation_retained": True,
            "safe_to_retry": False, "recovery_disposition": "no_automatic_retry",
        }
        raise error

    def request_identity(
        self,
        *,
        binding: dict,
        role_prompt: str,
        payload: dict,
        output_schema: dict,
        max_output_tokens: int = 1024,
    ) -> dict:
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 16384:
            raise DecisionProducerError("invalid decision output budget")
        raise_if_cancelled(self.cancel_token)
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(output_schema)
        output_policy = self.output_policy(output_schema)
        policy = (
            "Evaluate only the supplied historical prefix. This is a current-model retrospective replay, "
            "not contemporaneous or live evidence. Do not use later knowledge. "
            "Submit exactly one record_replay_decision tool call matching its input schema. "
            "This records data only and never executes tools or trades. No other tools are permitted."
        )
        system = policy + "\nRole:\n" + role_prompt
        text = _json(payload)
        identity = dict(
            binding=binding,
            system=system,
            payload=payload,
            provider=self.provider,
            model=self.model,
            max_output_tokens=max_output_tokens,
            temperature=0,
            output_policy=output_policy,
        )
        digest = hashlib.sha256(_json(identity).encode()).hexdigest()
        wire = {key: output_policy[key] for key in ("tools", "tool_choice")}
        reserve = len(system.encode()) + len(text.encode()) + len(_json(wire).encode()) + max_output_tokens + 256
        return {"digest": digest, "reserve": reserve, "system": system, "text": text}

    def produce(
        self,
        *,
        binding: dict,
        role_prompt: str,
        payload: dict,
        output_schema: dict,
        max_output_tokens: int = 1024,
    ) -> dict:
        from jsonschema import Draft202012Validator

        prepared = self.request_identity(
            binding=binding,
            role_prompt=role_prompt,
            payload=payload,
            output_schema=output_schema,
            max_output_tokens=max_output_tokens,
        )
        digest, reserve, system, text = (
            prepared[key] for key in ("digest", "reserve", "system", "text")
        )
        output_policy = self.output_policy(output_schema)
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT status,result FROM requests WHERE owner=? AND digest=?",
                (self.authorization_id, digest),
            ).fetchone()
            if prior:
                if prior[0] == "complete":
                    return json.loads(prior[1])
                if prior[0] == "failed_response":
                    raise DecisionProducerError("decision_request_failed_response; no automatic retry")
                raise DecisionProducerError(
                    "decision_request_unconfirmed; reconcile before retry"
                )
            if isinstance(self.backend, OpenAIMessagesBackend):
                effort = self.reasoning_effort if self.reasoning_effort is not None else self.backend.reasoning_effort
                # Deterministic adapter rejection precedes the durable wire reservation.
                _model_reasoning_wire(self.backend.provider_name, self.backend.model,
                                      normalise_reasoning_effort(effort))
            calls, reserved = db.execute(
                "SELECT calls,reserved FROM budget WHERE id=?", (self.authorization_id,)
            ).fetchone()
            exhausted = [name for name, hit in (
                ("deadline", time.time() >= self.deadline),
                ("calls", calls >= self.max_calls),
                ("tokens", reserved + reserve > self.max_tokens),
            ) if hit]
            if exhausted:
                error = DecisionProducerError("decision_budget_exhausted")
                # Host-owned facts flow into failure.json and tool diagnostics.
                # Never include prompts/results or mutate a rejected reservation.
                error.receipt = {
                    "reason": "decision_budget_exhausted",
                    "label": "current_model_retrospective_replay",
                    "performance_evidence": False,
                    "authorization_id": self.authorization_id,
                    "exhausted_limits": exhausted,
                    "calls": calls, "max_calls": self.max_calls,
                    "reserved_tokens": reserved, "max_tokens": self.max_tokens,
                    "remaining_calls": max(0, self.max_calls - calls),
                    "remaining_tokens": max(0, self.max_tokens - reserved),
                    "required_reservation_tokens": reserve,
                    "deadline": self.deadline, "request_sent": False,
                }
                raise error
            db.execute(
                "UPDATE budget SET calls=calls+1,reserved=reserved+? WHERE id=?",
                (reserve, self.authorization_id),
            )
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,NULL)",
                (self.authorization_id, digest, "unconfirmed"),
            )
            db.commit()
        finally:
            db.close()
        # Each durable reservation authorizes exactly one wire attempt,
        # independent of any surrounding Agent turn's retry allowance.
        try:
            with attempt_budget_scope(AttemptBudget(limit=0)):
                response = self.backend(
                    MessagesRequest(
                        system=system,
                        messages=[{"role": "user", "content": text}],
                        tools=output_policy["tools"],
                        tool_choice=output_policy["tool_choice"],
                        max_tokens=max_output_tokens,
                        temperature=0,
                        stream=False,
                        deadline=self.deadline,
                        cancel_token=self.cancel_token,
                        reasoning_effort=self.reasoning_effort,
                        metadata={"retrospective_replay": True},
                    )
                )
        except CancelledError as exc:
            # Cancellation cannot attest whether the provider processed this
            # reservation. Preserve its identity and charge without retrying.
            exc.receipt = {
                "reason": "deadline_exceeded" if str(exc) == "deadline_exceeded" else "cancelled",
                "label": "current_model_retrospective_replay", "performance_evidence": False,
                "authorization_id": self.authorization_id, "request_digest": digest,
                "status": "cancelled", "request_status": "unconfirmed",
                "reservation_retained": True, "safe_to_retry": False,
                "recovery_disposition": "no_automatic_retry",
                "requires_receipt_reconciliation": True,
            }
            raise
        except LLMError as exc:
            status = getattr(exc, "status_code", None)
            if type(status) is not int or not 400 <= status < 600:
                raise
            # A typed HTTP rejection confirms a failed response, not usage or
            # success. Keep the entire reservation and never retry this identity.
            request_id = getattr(exc, "request_id", "")
            diagnostic = {
                "max_output_tokens": max_output_tokens, "reserved_tokens": reserve,
                "status_code": status,
                "request_id": request_id[:160] if type(request_id) is str else "",
            }
            with closing(self._db()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO response_diagnostics VALUES (?,?,?,?)",
                           (self.authorization_id, digest, ResponseFailure.REJECTED.value, _json(diagnostic)))
                db.execute("UPDATE requests SET status='failed_response' WHERE owner=? AND digest=? AND status='unconfirmed'",
                           (self.authorization_id, digest))
            self._response_failure(digest, ResponseFailure.REJECTED)
        # Persist response facts before any rejection. No model text, tool args,
        # raw provider body, or exception detail enters the diagnostic ledger.
        raw, encoded = None, None
        tool_calls = response.tool_uses()
        try:
            raw = response.text()
            encoded = (
                _json({"text": raw, "tool_uses": tool_calls}) if tool_calls else raw
            ).encode("utf-8")
        except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError):
            pass
        usage_raw = response.usage
        def valid_count(value):
            return type(value) is int and 0 <= value <= 2**53 - 1
        usage_valid = type(usage_raw) is dict and all(valid_count(value) for value in usage_raw.values())
        usage = {key: usage_raw.get(key, 0) if usage_valid else None
                 for key in ("input_tokens", "output_tokens")}
        stop = response.stop_reason
        stops = {"end_turn", "max_tokens", "length", "tool_use", "stop_sequence", "content_filter", "pause_turn", "refusal"}
        diagnostic = {
            "max_output_tokens": max_output_tokens,
            "reserved_tokens": reserve,
            "stop_reason": stop if type(stop) is str and stop in stops else "unknown",
            "provider_match": response.provider == self.provider,
            "model_match": response.model == self.model,
            "tool_call_count": len(tool_calls),
            "usage": usage,
            "response_sha256": hashlib.sha256(encoded).hexdigest() if encoded is not None else None,
        }
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO response_diagnostics VALUES (?,?,?,?)",
                       (self.authorization_id, digest, ResponseFailure.PENDING.value, _json(diagnostic)))
            db.execute("UPDATE requests SET status='failed_response' WHERE owner=? AND digest=? AND status='unconfirmed'",
                       (self.authorization_id, digest))
        if not usage_valid:
            self._response_failure(digest, ResponseFailure.USAGE)
        if sum(usage.values()) > reserve:
            self._response_failure(digest, ResponseFailure.OVERRUN)
        if not diagnostic["provider_match"] or not diagnostic["model_match"]:
            self._response_failure(digest, ResponseFailure.IDENTITY)
        if stop in ("max_tokens", "length"):
            self._response_failure(digest, ResponseFailure.TRUNCATED)
        if tool_calls and (len(tool_calls) != 1 or tool_calls[0].get("name") != "record_replay_decision"):
            self._response_failure(digest, ResponseFailure.TOOLS)
        if stop != ("tool_use" if tool_calls else "end_turn"):
            self._response_failure(digest, ResponseFailure.COMPLETION)
        if encoded is None:
            self._response_failure(digest, ResponseFailure.JSON if tool_calls else ResponseFailure.TEXT)
        if len(encoded) > 64_000:
            self._response_failure(digest, ResponseFailure.SIZE)
        if tool_calls:
            output = tool_calls[0].get("input")
        else:
            # Compatibility for backends returning plain JSON despite forced
            # choice. Parse the entire text strictly; never extract or repair.
            try:
                output = json.loads(raw, object_pairs_hook=_unique,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            except (ValueError, RecursionError):
                self._response_failure(digest, ResponseFailure.JSON)
        if not isinstance(output, dict):
            self._response_failure(digest, ResponseFailure.OBJECT)
        if not Draft202012Validator(output_schema).is_valid(output):
            self._response_failure(digest, ResponseFailure.SCHEMA)
        # Persist only bounded public evidence, never raw provider bodies.
        result = {
            "request_digest": digest,
            "output": output,
            "provider": self.provider,
            "model": self.model,
            "generated_at": time.time(),
            "response_sha256": diagnostic["response_sha256"],
            "reserved_tokens": reserve,
            "usage": usage,
            "evidence_level": "current_model_retrospective_replay",
        }
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE requests SET status='complete',result=? WHERE owner=? AND digest=? AND status='failed_response'",
                (_json(result), self.authorization_id, digest),
            ).rowcount
            if updated != 1:
                raise DecisionProducerError("decision_response_ledger_conflict")
            exhausted = db.execute("SELECT 1 FROM response_diagnostics WHERE owner=? AND reason_code=? LIMIT 1",
                                   (self.authorization_id, ResponseFailure.OVERRUN.value)).fetchone()
            if not exhausted and all(value > 0 for value in usage.values()):
                # Subtract only this reservation's unused portion. Concurrent
                # reservations remain charged; completion and release are atomic.
                db.execute("UPDATE budget SET reserved=reserved-? WHERE id=?",
                           (reserve - sum(usage.values()), self.authorization_id))
            db.execute("UPDATE response_diagnostics SET reason_code='complete' WHERE owner=? AND digest=?",
                       (self.authorization_id, digest))
            db.commit()
        finally:
            db.close()
        return result
