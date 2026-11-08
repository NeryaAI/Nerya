"""Read-only coordinator evidence from admitted work and host-recorded checks.

Command success describes execution, not the truth of the model's answer. This
projection never promotes prose or a saved verifier summary into a receipt, and
does not itself enforce final-answer correctness or alter command lifecycle.
"""
from __future__ import annotations

import json
import re
from contextlib import closing

from ..core.redaction import redact_text
from ..db.sqlite import connect_readonly
from .verifier import verify_hard

_CONTEXT_TASK_LIMIT = 13  # up to eight active tasks plus five recent tasks


def _object(value):
    try:
        value = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _delivery(command):
    delivery = _object(command['delivery_json'])
    if command['delivery_json'] and (delivery.get('protocol') != 'nerya.task_delivery.v1'
            or delivery.get('command_id') != command['command_id']
            or not isinstance(delivery.get('delivery_id'), str)
            or type(delivery.get('delivery_revision')) is not int
            or not isinstance(delivery.get('completion'), dict)):
        return {'delivery_id': '', 'delivery_revision': 0,
                'completion': {'allowed': False, 'status': 'invalid',
                               'reasons': ['invalid_delivery_record']}}
    return delivery


def _reasons(value):
    return [redact_text(x)[:256] for x in value[:64] if isinstance(x, str)] if isinstance(value, list) else []


def _artifacts(value):
    if not isinstance(value, list):
        return []
    artifacts = []
    for item in value[:32]:
        if not isinstance(item, dict) or not isinstance(item.get('artifact_id'), str):
            continue
        artifact = {key: redact_text(val)[:512] for key, val in item.items()
            if key in {'artifact_id', 'kind', 'label', 'ref', 'revision', 'status'} and isinstance(val, str)}
        ref = artifact.get('ref', '')
        if ref.startswith(('/', '\\', '~')) or (len(ref) > 1 and ref[1] == ':') or '..' in ref.split('/'):
            artifact['ref'] = '[redacted_path]'
        artifacts.append(artifact)
    return artifacts


def _complete(delivery):
    completion = delivery.get('completion') or {}
    return (completion.get('allowed') is True and completion.get('status') == 'complete'
            and not delivery.get('limitations'))


def _allowed_partial(delivery):
    completion = delivery.get('completion') or {}
    if (completion.get('allowed') is not True or completion.get('status') != 'partial'
            or completion.get('reasons') != []):
        return False
    # Reuse the producer floor; a saved verdict or limitations cannot waive it.
    from ..task_delivery import _completion
    try:
        current = _completion(delivery)
    except (TypeError, ValueError, AttributeError, KeyError):
        return False
    return current['allowed'] is True and current['status'] == 'partial'


def unresolved_deliveries(con, work, *, through=None):
    """Read owned records, never follow IDs or copy checks from old context."""
    rows = con.execute("""SELECT * FROM agent_commands WHERE session_id=? AND actor_id=?
        AND kind!='guide' AND delivery_json IS NOT NULL
        AND (? IS NULL OR created_at<? OR (created_at=? AND position<=?))
        ORDER BY created_at,position""", (work['task_session_id'], work['actor_id'],
            through['created_at'] if through is not None else None,
            through['created_at'] if through is not None else None,
            through['created_at'] if through is not None else None,
            through['position'] if through is not None else None))
    references = []
    for row in rows:
        delivery = _delivery(row)
        if not delivery or _complete(delivery):
            continue
        completion = delivery.get('completion') or {}
        references.append({
            'task_id': work['task_id'], 'session_id': row['session_id'],
            'command_id': row['command_id'], 'command_revision': row['revision'],
            'delivery_id': delivery['delivery_id'], 'delivery_revision': delivery['delivery_revision'],
            'reasons': _reasons(completion.get('reasons')),
            'artifacts': _artifacts(delivery.get('artifacts')),
        })
        if len(references) == 17:  # one overflow sentinel; callers disclose truncation
            break
    return references


