"""Convert upstream ``agent-plugins/<slug>`` to a Nerya workspace subagent.

Upstream shape::

    plugins/agent-plugins/<slug>/
    ├── .claude-plugin/plugin.json
    ├── .mcp.json
    ├── agents/
    │   └── <slug>.md          ← YAML frontmatter (name, description, tools) + body
    └── skills/<name>/SKILL.md ← duplicated from vertical-plugins by sync-script

Nerya target shape::

    <workspace>/subagents/
    ├── <snake>.agent.md       ← pure markdown, **no frontmatter**, starts with H1
    └── <snake>.role.yaml      ← {name, tier, allowed_skills}

We deliberately do *not* mutate ``workspace/_prompt_bundles/default/bundle.yml``:
:meth:`SubAgentRegistry.load_registry <nerya.subagents.registry.load_registry>`
glob-scans ``workspace/subagents/*.agent.md`` directly. Anything we drop
into that directory becomes available the moment the runtime reloads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .name_map import to_snake

#: YAML frontmatter regex for upstream ``agents/<slug>.md`` files. Their
#: frontmatter is YAML-shaped but we don't import a YAML library; the
#: keys we care about are simple ``key: value`` lines and we extract them
#: with a key-value scanner. Multi-line / quoted YAML is rare in this
#: corpus (the upstream copy is one flat key-per-line).
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*)", re.DOTALL)
_KV_RE = re.compile(r"^(?P<key>[a-zA-Z][\w-]*)\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_SKILLS_LIST_RE = re.compile(r"`([a-z0-9][a-z0-9-]*)`")

#: Allowed-skills *prefix* default. The dispatcher's own
#: ``SUBAGENT_SKILL_DENYLIST`` already filters out trading_write / wallet
#: / script_runtime, so granting ``operator`` + ``script`` here is safe
#: and matches what every existing analyst lane in
#: ``DEFAULT_SUBAGENT_SKILLS`` carries. We append the imported finance
#: skills on top so the agent loop can load them by id.
_DEFAULT_FINANCE_SUBAGENT_SKILLS: tuple[str, ...] = (
    "research", "research_report", "market_research", "analysis",
    "operator", "script", "websearch", "trace", "llm",
)


@dataclass(frozen=True)
class SubagentImport:
    upstream_md: Path
    nerya_name: str
    upstream_name: str
    description: str
    upstream_tools: tuple[str, ...]
    referenced_skill_ids: tuple[str, ...]
    allowed_skills: tuple[str, ...]
    tier: str
    prompt_text: str
    role_meta: dict[str, object] = field(default_factory=dict)

    @property
    def agent_md_path_rel(self) -> Path:
        return Path("subagents") / f"{self.nerya_name}.agent.md"

    @property
    def role_yaml_path_rel(self) -> Path:
        return Path("subagents") / f"{self.nerya_name}.role.yaml"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    fm = match.group("fm")
    body = match.group("body").strip()
    fm_dict: dict[str, str] = {}
    for kv in _KV_RE.finditer(fm):
        key = kv.group("key").lower()
        value = kv.group("value").strip().strip('"').strip("'")
        fm_dict[key] = value
    return fm_dict, body


def _extract_skill_refs(body: str) -> list[str]:
    """Pick up every ``\u0060<skill-id>\u0060`` mention in the prompt body.

    The upstream agent prompt tends to have a final "Skills this agent
    uses" section listing skills inside backticks; we use a forgiving
    regex on the whole body so newer doc styles also work.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _SKILLS_LIST_RE.finditer(body):
        token = match.group(1)
        if token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found


def _suggest_tier(role_name: str) -> str:
    """Heuristic — high-stakes / synthesis lanes get ``high`` tier."""
    if any(needle in role_name for needle in ("auditor", "reviewer", "screener", "kyc")):
        return "high"
    if any(needle in role_name for needle in ("researcher", "prep", "analyst", "writer")):
        return "medium"
    return "medium"


