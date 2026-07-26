"""Session-scoped placement for files authored during an Agent conversation.

The workspace contains canonical runtime trees (strategies, proposals, memory,
and so on), but free-form Agent output should not accumulate at its root. This
module gives native tools one stable place for those files while retaining an
explicit, auditable escape hatch for real deliverables and tool-required paths.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import to_workspace_relative


_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_UNSAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CONVERSATION_ROOT = Path("artifacts") / "conversations"
_CANONICAL_NEW_FILE_ROOTS = (Path("evolution") / "proposals",)


def conversation_storage_key(session_id: str) -> str:
    """Return a stable path component for trusted or caller-supplied ids."""

    raw = str(session_id or "").strip()
    if _SAFE_SESSION_ID_RE.fullmatch(raw) and raw not in {".", ".."}:
        return raw
    slug = _UNSAFE_COMPONENT_RE.sub("-", raw).strip("._-")[:48] or "session"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def conversation_files_dir(root: Path, session_id: str) -> Path:
    """Resolve the durable free-form file directory for one conversation."""

    return Path(root).resolve() / _CONVERSATION_ROOT / conversation_storage_key(session_id)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def is_canonical_new_file_path(path: Path, *, root: Path) -> bool:
    """Return whether a new file belongs to a runtime-owned staging tree."""

    root_resolved = Path(root).resolve()
    return any(
        _is_within(path, root_resolved / relative_root)
        for relative_root in _CANONICAL_NEW_FILE_ROOTS
    )


@dataclass(frozen=True)
class ConversationFilePlacement:
    path: Path
    kind: str
    outside_reason: str = ""


def place_new_conversation_file(
    requested_path: Path,
    *,
    root: Path,
    session_id: str | None,
    allow_outside_conversation: bool = False,
    outside_conversation_reason: str = "",
) -> ConversationFilePlacement:
    """Choose the destination for a new model-authored workspace file."""

    requested = requested_path.resolve()
    if not session_id:
        return ConversationFilePlacement(
            path=requested,
            kind="unscoped",
        )

    conversation_dir = conversation_files_dir(root, session_id)
    if _is_within(requested, conversation_dir):
        return ConversationFilePlacement(
            path=requested,
            kind="conversation",
        )
    if is_canonical_new_file_path(requested, root=root):
        return ConversationFilePlacement(
            path=requested,
            kind="canonical",
        )
    if allow_outside_conversation:
        reason = str(outside_conversation_reason or "").strip()
        if not reason:
            raise ValueError(
                "outside_conversation_reason is required when "
                "allow_outside_conversation=true"
            )
        return ConversationFilePlacement(
            path=requested,
            kind="explicit_exception",
            outside_reason=reason,
        )

    root_resolved = Path(root).resolve()
    relative = requested.relative_to(root_resolved)
    return ConversationFilePlacement(
        path=conversation_dir / relative,
        kind="conversation_reroute",
    )


def render_conversation_file_policy(root: Path, session_id: str | None) -> str:
    """Render the per-turn rule block injected into the system prompt."""

    if not session_id:
        return ""
    target = conversation_files_dir(root, session_id)
    relative = to_workspace_relative(target, root)
    return (
        "Conversation file policy:\n"
        f"- Current conversation directory: {relative}/\n"
        "- Put new free-form plans, notes, research, reports, downloads, "
        "screenshots, scratch scripts, logs, and intermediate outputs there; "
        "write_file and common shell writes enforce this placement.\n"
        "- Editing an existing file keeps its current path. Proposal-staged "
        "files keep their canonical evolution/proposals path.\n"
        "- Standard test and build commands may keep the project cwd because "
        "the build system owns their output paths.\n"
        "- Create a new file elsewhere only when the user requested that "
        "canonical destination or a tool/build requires it. For write_file or "
        "run_shell, set allow_outside_conversation=true and provide a concrete "
        "outside_conversation_reason; this is an approval-worthy exception."
    )


__all__ = [
    "ConversationFilePlacement",
    "conversation_files_dir",
    "conversation_storage_key",
    "is_canonical_new_file_path",
    "place_new_conversation_file",
    "render_conversation_file_policy",
]