def delivery_projection(con, work, command):
    """One publication contract; command success alone never proves delivery."""
    if command is not None and (command['session_id'] != work['task_session_id']
            or command['actor_id'] != work['actor_id']):
        # A malformed legacy row must not prevent other tasks from being read.
        return {'task_complete': False,
            'delivery_completion': {'allowed': False, 'status': 'invalid', 'reasons': ['command_binding_mismatch']},
            'unresolved_deliveries': [], 'unresolved_deliveries_truncated': False,
            'publication': {'verification_status': 'unverified', 'language': 'und',
                            'label_key': 'task_delivery_unverified'}}
    delivery = _delivery(command) if command is not None else {}
    unresolved = unresolved_deliveries(con, work, through=command)
    partial = bool(command is not None and command['state'] == 'succeeded'
                   and _allowed_partial(delivery))
    if partial:
        # This command delivered its verified scope. Prior commands still block
        # the task rollup, including prior partials with remaining scope.
        unresolved = [ref for ref in unresolved if ref['command_id'] != command['command_id']]
    complete = bool(command is not None and command['state'] == 'succeeded'
                    and _complete(delivery) and not unresolved)
    completion = delivery.get('completion') or {}
    reasons = list(dict.fromkeys(_reasons(completion.get('reasons'))
        + [reason for ref in unresolved for reason in ref['reasons']]
        + ([] if delivery else ['delivery_not_started'])))
    language = 'und'
    if command is not None:
        context = _object(command['context_json'])
        language = context.get('publication_language', 'und')
        if language not in {'zh', 'en'}:
            binding = _object(command['request_json']).get('assistant_request_binding') or {}
            binding = binding if isinstance(binding, dict) else {}
            parent = con.execute('SELECT * FROM agent_commands WHERE command_id=? AND session_id=? AND actor_id=?',
                (binding.get('command_id'), work['parent_session_id'], work['actor_id'])).fetchone()
            if parent is not None and parent['turn_id'] == binding.get('turn_id'):
                contract = _object(parent['context_json']).get('assistant_request')
                language = contract.get('language', 'und') if isinstance(contract, dict) else 'und'
    language = language if language in {'zh', 'en'} else 'und'
    return {'task_complete': complete,
        'delivery_completion': {'allowed': complete or (partial and not unresolved),
            'status': 'complete' if complete else 'partial' if unresolved else completion.get('status', 'unavailable'),
            'reasons': reasons[:64]},
        'limitations': _reasons(delivery.get('limitations'))[:16],
        'unresolved_deliveries': unresolved[:16], 'unresolved_deliveries_truncated': len(unresolved) > 16,
        'publication': {'verification_status': 'verified' if complete else 'unverified',
            'language': language, 'label_key': 'task_delivery_verified' if complete else 'task_delivery_unverified'}}


def task_command_projection(con, work, command):
    """Read bound command snapshots; history is not inherited verification."""
    from .command_store import CommandStore

    def project(row):
        public = CommandStore.public(row)
        delivery = public['delivery']
        if delivery is not None:
            if _delivery(row).get('protocol') != 'nerya.task_delivery.v1':
                public['delivery'] = None
            else:
                for check in delivery.get('checks', []):
                    if isinstance(check, dict) and not check.get('producer_owned') and check.get('evidence_ref'):
                        check.pop('evidence_ref', None)
        return {key: public[key] for key in
                ('command_id', 'session_id', 'turn_id', 'revision', 'state', 'kind', 'delivery')}

    if command is None or (command['session_id'] != work['task_session_id']
                           or command['actor_id'] != work['actor_id']):
        return {'command': None, 'delivery_history': [], 'delivery_history_truncated': False}
    rows = con.execute(
        """SELECT * FROM agent_commands WHERE session_id=? AND actor_id=?
        AND kind!='guide' AND delivery_json IS NOT NULL AND command_id!=?
        AND (created_at<? OR (created_at=? AND position<?))
        ORDER BY created_at DESC,position DESC LIMIT 17""",
        (work['task_session_id'], work['actor_id'], command['command_id'],
         command['created_at'], command['created_at'], command['position'])).fetchall()
    return {'command': project(command), 'delivery_history': [project(row) for row in rows[:16]],
            'delivery_history_truncated': len(rows) > 16}


def reported_result(command, text):
    return {'authority': 'untrusted_model_output', 'command_id': command['command_id'],
            'text': text, 'truncated': False}


