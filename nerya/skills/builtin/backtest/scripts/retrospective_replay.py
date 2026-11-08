"""Host orchestration for bounded current-model retrospective decision replay.

The candidate stays in an OS sandbox. Engine, data, ledger, authenticated tape,
and model authorization stay in the host. No live trading facades are exposed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .....core.atomic_write import atomic_write_bytes
from .....core.runtime_identity import BUILD_ID, SDK_BUILD_ID
from .....harness.cancellation import CancelledError
from .....strategies.verification import source_revision
from .....strategies.workflow_service import _read_files
from .decision_tape import DecisionTape, DecisionRequired, DecisionTapeError
from .engine import run_backtest
from .isolated_context import context_snapshot, dispatch_context
from .isolated_strategy import IsolatedStrategy

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "confidence"],
    "properties": {
        "decision": {"enum": ["ENTRY", "HOLD"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "drivers": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 500},
        },
        "veto_reason": {"type": "string", "maxLength": 1000},
    },
}


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def run_retrospective(
    package,
    cfg,
    *,
    candles_by_market,
    timeframe_candles_by_market,
    strategy_config,
    producer,
    host_key: bytes,
    tape_path: Path,
    role: str,
    role_prompt: str,
    max_output_tokens: int = 4096,
    check_cancel=None,
):
    """Return replay result plus producer evidence, or retain partial tape on error.

    v1 supports one explicitly selected confirmation role. No implicit role
    switching, live input fetching, tool calls, or fallback model is permitted.
    """
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 16384:
        raise DecisionTapeError("invalid decision output budget")
    if role not in package.manifest.subagents or not role_prompt.strip():
        raise DecisionTapeError("confirmation_role_not_bound")
    if package.manifest.extras.get("execution_mode", "script") != "script":
        raise DecisionTapeError("retrospective_requires_script_entrypoint")
    if not candles_by_market or sum(map(len, candles_by_market.values())) > 10_000:
        raise DecisionTapeError("historical_bar_limit")
    files, omitted = _read_files(package.root)
    if omitted:
        raise DecisionTapeError("source_snapshot_incomplete")
    if files.get("subagents/" + role + ".agent.md") != role_prompt:
        raise DecisionTapeError("role_prompt_source_mismatch")
    from .....core import yaml_io
    from .....strategies.package import _parse_manifest

    frozen_manifest = _parse_manifest(
        yaml_io.loads(files["strategy.yml"]), source=package.root / "strategy.yml"
    )
    if (
        frozen_manifest.asdict() != strategy_config
        or frozen_manifest.strategy_id != package.strategy_id
    ):
        raise DecisionTapeError("manifest_source_mismatch")
    revision = source_revision(files)
    binding = {
        "candidate_id": package.strategy_id,
        "source_revision": revision,
        "runtime_build_id": BUILD_ID,
        "sdk_build_id": SDK_BUILD_ID,
        "config_hash": _digest([cfg.asdict(), strategy_config]),
        "data_hash": _digest([candles_by_market, timeframe_candles_by_market]),
        "role_prompt_hash": _digest([role, role_prompt, VERDICT_SCHEMA]),
        "model_settings_hash": _digest(
            [producer.provider, producer.model, 0, max_output_tokens, producer.output_policy(VERDICT_SCHEMA)]
        ),
        "provider": producer.provider,
        "model": producer.model,
        "model_version": producer.model + ":provider-version-unreported",
    }
    tape_path = Path(tape_path)
    if tape_path.is_symlink():
        raise DecisionTapeError("unsafe_tape_path")
    raw = tape_path.read_bytes() if tape_path.exists() else None
    passes = 0
    while True:
        if check_cancel:
            check_cancel()
        if time.time() >= producer.deadline:
            raise DecisionTapeError("retrospective_deadline")
        current, skipped = _read_files(package.root)
        if skipped or source_revision(current) != revision:
            raise DecisionTapeError("source_changed")
        tape = (
            DecisionTape.verify(
                raw, binding=binding, host_key=host_key, max_receipts=producer.max_calls
            )
            if raw is not None
            else DecisionTape(
                binding, host_key=host_key, max_receipts=producer.max_calls
            )
        )
        pending = {}

        def decide(ctx, args, kwargs):
            requested = args[0] if args else kwargs.get("name")
            if requested != role or len(args) > 1 or set(kwargs) - {"name", "payload"}:
                raise DecisionTapeError("confirmation_role_or_arguments_mismatch")
            payload = kwargs.get("payload", {})
            if not isinstance(payload, dict):
                raise DecisionTapeError("invalid_confirmation_payload")
            request = tape.bind(
                timestamp_ms=ctx.clock.now_ms(),
                market=ctx.market_name,
                timeframe=ctx.config_obj.tf,
                role=role,
                payload=payload,
            )
            pending.update(request=request, payload=payload)
            # Record actual use separately from model/candidate assertions.
            ctx.subagents.call_counts["retrospective"] = (
                ctx.subagents.call_counts.get("retrospective", 0) + 1
            )
            return tape.consume(request)

        try:
            timeout = max(0.1, min(300, producer.deadline - time.time()))
            with IsolatedStrategy(
                package,
                frozen_files=files,
                lifecycle_timeout=timeout,
                cancel_token=getattr(producer, "cancel_token", None),
            ) as runner:
                if runner.safety.get("source_revision") != revision:
                    raise DecisionTapeError("executed_source_revision_mismatch")

                def tick(ctx):
                    return runner.run(
                        context_snapshot(ctx),
                        lambda method, args, kwargs: dispatch_context(
                            ctx, method, args, kwargs, decision=decide
                        ),
                    )

                result = run_backtest(
                    package.root,
                    cfg,
                    candles_by_market=candles_by_market,
                    timeframe_candles_by_market=timeframe_candles_by_market,
                    strategy_config=strategy_config,
                    run_fn=tick,
                    check_cancel=check_cancel,
                )
            proof = tape.finish()
            current, skipped = _read_files(package.root)
            if skipped or source_revision(current) != revision:
                raise DecisionTapeError("source_changed")
            raw = tape.dumps()
            atomic_write_bytes(tape_path, raw)
            return result, {
                **proof,
                "binding": binding,
                "tape_sha256": hashlib.sha256(raw).hexdigest(),
                "replay_passes": passes + 1,
                "isolation": runner.safety,
                "performance_evidence": False,
                "limitations": [
                    "Current model may know later events; not contemporaneous decisions.",
                    "Provider model version was not attested.",
                    "Historical fills are simulated, not live execution.",
                ],
            }
        except DecisionRequired as need:
            if not pending or pending["request"] != need.request:
                raise DecisionTapeError("uncaptured_decision_request")
            # No candidate process is alive when model credentials are used.
            inputs = dict(
                binding=need.request,
                role_prompt=role_prompt,
                payload={
                    "historical_timestamp_ms": need.request["timestamp_ms"],
                    "input": pending["payload"],
                },
                output_schema=VERDICT_SCHEMA,
                max_output_tokens=max_output_tokens,
            )
            expected_digest = producer.request_identity(**inputs)["digest"]
            try:
                answer = producer.produce(**inputs)
            except CancelledError as exc:
                # Count only the signed tape already persisted for this run,
                # not other requests sharing this authorization's ledger.
                exc.receipt = {
                    **(getattr(exc, "receipt", None) or {}),
                    "decision_count": len(json.loads(raw)["receipts"]) if raw is not None else 0,
                }
                raise
            tape.append_produced(
                need.request, result=answer, expected_request_digest=expected_digest
            )
            raw = tape.dumps()
            atomic_write_bytes(tape_path, raw)
            passes += 1
