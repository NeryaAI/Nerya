"""Self-configuration patches — the agent proposes mutations to its own
runtime configuration (``nerya.yml`` / ``agents.yml`` / ``workspace.yml`` /
``news_feeds.yml`` / ``messages/channels.yml``)
using the same proposal -> approval -> promotion pipeline that governs
strategies.

Nothing here auto-applies. Protected scopes (see
:data:`nerya.evolution.patch_proposal.PROTECTED_SCOPES`) are rejected
at proposal-creation time so the agent cannot even stage a patch
against risk limits, the kill switch, or credentials.

This module is intentionally small and declarative. It does NOT try
to mutate config in memory — the only legal effect is to write an
evolution proposal for an operator to approve.
"""

from __future__ import annotations

from typing import Any

from ..core import yaml_io
from ..core.errors import ProtectedScopeViolation
from ..core.paths import WorkspacePaths
from ..messaging.platforms import all_safe_field_keys, all_secret_field_keys
from .patch_proposal import (
    PROTECTED_SCOPES,
    Proposal,
    create_proposal,
    is_protected,
)

_ALLOWED_TARGETS = frozenset({
    "nerya.yml",
    "agents.yml",
    "workspace.yml",
    # Declarative dashboard UI.  Keep the canonical path explicit and retain
    # the prototype alias so old workspaces can be migrated through the same
    # proposal-only lane without opening a direct file-write escape hatch.
    "ui/workspace.yml",
    "workspace/ui.yml",
    "news_feeds.yml",
    "news_feeds.yaml",
    "messages/channels.yml",
    "messages/channels.yaml",
    "triggers/routes.yml",   # non-protected sections only (handled by is_protected)
    "policies/planner.yml",
    "policies/tier_policy.yml",
    # Skill allow-list: which builtin / installed skills the agent may
    # load. Capability changes, so proposal-only (never a live edit).
    "skills/enabled.yml",
    "skills/enabled.yaml",
})


_MISSING = object()
_MESSAGE_CHANNEL_SAFE_KEYS = all_safe_field_keys() | {"kind"}
_MESSAGE_CHANNEL_SECRET_TO_REF = {
    "bot_token": "bot_token_ref",
    "token": "token_ref",
    "webhook_url": "webhook_url_ref",
    "incoming_webhook_url": "incoming_webhook_url_ref",
    "url": "url_ref",
    "status_webhook_url": "status_webhook_url_ref",
    "status_url": "status_webhook_url_ref",
    "auth_header": "auth_header_ref",
}
_MESSAGE_CHANNEL_SECRET_TO_REF.update(all_secret_field_keys())


def _safe_ref_slug(value: Any) -> str:
    text = str(value or "channel").strip().lower()
    chars: list[str] = []
    last_was_sep = False
    for ch in text:
        if ch.isalnum():
            chars.append(ch)
            last_was_sep = False
        elif not last_was_sep:
            chars.append("_")
            last_was_sep = True
    slug = "".join(chars).strip("_")
    return slug or "channel"