def _result(record) -> dict:
    if record is None:
        return {}
    try:
        value = json.loads(record["result_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def command_verification(command) -> dict:
    """Reduce only this command's persisted host blocks, never its claims."""
    result = _result(command)
    blocks = result.get("blocks")
    blocks = blocks if isinstance(blocks, list) else []
    turn_id = command["turn_id"] if command is not None else None
    blocks = [row for row in blocks if isinstance(row, dict)
              and (not row.get("turn_id") or row["turn_id"] == turn_id)]
    _, evidence = verify_hard(blocks=blocks)
    checks = evidence["checks"]
    # Preserve the binding and status, not arbitrary producer text or commands.
    fields = ("artifact_id", "revision", "check_id", "status", "required", "binding", "evidence_ref")
    return {
        "command_id": command["command_id"] if command is not None else None,
        "hard_status": evidence["hard_status"],
        "has_hard_evidence": evidence["has_hard_evidence"],
        "scope": "Recorded checks for this command only; not task completion or artifact creation.",
        "checks": [{key: (redact_text(item[key])[:256] if isinstance(item[key], str)
                            and key != "revision" else item[key])
                    for key in fields if key in item} for item in checks[:16]],
        "checks_truncated": len(checks) > 16,
        "unverified": [redact_text(value) for value in evidence["unverified"][:16]],
        "unverified_truncated": len(evidence["unverified"]) > 16,
    }


def producer_receipt_history(con, *, sid: str, actor_id: str, current, paths=None) -> tuple[list[dict], bool]:
    """Return bounded host receipts from this conversation, never model claims."""
    rows = con.execute("""SELECT * FROM agent_commands WHERE session_id=? AND actor_id=?
        AND kind!='guide' AND command_id!=?
        AND (created_at<? OR (created_at=? AND position<?))
        AND EXISTS (
            SELECT 1 FROM (
                SELECT CASE WHEN envelope.type='object' THEN
                    CASE WHEN json_type(envelope.value,'$.block') IS NULL THEN envelope.value
                         WHEN json_type(envelope.value,'$.block')='object'
                         THEN json_extract(envelope.value,'$.block') ELSE '{}' END
                    ELSE '{}' END AS block,
                    CASE WHEN envelope.type='object'
                         THEN json_extract(envelope.value,'$.turn_id') END AS evidence_turn
                FROM json_each(CASE WHEN json_valid(agent_commands.result_json)
                    THEN agent_commands.result_json ELSE '{}' END,'$.blocks') AS envelope
            )
            WHERE json_extract(block,'$.kind')='tool_result'
                AND json_extract(block,'$.verification.protocol')='nerya.verification.v1'
                AND (evidence_turn IS NULL OR evidence_turn='' OR evidence_turn=agent_commands.turn_id)
        )
        ORDER BY created_at DESC,position DESC LIMIT 17""",
        (sid, actor_id, current["command_id"], current["created_at"],
         current["created_at"], current["position"])).fetchall()
    receipts = []
    for row in rows[:16]:
        verification = command_verification(row)
        checks = [item for item in verification["checks"]
                  if item.get("artifact_id") and item.get("revision")]
        if not verification["has_hard_evidence"] or not checks:
            continue
        safe_checks = []
        for item in checks[:16]:
            safe = {key: item[key] for key in
                    ("artifact_id", "revision", "check_id", "status", "required", "binding")
                    if key in item}
            if item.get("evidence_ref"):
                refs = _artifacts([{"artifact_id": item["artifact_id"],
                                    "ref": item["evidence_ref"]}])
                if refs and refs[0].get("ref"):
                    safe["evidence_ref"] = refs[0]["ref"]
            safe_checks.append(safe)
        observations = []
        if paths is not None and sum("observations" in item for item in receipts) < 2:
            from ..strategies.tool_verification import recorded_replay_observations
            observations = recorded_replay_observations(paths, row)
        receipts.append({
            "command_id": row["command_id"], "state": row["state"],
            "turn_id": row["turn_id"], "hard_status": verification["hard_status"],
            "checks": safe_checks, "scope": verification["scope"],
            "read_guidance": "Read the named producer receipt with its Skill/tool; do not glob, inspect journals, or repeat the unchanged producer call.",
            **({"observations": observations} if observations else {}),
        })
    return receipts[:8], len(rows) > 16


def failed_tool_recovery_history(con, *, sid: str, actor_id: str, current) -> tuple[list[dict], bool]:
    """Historical operation hints only; no inference from errors or command state."""
    rows = con.execute("""SELECT command_id,turn_id,block FROM (
        SELECT c.command_id,c.turn_id,c.created_at,c.position,envelope.key AS block_position,
            CASE WHEN envelope.type='object' THEN
                CASE WHEN json_type(envelope.value,'$.block') IS NULL THEN envelope.value
                     WHEN json_type(envelope.value,'$.block')='object'
                     THEN json_extract(envelope.value,'$.block') ELSE '{}' END
                ELSE '{}' END AS block,
            CASE WHEN envelope.type='object'
                 THEN json_extract(envelope.value,'$.turn_id') END AS evidence_turn
        FROM agent_commands c, json_each(CASE WHEN json_valid(c.result_json)
            THEN c.result_json ELSE '{}' END,'$.blocks') AS envelope
        WHERE c.session_id=? AND c.actor_id=? AND c.kind!='guide' AND c.command_id!=?
            AND (c.created_at<? OR (c.created_at=? AND c.position<?))
        ) WHERE json_extract(block,'$.kind')='tool_result'
            AND json_type(block,'$.ok')='false'
            AND json_type(block,'$.recovery')='object'
            AND json_type(block,'$.recovery.action')='text'
            AND length(json_extract(block,'$.recovery.action')) BETWEEN 1 AND 128
            AND json_extract(block,'$.recovery.action') NOT GLOB '*[^A-Za-z0-9_.:-]*'
            AND (evidence_turn IS NULL OR evidence_turn='' OR evidence_turn=turn_id)
        ORDER BY created_at DESC,position DESC,block_position DESC LIMIT 17""",
        (sid, actor_id, current["command_id"], current["created_at"],
         current["created_at"], current["position"])).fetchall()
    history = []
    for row in rows[:16]:
        block = json.loads(row["block"])
        origin = {"command_id": row["command_id"], "turn_id": row["turn_id"],
                  "call_id": block.get("call_id")}
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
               for value in origin.values()):
            continue
        action = block.get("action")
        if isinstance(action, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", action):
            origin["action"] = action
        recovery = block["recovery"]
        safe = {"action": recovery["action"]}
        for key in ("safe_to_retry", "retryable", "requires_approval", "requires_confirmation",
                    "requires_receipt_reconciliation", "execution_unknown",
                    "financial_execution_allowed", "owner_action_required"):
            if type(recovery.get(key)) is bool:
                safe[key] = recovery[key]
        diagnostics = recovery.get("diagnostics")
        if isinstance(diagnostics, dict):
            safe["diagnostics"] = {key: value for key, value in diagnostics.items()
                if key in {"provider", "action", "http_status", "error_code", "error_category",
                           "error_name", "owner_action_required"}
                and (type(value) in (int, bool) or isinstance(value, str)
                     and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value))}
        history.append({**origin, "recovery": safe,
            "scope": "Historical advisory for the recorded operation only; current preconditions unknown. "
                     "Not current verification, permission, status, or a provider ban."})
    return history, len(rows) > 16


def execution_context(paths, sid: str, command_id: str) -> str:
    """Bounded read-only handoff for a task's own next turn, not a new receipt.

    Coordinator context is scoped to parent conversations. Execution follow-ups
    need their own unfinished workflow IDs and evidence too, without borrowing
    another task's state or promoting an earlier model's success claims.
    """
    if not sid or not command_id:
        return ""

    def model_delivery(delivery):
        if not isinstance(delivery, dict):
            return None

        def fields(value, keys):
            return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else {}

        # Persisted model descriptions are not producer observations.
        result = fields(delivery, ("protocol", "delivery_id", "command_id", "delivery_revision",
                                   "skill_id", "workflow_id"))
        scope = delivery.get("requested_scope")
        if isinstance(scope, dict):
            result["requested_scope"] = fields(scope, ("authority", "limitations_are_authorization"))
            if scope.get("authority") == "admitted_request":
                result["requested_scope"].update(fields(scope, ("summary", "scope_hash")))
        if "completion" in delivery:
            result["completion"] = fields(delivery["completion"], ("allowed", "status", "reasons"))
        for key, keys in (
            ("stages", ("id", "state", "required_checks", "required_artifacts")),
            ("artifacts", ("artifact_id", "ref", "revision", "kind", "status")),
            ("checks", ("artifact_id", "revision", "check_id", "status", "required",
                        "binding", "evidence_ref", "producer_owned")),
        ):
            if isinstance(delivery.get(key), list):
                result[key] = [fields(row, keys) for row in delivery[key] if isinstance(row, dict)
                               and (key != "checks" or row.get("producer_owned") is True)]
        recovery = delivery.get("recovery")
        if isinstance(recovery, dict):
            # Keep retry/reconciliation/approval guards, including false flags.
            result["recovery"] = {key: value for key, value in recovery.items()
                                  if key == "action" or type(value) is bool}
        return result

    with closing(connect_readonly(paths.db)) as con:
        con.execute("BEGIN")
        work = con.execute("SELECT * FROM assistant_work WHERE task_session_id=?", (sid,)).fetchone()
        current = con.execute("SELECT * FROM agent_commands WHERE command_id=? AND session_id=?",
                              (command_id, sid)).fetchone()
        if current is None:
            return ""
        if work is not None and work["actor_id"] != current["actor_id"]:
            return ""
        deleted_scope = (sid, work["parent_session_id"]) if work is not None else (sid,)
        if con.execute(
                "SELECT 1 FROM agent_deleted_sessions WHERE session_id IN (%s)"
                % ",".join("?" for _ in deleted_scope), deleted_scope).fetchone():
            return ""
        actor_id = work["actor_id"] if work is not None else current["actor_id"]
        previous = con.execute("""SELECT * FROM agent_commands WHERE session_id=? AND actor_id=?
            AND kind!='guide' AND command_id!=? AND (created_at<? OR (created_at=? AND position<?))
            ORDER BY created_at DESC,position DESC LIMIT 1""",
            (sid, actor_id, command_id, current["created_at"], current["created_at"], current["position"])).fetchone()
        if previous is None and not current['delivery_json']:
            return ""
        deliveries = con.execute("""SELECT * FROM agent_commands WHERE session_id=? AND actor_id=?
            AND kind!='guide' AND delivery_json IS NOT NULL AND command_id!=?
            AND (created_at<? OR (created_at=? AND position<?))
            ORDER BY created_at DESC,position DESC LIMIT 3""",
            (sid, actor_id, command_id, current["created_at"], current["created_at"], current["position"])).fetchall()
        workflows = []
        for row in deliveries[:2]:
            delivery = _delivery(row)
            if not delivery or _complete(delivery):
                continue
            checks = delivery.get("checks") or []
            checks = [item for item in checks if isinstance(item, dict)] if isinstance(checks, list) else []
            scope = delivery.get("requested_scope") or {}
            workflows.append({
                "command_id": row["command_id"], "delivery_id": delivery.get("delivery_id"),
                "delivery_revision": delivery.get("delivery_revision"),
                "skill_id": redact_text(str(delivery.get("skill_id", "")))[:128],
                "workflow_id": redact_text(str(delivery.get("workflow_id", "")))[:128],
                "requested_scope": redact_text(str(scope.get("summary", "")))[:1200]
                    if isinstance(scope, dict) and scope.get("authority") == "admitted_request" else "",
                "completion_reasons": _reasons((delivery.get("completion") or {}).get("reasons")),
                "artifacts": [{key: value for key, value in artifact.items() if key != "label"}
                              for artifact in _artifacts(delivery.get("artifacts"))],
                "source_refs": _artifacts([{"artifact_id": item.get("artifact_id"),
                    "ref": item.get("evidence_ref", ""), "revision": item.get("revision", ""),
                    "status": item.get("status", "")}
                    for item in checks if item.get("producer_owned") and item.get("evidence_ref")]),
                "checks": [{key: redact_text(str(item[key]))[:256]
                    for key in ("artifact_id", "revision", "check_id", "status") if key in item}
                    for item in checks[:24] if item.get("producer_owned") is True],
                "checks_truncated": len(checks) > 24,
            })
        projection = task_command_projection(
            con, work or {"task_session_id": sid, "actor_id": actor_id}, current)
        history = projection["delivery_history"]
        # The UI keeps its full projection; model context needs only the latest
        # state of each workflow, not many copies of the same artifact/check set.
        latest, seen = [], set()
        for item in history:
            delivery = item.get("delivery") or {}
            key = (delivery.get("skill_id"), delivery.get("workflow_id"))
            if key not in seen and len(latest) < 2:
                latest.append(item)
                seen.add(key)
        projection["delivery_history"] = latest
        projection["delivery_history_truncated"] |= len(latest) < len(history)
        for item in [projection["command"], *latest]:
            if item is not None:
                item["delivery"] = model_delivery(item.get("delivery"))
        recoveries, recoveries_truncated = failed_tool_recovery_history(
            con, sid=sid, actor_id=actor_id, current=current)
        return json.dumps({
            "protocol": "nerya.task_continuation.v1",
            "task_id": work["task_id"] if work is not None else None,
            "session_id": sid, "command_id": command_id,
            "scope": "Prior owned records, not current verification, new permission, or task completion.",
            "continuation_guidance": (
                "command.delivery and delivery_history are persisted delivery snapshots from the command database, "
                "not filesystem artifacts. Never search the filesystem, journals, or logs for a delivery_id. "
                "Read the recorded completion status and reasons directly; use readonly receipt/report reads only. "
                "Do not finalize, write, or mutate delivery while reconstructing context."
            ),
            "current_verification": command_verification(current),
            **projection,
            "previous_command": {"command_id": previous["command_id"], "state": previous["state"],
                                 "verification": command_verification(previous)} if previous is not None else None,
            "producer_receipts": producer_receipt_history(
                con, sid=sid, actor_id=actor_id, current=current, paths=paths)[0],
            "failed_tool_recoveries": recoveries,
            "failed_tool_recoveries_truncated": recoveries_truncated,
            "unfinished_workflows": workflows, "workflow_history_truncated": len(deliveries) > 2,
        }, ensure_ascii=False)


def coordination_context(paths, sid: str) -> str:
    """Current scoped receipts, including unfinished work; no sync side effects."""
    with closing(connect_readonly(paths.db)) as con:
        con.execute('BEGIN')
        # One snapshot ties the state, command and evidence together. Pick an
        # active command before a queued follow-up, like assistant_task_read.
        rows = con.execute("""
            SELECT w.task_id,w.title,w.task_kind,w.task_session_id,
                   w.command_id AS creation_command_id,
                   c.command_id,c.state,c.revision,c.turn_id,c.result_json,
                   COUNT(*) OVER() AS total_tasks
            FROM assistant_work w
            JOIN assistant_sessions s ON s.session_id=w.parent_session_id
                AND s.assistant_id=w.assistant_id
            LEFT JOIN agent_commands c ON c.command_id=(
                SELECT command_id FROM agent_commands
                WHERE session_id=w.task_session_id AND kind!='guide'
                ORDER BY CASE WHEN state IN ('running','stopping','unconfirmed')
                    THEN 0 ELSE 1 END,created_at DESC,position DESC LIMIT 1)
            WHERE w.parent_session_id=?
                AND NOT EXISTS (SELECT 1 FROM agent_deleted_sessions
                    WHERE session_id IN (w.parent_session_id,w.task_session_id))
            ORDER BY CASE WHEN c.state IN
                ('queued','running','stopping','unconfirmed','awaiting_input','awaiting_approval')
                OR c.command_id IS NULL THEN 0 ELSE 1 END,w.created_at DESC,w.task_id
            LIMIT ?
        """, (sid, _CONTEXT_TASK_LIMIT)).fetchall()
        deliveries = {}
        for row in rows:
            work = con.execute('SELECT * FROM assistant_work WHERE task_id=?', (row['task_id'],)).fetchone()
            command = con.execute('SELECT * FROM agent_commands WHERE command_id=?', (row['command_id'],)).fetchone()
            deliveries[row['task_id']] = delivery_projection(con, work, command)
    tasks = []
    for row in rows:
        result = _result(row)
        answer = result.get("reply_text") or result.get("final_text")
        answer = redact_text(answer) if isinstance(answer, str) else ""
        tasks.append({
            **deliveries[row['task_id']],
            "task_id": row["task_id"], "title": redact_text(row["title"]),
            "task_kind": row["task_kind"], "task_session_id": row["task_session_id"],
            "creation_command_id": row["creation_command_id"],
            "command_id": row["command_id"], "command_revision": row["revision"],
            "state": row["state"] if row["command_id"] else "unconfirmed",
            "admission_confirmed": row["command_id"] is not None,
            "verification": command_verification(row),
            "reported_result": {
                "authority": "untrusted_model_output", "command_id": row["command_id"],
                "summary": answer[:1200], "truncated": len(answer) > 1200,
            } if answer else None,
        })
    total = rows[0]["total_tasks"] if rows else 0
    return json.dumps({
        "protocol": "nerya.assistant_coordination.v1", "parent_session_id": sid,
        "source": "assistant_work+agent_commands",
        "scope": "Existing task admissions and current command evidence; not receipts for a new request.",
        "total_tasks": total, "truncated": total > len(tasks), "tasks": tasks,
    }, ensure_ascii=False)
