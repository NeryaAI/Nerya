"""Create or update a recurring Nerya task schedule.

Standalone CLI usage::

    python -m nerya.skills.builtin.tasks.scripts.create_task --json \
      '{"cron":"0 11 * * *","task_type":"agent","source_request":"send me a daily digest","generated_prompt":"Run the daily digest workflow..."}'

The script only creates schedule rows. Runtime execution still goes through
the cron scheduler, approved script runner, agent session runner, and delivery
pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from nerya.core.config import load_config
from nerya.messaging.platforms import PLATFORM_IDS
from nerya.triggers.schedule import ScheduleEntry, load_schedules, save_schedules


def run(
    payload: dict[str, Any] | None = None,
    *,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    cfg = load_config(Path(workspace).expanduser() if workspace else None)

    task_type = _normalise_task_type(
        payload.get("task_type") or payload.get("session_kind")
    )
    script_id = _normalise_script_id(payload.get("script_id"))
    if task_type == "script" or script_id:
        session_kind = "script"
    else:
        session_kind = "agent"

    source_request = _clean(
        payload.get("source_request")
        or payload.get("user_request")
        or payload.get("request")
    )
    entry_payload = dict(payload.get("payload") or {})
    generated_prompt = _generated_prompt(payload, entry_payload, source_request)
    title = _clean(payload.get("title") or payload.get("name"))
    schedule_id = _clean(payload.get("id") or payload.get("schedule_id"))
    if not schedule_id:
        schedule_id = _generated_id(
            title or source_request or generated_prompt or script_id or session_kind
        )

    if session_kind == "agent":
        if not generated_prompt:
            return _error(
                "prompt or generated_prompt is required for agent tasks"
            )
        guard_reason = _recursive_schedule_guard_reason(
            schedule_id=schedule_id,
            title=title,
            source_request=source_request,
            generated_prompt=generated_prompt,
            entry_payload=entry_payload,
        )
        if guard_reason:
            return _error(guard_reason, code="recursive_schedule_blocked")
        entry_payload["prompt"] = generated_prompt
        if source_request:
            entry_payload.setdefault("source_request", source_request)
        entry_payload.setdefault(
            "prompt_source",
            "agent_generated" if _has_explicit_generated_prompt(payload, entry_payload)
            else "prompt_fallback",
        )
    else:
        if not script_id:
            script_id = _script_id_from_target(_clean(payload.get("target")))
        if not script_id:
            return _error("script_id is required for script tasks")
        if not _approved_script_exists(cfg.paths, script_id):
            return _error(
                "approved_script_not_found: script tasks must reference an "
                f"approved script id under scripts/approved ({script_id!r} "
                "was not found). For recurring monitoring, reporting, or "
                "research without an existing approved script, call "
                "task_create with task_type='agent' and generated_prompt "
                "instead of inventing a script_id.",
                code="approved_script_not_found",
            )
        entry_payload.setdefault("script_id", script_id)
        script_args = payload.get("script_args")
        if isinstance(script_args, dict):
            entry_payload.setdefault("args", dict(script_args))

    every_seconds = payload.get("every_seconds")
    cron = _clean(payload.get("cron"))
    if every_seconds is not None:
        every_seconds = int(every_seconds)

    target = _clean(payload.get("target"))
    if not target:
        target = f"script:{script_id}" if session_kind == "script" else "agent"
    elif session_kind == "script":
        target = f"script:{script_id}"

    delivery_targets = _delivery_targets(payload.get("delivery_targets"))
    delivery_targets = _filter_unrequested_delivery_targets(
        delivery_targets,
        source_request=_clean(payload.get("source_request")),
    )
    if not delivery_targets:
        delivery_targets = _delivery_targets_from_source_request(
            _clean(payload.get("source_request"))
        )

    entry_kwargs: dict[str, Any] = {
        "id": schedule_id,
        "kind": _clean(payload.get("kind")) or f"{session_kind}.task",
        "target": target,
        "payload": entry_payload,
        "enabled": bool(payload.get("enabled", True)),
        "timezone": _clean(payload.get("timezone")) or None,
        "strategy_id": _clean(payload.get("strategy_id")) or None,
        "session_kind": session_kind,
        "attached_skills": list(payload.get("attached_skills") or []),
        "delivery_targets": delivery_targets,
        "session_ttl_seconds": payload.get("session_ttl_seconds"),
    }
    if cron:
        entry_kwargs["cron"] = cron
    if every_seconds is not None:
        entry_kwargs["every_seconds"] = every_seconds
    if payload.get("starts_at") is not None:
        entry_kwargs["starts_at"] = payload.get("starts_at")
    if payload.get("ends_at") is not None:
        entry_kwargs["ends_at"] = payload.get("ends_at")
    if session_kind == "agent":
        entry_kwargs["session_mode"] = payload.get("session_mode") or "ephemeral"
        if payload.get("session_id"):
            entry_kwargs["session_id"] = payload.get("session_id")
        if payload.get("session_ids"):
            entry_kwargs["session_ids"] = list(payload.get("session_ids") or [])

    entry = ScheduleEntry(**entry_kwargs)
    entries = load_schedules(cfg.paths)
    existed = any(e.id == entry.id for e in entries)
    next_entries = [e for e in entries if e.id != entry.id]
    next_entries.append(entry)
    save_schedules(cfg.paths, next_entries)

    return {
        "ok": True,
        "created": not existed,
        "updated": existed,
        "task_id": entry.id,
        "schedule": _entry_dict(entry),
        "next_action": (
            "Use scripts/list_tasks.py or /triggers/schedules/status to verify "
            "next run and enabled state."
        ),
    }


def _entry_dict(entry: ScheduleEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "target": entry.target,
        "enabled": entry.enabled,
        "cron": entry.cron,
        "every_seconds": entry.every_seconds,
        "timezone": entry.timezone,
        "session_kind": entry.session_kind,
        "session_mode": entry.session_mode,
        "session_id": entry.session_id,
        "session_ids": list(entry.session_ids or []),
        "payload": dict(entry.payload or {}),
        "delivery_targets": [dict(t) for t in entry.delivery_targets or []],
    }


def _normalise_task_type(value: Any) -> str:
    raw = _clean(value).lower()
    if raw in {"script", "approved_script"}:
        return "script"
    return "agent"


def _generated_prompt(
    payload: dict[str, Any],
    entry_payload: dict[str, Any],
    source_request: str,
) -> str:
    prompt = _clean(
        payload.get("generated_prompt")
        or payload.get("task_prompt")
        or payload.get("agent_prompt")
        or entry_payload.get("generated_prompt")
        or entry_payload.get("task_prompt")
        or entry_payload.get("agent_prompt")
    )
    if prompt:
        return prompt
    legacy_prompt = _clean(payload.get("prompt") or entry_payload.get("prompt"))
    return legacy_prompt or source_request


def _has_explicit_generated_prompt(
    payload: dict[str, Any],
    entry_payload: dict[str, Any],
) -> bool:
    return any(
        _clean(source.get(key))
        for source in (payload, entry_payload)
        for key in ("generated_prompt", "task_prompt", "agent_prompt")
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalise_script_id(value: Any) -> str:
    raw = _clean(value).strip("'\"")
    if not raw:
        return ""
    if raw.startswith("script:"):
        return _normalise_script_id(raw.split(":", 1)[1])
    parts = [part for part in re.split(r"[\\/]+", raw) if part and part != "."]
    if any(part == ".." for part in parts):
        return ""
    lowered = [part.lower() for part in parts]
    for idx in range(0, max(0, len(parts) - 2)):
        if lowered[idx] == "scripts" and lowered[idx + 1] == "approved":
            return _safe_script_id(parts[idx + 2])
    if len(parts) > 1:
        return ""
    candidate = Path(raw).stem if raw.endswith(".py") else raw
    return _safe_script_id(candidate)


def _safe_script_id(value: str) -> str:
    safe = _clean(value)
    if not safe or safe != Path(safe).name:
        return ""
    if ":" in safe or "/" in safe or "\\" in safe:
        return ""
    return safe


def _script_id_from_target(target: str) -> str:
    if target.startswith("script:"):
        return _normalise_script_id(target.split(":", 1)[1])
    return _normalise_script_id(target)


def _approved_script_exists(paths: Any, script_id: str) -> bool:
    safe = _normalise_script_id(script_id)
    if not safe:
        return False
    script_dir = Path(paths.scripts_approved) / safe
    if not script_dir.is_dir():
        return False
    manifest = script_dir / "manifest.yml"
    if manifest.exists():
        text = manifest.read_text(encoding="utf-8", errors="ignore").lower()
        return "state: approved" in text or "state:approved" in text
    return any(script_dir.glob("*.py"))


def _recursive_schedule_guard_reason(
    *,
    schedule_id: str,
    title: str,
    source_request: str,
    generated_prompt: str,
    entry_payload: dict[str, Any],
) -> str:
    """Block recurring agent prompts that mint more recurring agent prompts.

    This is a tool-boundary safety invariant, not model routing: recurring
    schedules are allowed, but their durable prompt must not instruct each tick
    to create more schedules/tasks or clone itself.
    """

    text = _guard_text(
        schedule_id,
        title,
        source_request,
        generated_prompt,
        *(str(v) for v in entry_payload.values() if isinstance(v, (str, int, float))),
    )
    compact = re.sub(r"\s+", "", text.lower())
    creates_schedule = (
        "task_create" in compact
        or "create_task" in compact
        or "createschedule" in compact
        or "createanewschedule" in compact
        or "createnewschedule" in compact
        or "createnewtask" in compact
        or "createarecurring" in compact
        or "创建schedule" in compact
        or "创建一个schedule" in compact
        or "创建新的schedule" in compact
        or "创建定时任务" in compact
        or "创建一个定时任务" in compact
        or "创建新的定时任务" in compact
        or "创建调度" in compact
        or "创建任务" in compact
        or "新建schedule" in compact
        or "新建定时任务" in compact
    )
    repeats_itself = (
        "eachtick" in compact
        or "everytick" in compact
        or "on-each-tick" in compact
        or "oneachrun" in compact
        or "everyrun" in compact
        or "selfreplicat" in compact
        or "forkbomb" in compact
        or "recursive" in compact
        or "recursion" in compact
        or "clone" in compact
        or "sameprompt" in compact
        or "每tick" in compact
        or "每个tick" in compact
        or "每次tick" in compact
        or "每次运行" in compact
        or "每次执行" in compact
        or "每次触发" in compact
        or "每次复制" in compact
        or "自复制" in compact
        or "复制自己" in compact
        or "递归" in compact
        or "无限" in compact
        or "完全一致" in compact
    )
    if creates_schedule and repeats_itself:
        return (
            "recursive_schedule_blocked: recurring agent schedules may not "
            "instruct each tick/run to create more schedules or clone the same "
            "prompt. Use one stable schedule, or an explicitly bounded chain "
            "with a max depth and operator-visible stop condition."
        )
    return ""


def _guard_text(*parts: str) -> str:
    return "\n".join(str(part or "") for part in parts if str(part or "").strip())


def _generated_id(seed: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", seed.lower()).strip("_")
    digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:8]
    if not slug:
        slug = "task"
    return f"task_{slug[:40]}_{digest}"


def _delivery_targets(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str) and value.strip():
        return [{"kind": "gateway", "platform": value.strip()}]
    if isinstance(value, dict):
        if not value:
            return []
        if set(value) == {"item"}:
            return _delivery_targets(value.get("item"))
        for text_key in ("$text", "text"):
            if set(value) == {text_key}:
                return _delivery_targets(value.get(text_key))
        target = _normalise_delivery_target_dict(value)
        return [target] if target else []
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            out.extend(_delivery_targets(item))
        return out
    return []


_DEFAULT_DELIVERY_PLATFORMS = {"dashboard", "local"}


def _filter_unrequested_delivery_targets(
    targets: list[dict[str, Any]],
    *,
    source_request: str,
) -> list[dict[str, Any]]:
    if not targets or not source_request:
        return targets

    source = source_request.casefold()
    kept: list[dict[str, Any]] = []
    removed_any = False
    for target in targets:
        platform = _delivery_target_platform(target)
        if not platform or platform in _DEFAULT_DELIVERY_PLATFORMS:
            kept.append(target)
            continue
        if platform in source:
            kept.append(target)
            continue
        removed_any = True

    if kept:
        return kept
    if removed_any:
        return [{"kind": "gateway", "platform": "dashboard"}]
    return targets


def _delivery_targets_from_source_request(source_request: str) -> list[dict[str, Any]]:
    source = source_request.casefold()
    if not source:
        return []
    out: list[dict[str, Any]] = []
    for platform in PLATFORM_IDS:
        platform_id = str(platform or "").strip().casefold()
        if not platform_id or platform_id in _DEFAULT_DELIVERY_PLATFORMS:
            continue
        if platform_id in source:
            out.append({"kind": "gateway", "platform": platform_id})
    return out


def _delivery_target_platform(target: dict[str, Any]) -> str:
    for key in ("platform", "channel", "kind", "target"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    return ""


def _normalise_delivery_target_dict(value: dict[str, Any]) -> dict[str, Any]:
    item = dict(value)
    kind = _clean(item.get("kind")).lower()
    if (
        kind in {"telegram", "discord", "slack", "webhook"}
        and not item.get("platform")
        and not item.get("channel")
    ):
        item["kind"] = "gateway"
        item["platform"] = kind
    if not _delivery_target_platform(item):
        return {}
    return item


def _error(message: str, *, code: str | None = None) -> dict[str, Any]:
    result = {"ok": False, "error": message}
    if code:
        result["code"] = code
    return result


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--workspace", dest="workspace", default=None)
    args = parser.parse_args()

    try:
        result = run(_load_payload(args), workspace=args.workspace)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