def derive_subagent(upstream_md: Path, *, vertical_namespace: str | None = None) -> SubagentImport:
    """Read an upstream ``agents/<slug>.md`` and produce the Nerya
    subagent record (including the prompt text we will write to disk)."""
    text = upstream_md.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)

    upstream_name = fm.get("name") or upstream_md.stem
    nerya_name = to_snake(upstream_name)
    description = fm.get("description", "")
    upstream_tools = tuple(
        t.strip() for t in fm.get("tools", "").split(",") if t.strip()
    )

    referenced_skills_raw = _extract_skill_refs(body)
    referenced_skills = tuple(to_snake(s) for s in referenced_skills_raw)

    namespaced_finance_skills: tuple[str, ...]
    if vertical_namespace:
        namespaced_finance_skills = tuple(
            f"finance.{vertical_namespace}.{s}" for s in referenced_skills
        )
    else:
        namespaced_finance_skills = referenced_skills

    allowed_skills = (
        _DEFAULT_FINANCE_SUBAGENT_SKILLS + namespaced_finance_skills
    )

    tier = _suggest_tier(nerya_name)

    prompt_text = _render_prompt_text(
        nerya_name=nerya_name,
        description=description,
        body=body,
        upstream_tools=upstream_tools,
        referenced_finance_skills=namespaced_finance_skills,
    )

    role_meta: dict[str, object] = {
        "name": nerya_name,
        "tier": tier,
        "allowed_skills": list(allowed_skills),
        "imported_from": {
            "upstream_repo": "financial-services",
            "upstream_name": upstream_name,
            "upstream_path": upstream_md.as_posix(),
            "imported_by": "finance_skills_importer/0.0.1",
        },
    }

    return SubagentImport(
        upstream_md=upstream_md,
        nerya_name=nerya_name,
        upstream_name=upstream_name,
        description=description,
        upstream_tools=upstream_tools,
        referenced_skill_ids=referenced_skills,
        allowed_skills=allowed_skills,
        tier=tier,
        prompt_text=prompt_text,
        role_meta=role_meta,
    )


def _render_prompt_text(
    *,
    nerya_name: str,
    description: str,
    body: str,
    upstream_tools: tuple[str, ...],
    referenced_finance_skills: tuple[str, ...],
) -> str:
    """Emit a Nerya-flavoured ``<name>.agent.md`` body.

    Convention (matches existing
    ``workspace/_prompt_bundles/default/subagents/<role>.agent.md``):

    * starts with H1 ``# <Role Name>``;
    * pure markdown — *no* YAML frontmatter (Nerya subagent loader does
      not parse one);
    * the upstream prompt body is preserved verbatim (it is high-quality
      copywriting we don't want to paraphrase);
    * a short Nerya-flavoured "Operating envelope" section is appended
      so the lane knows about the safety boundary explicitly.
    """
    pretty = _pretty_role_name(nerya_name)

    lines: list[str] = [f"# {pretty}", ""]
    if description:
        lines.append(description)
        lines.append("")

    lines.append(body.rstrip())

    lines.append("")
    lines.append("## Operating envelope (Nerya 注入)")
    lines.append("")
    lines.append(
        "- 本 lane 仅负责分析与文档产出。下单 / 转账 / 链上发送 / 修改账本 / 对外消息"
        " 均必须显式调用 Nerya 对应 skill（`trading.submit_intent`、`messaging.pipeline`"
        "、`risk.check`、`approval_gate`），永远不在本 lane 内部绕过。"
    )
    lines.append(
        "- 涉及客户 PII / 敏感凭证的输入必须先过 `nerya.security.redaction`；"
        "输出文档前确认 PII redaction map 留痕。"
    )
    if upstream_tools:
        joined = ", ".join(f"`{t}`" for t in upstream_tools)
        lines.append(
            f"- 上游 `tools` 字段（仅作参考，Nerya 实际能力由 ``allowed_skills`` 决定）：{joined}"
        )
    if referenced_finance_skills:
        joined = ", ".join(f"`{s}`" for s in referenced_finance_skills)
        lines.append(f"- 调度上游同名 skill 时使用 Nerya 命名：{joined}")
    lines.append("")
    return "\n".join(lines)


def _pretty_role_name(snake: str) -> str:
    return " ".join(part.capitalize() for part in snake.split("_"))


def write_subagent(
    *,
    sub: SubagentImport,
    workspace_root: Path,
    dry_run: bool = True,
) -> tuple[Path, Path]:
    """Materialise the imported subagent. Returns ``(agent_md, role_yaml)`` paths."""
    agent_md = workspace_root / sub.agent_md_path_rel
    role_yaml = workspace_root / sub.role_yaml_path_rel
    if dry_run:
        return agent_md, role_yaml

    agent_md.parent.mkdir(parents=True, exist_ok=True)
    agent_md.write_text(sub.prompt_text, encoding="utf-8")
    role_yaml.write_text(_render_role_yaml(sub.role_meta), encoding="utf-8")
    return agent_md, role_yaml


def _render_role_yaml(role_meta: dict[str, object]) -> str:
    """Render the role-meta dict as YAML.

    Hand-rolled (no PyYAML at runtime). Subagent registry parses this
    via ``nerya.core.yaml_io`` — anything that round-trips through PyYAML
    is acceptable; we keep the output simple.
    """
    name = role_meta.get("name", "")
    tier = role_meta.get("tier", "medium")
    allowed = list(role_meta.get("allowed_skills") or [])
    imported = role_meta.get("imported_from") or {}

    lines: list[str] = [f"name: {name}", f"tier: {tier}"]
    lines.append("allowed_skills:")
    for skill in allowed:
        lines.append(f"  - {skill}")
    if isinstance(imported, dict) and imported:
        lines.append("imported_from:")
        for k, v in sorted(imported.items()):
            lines.append(f"  {k}: {v}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "SubagentImport",
    "derive_subagent",
    "write_subagent",
]