def _unwrap_provider_item(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"item"}:
        return _unwrap_provider_item(value.get("item"))
    if isinstance(value, list):
        return [_unwrap_provider_item(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _unwrap_provider_item(v) for k, v in value.items()}
    return value


def _coerce_config_scalar(value: Any) -> Any:
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
    return value


def _as_config_list(value: Any) -> list[str]:
    value = _unwrap_provider_item(value)
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(",", " ").split() if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalise_channel_kind(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")
    if text in {"discord_webhook", "discord_webhook_url"}:
        return "discord"
    if text in {"telegram_bot", "telegram_polling"}:
        return "telegram"
    if text.endswith("_webhook"):
        base = text[:-len("_webhook")]
        if base in {"discord", "slack", "webhook"}:
            return base
    return text


def _infer_channel_kind(channel_id: Any, channel: dict[str, Any]) -> str:
    channel_name = _normalise_channel_kind(channel_id)
    for known in ("discord", "telegram", "slack", "webhook"):
        if channel_name == known or channel_name.startswith(f"{known}_"):
            return known
    if any(
        key in channel
        for key in (
            "webhook_url",
            "webhook_url_ref",
            "incoming_webhook_url",
            "incoming_webhook_url_ref",
            "url",
            "url_ref",
        )
    ):
        return "webhook"
    if any(key in channel for key in ("bot_token", "bot_token_ref", "token", "token_ref")):
        return "telegram"
    return ""


def _vault_ref_for(channel_id: Any, secret_key: str) -> str:
    return f"vault://gateway_{_safe_ref_slug(channel_id)}_{_safe_ref_slug(secret_key)}"


def _normalise_ref_value(channel_id: Any, secret_key: str, value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("vault://"):
        return text
    return _vault_ref_for(channel_id, secret_key)


def _normalise_secret_refs(channel_id: Any, channel: dict[str, Any], kind: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for secret_key, ref_key in _MESSAGE_CHANNEL_SECRET_TO_REF.items():
        target_secret_key = secret_key
        target_ref_key = ref_key
        if secret_key == "url" and kind in {"discord", "slack"}:
            target_secret_key = "webhook_url"
            target_ref_key = "webhook_url_ref"
        for candidate in (ref_key, secret_key, f"{secret_key}_env", f"{secret_key}_default"):
            if candidate not in channel:
                continue
            value = channel.get(candidate)
            if value is None or str(value).strip().lower() in {"", "null", "none", "unset"}:
                continue
            out.setdefault(
                target_ref_key,
                _normalise_ref_value(channel_id, target_secret_key, value),
            )
            break
    delivery_targets = channel.get("delivery_targets")
    if (
        not any(key in out for key in ("webhook_url_ref", "url_ref", "incoming_webhook_url_ref"))
        and kind in {"discord", "slack", "webhook"}
        and isinstance(delivery_targets, dict)
        and delivery_targets.get("target_id")
    ):
        out["webhook_url_ref"] = _vault_ref_for(channel_id, "webhook_url")
    return out


def _normalise_channel_topics(channel: dict[str, Any]) -> list[str]:
    for key in ("topics", "events", "event_topics", "trigger_kinds"):
        topics = _as_config_list(channel.get(key))
        if topics:
            return topics
    return []


def _extract_message_format_fields(channel: dict[str, Any]) -> dict[str, Any]:
    message_format = _unwrap_provider_item(channel.get("message_format"))
    if not isinstance(message_format, dict):
        return {}
    out: dict[str, Any] = {}
    title = message_format.get("title")
    if title is not None and str(title).strip():
        out["label"] = str(title).strip()
    username = message_format.get("username")
    if username is not None and str(username).strip():
        out["username"] = str(username).strip()
    return out


def _looks_like_secret_source_note(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "env var",
            "environment variable",
            "vault://",
            "api key",
            "apikey",
            "webhook_url",
            "bot_token",
            "secret",
            "token",
        )
    )


def _normalise_message_channel(channel_id: Any, raw_channel: dict[str, Any]) -> dict[str, Any]:
    channel = {str(k): _unwrap_provider_item(v) for k, v in raw_channel.items()}
    kind_value = (
        channel.get("kind")
        or channel.get("type")
        or channel.get("channel_type")
        or channel.get("platform")
    )
    kind = _normalise_channel_kind(kind_value) or _infer_channel_kind(channel_id, channel)
    out: dict[str, Any] = {}
    if kind:
        out["kind"] = kind
    for key in sorted(_MESSAGE_CHANNEL_SAFE_KEYS):
        if key in {"kind", "topics"} or key not in channel:
            continue
        value = _unwrap_provider_item(channel[key])
        if value is None or str(value).strip().lower() in {"", "null", "none", "unset"}:
            continue
        if key in {
            "enabled",
            "disabled",
            "polling",
            "trade_notifications",
            "approvals",
            "markdown",
            "disable_web_page_preview",
            "auto_reply",
            "allow_unknown_users",
            "group_sessions_per_user",
            "thread_sessions_per_user",
        }:
            out[key] = _coerce_config_scalar(value)
        elif key in {"allowed_chat_ids", "allowed_user_ids", "allowed_users", "denied_user_ids"}:
            values = _as_config_list(value)
            if values:
                out[key] = values
        elif key == "timeout_s":
            try:
                timeout = float(value)
            except (TypeError, ValueError):
                continue
            if timeout > 0:
                out[key] = timeout
        elif key == "chat_id" and str(value).strip().startswith("vault://"):
            continue
        elif key == "description" and _looks_like_secret_source_note(value):
            continue
        else:
            out[key] = value if isinstance(value, bool) else str(value).strip()
    chat_id_ref = _unwrap_provider_item(channel.get("chat_id_ref"))
    chat_id = _unwrap_provider_item(channel.get("chat_id"))
    if isinstance(chat_id_ref, str) and chat_id_ref.strip():
        out["chat_id_ref"] = _normalise_ref_value(channel_id, "chat_id", chat_id_ref)
    elif isinstance(chat_id, str) and chat_id.strip().startswith("vault://"):
        out["chat_id_ref"] = _normalise_ref_value(channel_id, "chat_id", chat_id)
    for key, value in _extract_message_format_fields(channel).items():
        out.setdefault(key, value)
    topics = _normalise_channel_topics(channel)
    if topics:
        out["topics"] = topics
    for key, value in _normalise_secret_refs(channel_id, channel, kind).items():
        out[key] = value
    return out


def _normalise_severity_routes(config_after: dict[str, Any], channels: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def route_targets(value: Any) -> list[str]:
        value = _unwrap_provider_item(value)
        if isinstance(value, str) and value.strip().lower() in {
            "",
            "none",
            "silent",
            "drop",
            "suppress",
        }:
            return []
        return _as_config_list(value)

    def add(severity: Any, target_value: Any) -> None:
        severity_text = str(_unwrap_provider_item(severity) or "").strip().lower()
        if not severity_text:
            return
        values = route_targets(target_value)
        current = out.setdefault(severity_text, [])
        for value in values:
            if value not in current:
                current.append(value)

    def add_rule(severity: Any, rule: Any) -> None:
        rule = _unwrap_provider_item(rule)
        if isinstance(rule, dict):
            add(
                severity,
                rule.get("channels")
                or rule.get("targets")
                or rule.get("delivery_targets")
                or rule.get("route"),
            )
            return
        add(severity, rule)

    routes = config_after.get("severity_routes")
    if not isinstance(routes, dict):
        routing = _unwrap_provider_item(config_after.get("routing"))
        if isinstance(routing, dict):
            for key in ("severity", "by_severity", "severity_routes", "severity_routing"):
                if isinstance(routing.get(key), dict):
                    routes = routing[key]
                    break
    if isinstance(routes, dict):
        for severity, rule in routes.items():
            add_rule(severity, rule)

    severity_routing = _unwrap_provider_item(config_after.get("severity_routing"))
    if isinstance(severity_routing, dict):
        rules = _unwrap_provider_item(severity_routing.get("rules"))
        if isinstance(rules, dict):
            for severity, rule in rules.items():
                add_rule(severity, rule)
        for severity, rule in severity_routing.items():
            if severity in {"rules", "default_channels", "silent_suppresses_push"}:
                continue
            add_rule(severity, rule)
        default_channels = severity_routing.get("default_channels")
        if default_channels is not None:
            add("*", default_channels)
        if (
            str(severity_routing.get("silent_suppresses_push") or "").strip().lower()
            in {"1", "true", "yes", "on"}
            and "silent" not in out
        ):
            out["silent"] = []

    raw_routes = _unwrap_provider_item(config_after.get("routes"))
    if isinstance(raw_routes, dict):
        for route_id, route in raw_routes.items():
            if not isinstance(route, dict):
                continue
            severity = (
                route.get("severity")
                or route.get("min_severity")
                or route.get("level")
                or route_id
            )
            add(severity, route.get("channels") or route.get("targets"))

    for channel_id, channel in channels.items():
        if not isinstance(channel, dict):
            continue
        severity = channel.get("severity") or channel.get("min_severity")
        if severity:
            add(severity, [channel_id])
    return out


def _normalise_message_channels_config(config_after: dict[str, Any]) -> dict[str, Any]:
    channels = config_after.get("channels")
    if not isinstance(channels, dict):
        channel_entries: dict[str, Any] = {}
        routing_keys = {
            "version",
            "severity_routes",
            "severity_routing",
            "routing",
            "routes",
        }
        for key, value in config_after.items():
            if key in routing_keys or not isinstance(value, dict):
                continue
            kind = (
                _normalise_channel_kind(value.get("kind"))
                or _infer_channel_kind(key, value)
            )
            if kind:
                channel_entries[str(key)] = value
        if channel_entries:
            wrapped = {"channels": channel_entries}
            for key in routing_keys:
                if key in config_after:
                    wrapped[key] = config_after[key]
            return _normalise_message_channels_config(wrapped)
        severity_routes = _normalise_severity_routes(config_after, {})
        if severity_routes:
            return {"severity_routes": severity_routes}
        return config_after
    normalised: dict[str, Any] = {}
    if "version" in config_after:
        normalised["version"] = _coerce_config_scalar(_unwrap_provider_item(config_after["version"]))
    out_channels: dict[str, Any] = {}
    for channel_id, raw_channel in channels.items():
        if not isinstance(raw_channel, dict):
            out_channels[str(channel_id)] = raw_channel
            continue
        out_channels[str(channel_id)] = _normalise_message_channel(channel_id, raw_channel)
    normalised["channels"] = out_channels
    severity_routes = _normalise_severity_routes(config_after, channels)
    if severity_routes:
        normalised["severity_routes"] = severity_routes
    return normalised


def _normalise_core_config_after(target: str, config_after: dict[str, Any]) -> dict[str, Any]:
    if target in {"messages/channels.yml", "messages/channels.yaml"}:
        return _normalise_message_channels_config(config_after)
    if target in {"ui/workspace.yml", "workspace/ui.yml"}:
        # Import lazily so the general self-config module remains usable in
        # minimal CLI contexts that do not load dashboard support.
        from ..workspace.ui import validate_manifest, WorkspaceUiError

        checked = validate_manifest(config_after)
        if not checked.ok:
            raise WorkspaceUiError(
                "dashboard UI manifest validation failed: "
                + "; ".join(checked.errors)
            )
        return checked.manifest
    return config_after


def _value_at_path(data: dict[str, Any] | None, dotted: str) -> Any:
    cur: Any = data if isinstance(data, dict) else {}
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _require_non_protected_keys(
    target: str,
    config_after: dict[str, Any],
    *,
    current_config: dict[str, Any] | None = None,
) -> None:
    """Reject a proposed patch that would change a protected *sub*-key.

    Protected scopes can encode sub-keys with ``:`` notation (e.g.
    ``nerya.yml:runtime.live_trading_enabled``). Full-file proposals often
    include unchanged defaults, so presence alone is not enough to reject.
    The protected boundary is a value change or a new protected key.
    """
    flat = _flatten(config_after)
    for key in flat:
        protected_key = f"{target}:{key}"
        if is_protected(protected_key):
            current = _value_at_path(current_config, key)
            after = _value_at_path(config_after, key)
            if current is not _MISSING and current == after:
                continue
            raise ProtectedScopeViolation(
                f"proposed patch touches protected sub-key {protected_key!r}; "
                f"protected set={sorted(PROTECTED_SCOPES)}"
            )


def _flatten(d: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(d, dict):
        for k, v in d.items():
            sub = f"{prefix}.{k}" if prefix else str(k)
            out.append(sub)
            out.extend(_flatten(v, sub))
    return out


def propose_core_config_patch(
    paths: WorkspacePaths,
    *,
    target: str,
    summary: str,
    config_after: dict[str, Any],
    rationale: str = "",
    current_config: dict[str, Any] | None = None,
) -> Proposal:
    """Propose a mutation to a runtime config file.

    :param target: posix-relative path of the file inside the workspace
        (e.g. ``"nerya.yml"``).
    :param config_after: the full YAML content the operator would end up
        with after applying the patch.
    :raises ProtectedScopeViolation: if ``target`` itself is protected
        or the proposed body touches a protected sub-key.
    :raises ValueError: if ``target`` isn't in :data:`_ALLOWED_TARGETS`.
    """
    if target not in _ALLOWED_TARGETS:
        raise ValueError(
            f"target {target!r} is not allowed for self-config patches; "
            f"allowed={sorted(_ALLOWED_TARGETS)}"
        )
    if is_protected(target):
        raise ProtectedScopeViolation(
            f"target {target!r} is a protected scope and cannot be patched "
            f"through the self-config surface"
        )
    config_after = _normalise_core_config_after(target, config_after)
    _require_non_protected_keys(
        target,
        config_after,
        current_config=current_config,
    )

    body = yaml_io.dumps(config_after)
    rationale_md = rationale or f"# Core config patch\n\nTarget: `{target}`\n\n{summary}\n"
    return create_proposal(
        paths,
        kind="core_config_patch",
        summary=summary,
        rationale=rationale_md,
        extra_files={
            f"after/{target}": body,
            "target.yml": yaml_io.dumps({"target": target}),
        },
        target=target,
    )


__all__ = ["propose_core_config_patch"]
