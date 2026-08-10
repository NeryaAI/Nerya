"""Tests for ``scripts.finance_skills_importer`` (Phase A).

These tests focus on the deterministic / pure-function surface:

* ``name_map.to_snake`` and ``resolve_target`` (id + path conventions).
* ``frontmatter`` description composition + YAML rendering.
* ``transform.transform_skill_md`` end-to-end on the upstream
  ``private-equity/ic-memo`` SKILL.md (the same file we hand-converted
  during the manual smoke test, which proves a happy path).
* ``transform.apply_to_directory`` against the upstream PE vertical with
  ``dry_run=True`` to confirm the ten skills resolve cleanly without
  touching the operator workspace.
* ``command_to_route`` parsing of one upstream slash-command.
* ``agent_to_subagent`` conversion of one upstream agent-plugin.

We deliberately do not unit-test the ``cli`` glue here; that is exercised
indirectly through the dry-run round-trips below and a future Phase B
integration test will run the full CLI against a temp workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.skills.manifest import SkillManifest

from scripts.finance_skills_importer import (
    FRONTMATTER_END_MARKER,
    FRONTMATTER_START_MARKER,
)
from scripts.finance_skills_importer.agent_to_subagent import (
    derive_subagent,
    write_subagent,
)
from scripts.finance_skills_importer.builtin_promoter import (
    apply_to_builtin_directory,
    promote_finance_builtins,
)
from scripts.finance_skills_importer.command_to_route import (
    derive_route,
    parse_command_md,
    render_routes_yml,
)
from scripts.finance_skills_importer.frontmatter import (
    FrontmatterMeta,
    compose_nerya_description,
    extract_upstream_description,
    render_frontmatter_block,
)
from scripts.finance_skills_importer.name_map import (
    DO_NOT_IMPORT,
    KNOWN_VERTICALS,
    NERYA_BUILTIN_OVERLAPS,
    UnknownVerticalError,
    is_blocked,
    overlap_target,
    resolve_target,
    to_snake,
)
from scripts.finance_skills_importer.transform import (
    apply_to_directory,
    transform_skill_md,
)


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Locating the upstream financial-services checkout. We scan a couple of
# canonical locations so the test runs both from a developer machine and
# CI where the upstream repo sits as a sibling of Nerya/.
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_NERYA_REPO = _THIS_DIR.parent
_PROJECT_ROOT = _NERYA_REPO.parent

_UPSTREAM_CANDIDATES = [
    _PROJECT_ROOT / "financial-services",
    _NERYA_REPO.parent / "financial-services",
]


def _find_upstream() -> Path | None:
    for candidate in _UPSTREAM_CANDIDATES:
        marker = candidate / "plugins" / "vertical-plugins" / "private-equity" / "skills"
        if marker.is_dir():
            return candidate
    return None


_UPSTREAM_ROOT = _find_upstream()
_PE_DIR = (
    _UPSTREAM_ROOT / "plugins" / "vertical-plugins" / "private-equity"
    if _UPSTREAM_ROOT is not None
    else None
)

_skip_no_upstream = pytest.mark.skipif(
    _UPSTREAM_ROOT is None,
    reason="upstream financial-services repo not found alongside Nerya/",
)

_LEGACY_RULES_HEADER = "## " + "Nerya " + "\u5b88\u5219"


# ---------------------------------------------------------------------------
# name_map
# ---------------------------------------------------------------------------


class TestNameMap:
    def test_to_snake_basic(self) -> None:
        assert to_snake("ic-memo") == "ic_memo"
        assert to_snake("KYC-doc-parse") == "kyc_doc_parse"
        assert to_snake("3-statement-model") == "3_statement_model"

    def test_resolve_target_paths(self) -> None:
        target = resolve_target("private-equity", "ic-memo")
        assert target.skill_id == "ic_memo"
        assert target.namespace_id == "finance.private_equity.ic_memo"
        assert (
            target.rel_skill_md
            == Path("skills/finance/private_equity/ic_memo/SKILL.md")
        )

    def test_resolve_target_rejects_unknown_vertical(self) -> None:
        with pytest.raises(UnknownVerticalError):
            resolve_target("not-a-vertical", "ic-memo")

    def test_overlap_table_self_consistent(self) -> None:
        for (vertical, _skill), nerya_id in NERYA_BUILTIN_OVERLAPS.items():
            assert vertical in KNOWN_VERTICALS
            assert nerya_id and "/" not in nerya_id

    def test_overlap_target_lookup(self) -> None:
        # ``dcf-model`` is a known overlap with Nerya's dcf_valuation_skill
        assert overlap_target("financial-analysis", "dcf-model") == "dcf_valuation_skill"
        # ``initiating-coverage`` overlaps the lighter equity_research_skill
        assert (
            overlap_target("equity-research", "initiating-coverage")
            == "equity_research_skill"
        )
        # ``model-update`` and ``earnings-analysis`` are *new* lifecycle
        # capability Nerya does not have — they must NOT be flagged as
        # overlaps (otherwise ``import`` skips them).
        assert overlap_target("equity-research", "model-update") is None
        assert overlap_target("equity-research", "earnings-analysis") is None
        assert overlap_target("private-equity", "ic-memo") is None

    def test_do_not_import_allow_list(self) -> None:
        # ``skill-creator`` overlaps Nerya's own evolve / skill-authoring
        # surface — never auto-import.
        assert is_blocked("financial-analysis", "skill-creator")
        # Normal upstream skills are not blocked.
        assert not is_blocked("private-equity", "ic-memo")
        assert not is_blocked("financial-analysis", "comps-analysis")
        # Every entry in DO_NOT_IMPORT references a known vertical.
        for vertical, _skill in DO_NOT_IMPORT:
            assert vertical in KNOWN_VERTICALS

# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_extract_upstream_description(self) -> None:
        text = (
            "# Title\n\n"
            "description: Use whenever rebalancing. Triggers on 'rebalance'. "
            "Does the work end-to-end.\n\n"
            "## Workflow\n"
        )
        desc = extract_upstream_description(text)
        assert "Use whenever rebalancing" in desc
        assert "Triggers on" in desc
        assert "## Workflow" not in desc

    def test_compose_nerya_description_with_triggers(self) -> None:
        upstream_desc = (
            "Use when preparing for IC. Triggers on 'write IC memo', "
            "'investment committee memo'. Drafts the memo."
        )
        composed = compose_nerya_description(
            upstream_desc,
            upstream_vertical="private-equity",
            upstream_skill="ic-memo",
        )
        assert composed.lower().startswith("use ")
        assert "Triggers on" in composed
        assert "Adapted from" in composed
        assert "private-equity/ic-memo" in composed

    def test_render_frontmatter_block_round_trips_via_manifest(
        self, tmp_path: Path
    ) -> None:
        meta = FrontmatterMeta(
            name="ic_memo",
            description='Use when "writing" IC memos. Triggers on "write IC memo".',
        )
        block = render_frontmatter_block(meta)
        assert block.startswith(FRONTMATTER_START_MARKER)
        assert block.rstrip().endswith(FRONTMATTER_END_MARKER)
        # The double-quoted YAML scalar must escape inner double quotes.
        assert '\\"writing\\"' in block
        assert "risk_class:" not in block
        assert "adapted_from:" not in block
        assert "category:" not in block

        # Glue together a minimal SKILL.md and parse it with the real
        # Nerya manifest loader.
        skill_md = tmp_path / "ic_memo" / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(block + "\n# Investment Committee Memo\n", encoding="utf-8")

        manifest = SkillManifest.from_skill_md(skill_md)
        assert manifest.id == "ic_memo"
        assert manifest.title == "ic_memo"
        assert "writing" in manifest.description


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------


@_skip_no_upstream
class TestTransformIcMemo:
    """The upstream PE/ic-memo SKILL.md is the canonical happy path."""

    def setup_method(self) -> None:
        assert _PE_DIR is not None
        self.upstream_md = _PE_DIR / "skills" / "ic-memo" / "SKILL.md"
        assert self.upstream_md.is_file()
        self.target = resolve_target("private-equity", "ic-memo")
        self.upstream_text = self.upstream_md.read_text(encoding="utf-8")

    def test_transform_produces_loadable_skill_md(self, tmp_path: Path) -> None:
        nerya_text, upstream_desc, nerya_desc = transform_skill_md(
            self.upstream_text,
            self.target,
        )
        assert FRONTMATTER_START_MARKER in nerya_text
        assert FRONTMATTER_END_MARKER in nerya_text
        assert _LEGACY_RULES_HEADER not in nerya_text
        assert upstream_desc, "upstream description must be extracted"
        assert "Adapted from" in nerya_desc
        # The original upstream H1 must survive verbatim.
        assert "# Investment Committee Memo" in nerya_text

        # Dump it to disk and parse with the real Nerya loader.
        skill_md = tmp_path / "ic_memo" / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(nerya_text, encoding="utf-8")
        manifest = SkillManifest.from_skill_md(skill_md)
        assert manifest.id == "ic_memo"
        assert manifest.title == "ic_memo"
        assert "Adapted from" in manifest.description


@_skip_no_upstream
class TestPrivateEquityVerticalSnapshot:
    """Phase B's first vertical (private-equity) must transform cleanly."""

    def test_dry_run_resolves_all_pe_skills(self, tmp_path: Path) -> None:
        assert _PE_DIR is not None
        skill_dirs = sorted(
            d for d in (_PE_DIR / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        assert len(skill_dirs) == 10, (
            f"expected 10 PE skills upstream, got {len(skill_dirs)}: "
            f"{[d.name for d in skill_dirs]}"
        )

        produced_ids: set[str] = set()
        for skill_dir in skill_dirs:
            target = resolve_target("private-equity", skill_dir.name)
            result = apply_to_directory(
                upstream_skill_dir=skill_dir,
                target=target,
                workspace_root=tmp_path,
                dry_run=False,
            )
            assert result.target.skill_id == target.skill_id
            assert FRONTMATTER_START_MARKER in result.nerya_skill_md_text
            assert _LEGACY_RULES_HEADER not in result.nerya_skill_md_text
            produced_ids.add(target.skill_id)

            # Validate by round-tripping through SkillManifest.
            md = result.target.absolute_skill_md(tmp_path)
            assert md.is_file()
            manifest = SkillManifest.from_skill_md(md)
            assert manifest.id == target.skill_id

        # Ten unique skill ids; no collision after kebab → snake.
        assert len(produced_ids) == 10


@_skip_no_upstream
class TestWealthManagementVerticalSnapshot:
    """Phase C-2: wealth-management skills transform cleanly."""

    def test_resolves_six_skills(self, tmp_path: Path) -> None:
        assert _UPSTREAM_ROOT is not None
        wm_dir = (
            _UPSTREAM_ROOT / "plugins" / "vertical-plugins" / "wealth-management"
        )
        skill_dirs = sorted(
            d for d in (wm_dir / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        assert len(skill_dirs) == 6, (
            f"expected 6 wealth-management skills upstream, got "
            f"{len(skill_dirs)}: {[d.name for d in skill_dirs]}"
        )

        ids: set[str] = set()
        for skill_dir in skill_dirs:
            target = resolve_target("wealth-management", skill_dir.name)
            assert overlap_target("wealth-management", skill_dir.name) is None, (
                "wealth-management has no Nerya builtin overlaps yet"
            )
            result = apply_to_directory(
                upstream_skill_dir=skill_dir,
                target=target,
                workspace_root=tmp_path,
                dry_run=False,
            )
            assert result.target.skill_id == target.skill_id

            ids.add(target.skill_id)

        assert ids == {
            "client_report", "client_review", "financial_plan",
            "investment_proposal", "portfolio_rebalance",
            "tax_loss_harvesting",
        }


@_skip_no_upstream
class TestEquityResearchVerticalSnapshot:
    """Phase C: equity-research vertical (9 upstream skills, 1 overlap)."""

    def test_resolves_eight_new_and_skips_one_overlap(self, tmp_path: Path) -> None:
        assert _UPSTREAM_ROOT is not None
        eq_dir = (
            _UPSTREAM_ROOT / "plugins" / "vertical-plugins" / "equity-research"
        )
        skill_dirs = sorted(
            d for d in (eq_dir / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        assert len(skill_dirs) == 9

        imported: set[str] = set()
        skipped_overlaps: set[str] = set()
        for skill_dir in skill_dirs:
            upstream_skill = skill_dir.name
            if overlap_target("equity-research", upstream_skill) is not None:
                skipped_overlaps.add(upstream_skill)
                continue
            target = resolve_target("equity-research", upstream_skill)
            result = apply_to_directory(
                upstream_skill_dir=skill_dir,
                target=target,
                workspace_root=tmp_path,
                dry_run=False,
            )
            md = result.target.absolute_skill_md(tmp_path)
            manifest = SkillManifest.from_skill_md(md)
            assert manifest.id == target.skill_id
            imported.add(target.skill_id)

        # Exactly the one we registered in NERYA_BUILTIN_OVERLAPS.
        assert skipped_overlaps == {"initiating-coverage"}
        # Eight new (lifecycle / coverage) skills must all import cleanly.
        assert imported == {
            "catalyst_calendar", "earnings_analysis", "earnings_preview",
            "idea_generation", "model_update", "morning_note",
            "sector_overview", "thesis_tracker",
        }


@_skip_no_upstream
class TestBuiltinPromoter:
    def test_apply_to_builtin_directory_writes_compact_skill_and_full_reference(
        self, tmp_path: Path
    ) -> None:
        assert _PE_DIR is not None
        upstream_dir = _PE_DIR / "skills" / "ic-memo"
        target = resolve_target("private-equity", "ic-memo")

        result = apply_to_builtin_directory(
            upstream_skill_dir=upstream_dir,
            target=target,
            builtin_root=tmp_path,
            upstream_repo_root=_UPSTREAM_ROOT,
            apply=True,
        )

        md = result.target_path
        assert md.is_file()
        assert result.reference_path is not None
        assert result.reference_path.is_file()

        text = md.read_text(encoding="utf-8")
        assert _LEGACY_RULES_HEADER not in text
        assert len(text.splitlines()) <= 80
        assert "`references/full-playbook.md`" in text

        manifest = SkillManifest.from_skill_md(md)
        assert manifest.id == "finance.private_equity.ic_memo"
        assert manifest.source == "builtin"

        full_text = result.reference_path.read_text(encoding="utf-8")
        assert "# Full Playbook" in full_text
        assert "# Investment Committee Memo" in full_text
        assert _LEGACY_RULES_HEADER not in full_text

    def test_promote_finance_builtins_merges_overlaps_and_blocked_skill(
        self, tmp_path: Path
    ) -> None:
        assert _UPSTREAM_ROOT is not None
        for skill_id, manifest_name in [
            ("dcf_valuation_skill", "dcf_valuation"),
            ("equity_research_skill", "equity_research"),
            ("evolve", "evolve"),
        ]:
            _write_minimal_builtin(tmp_path / skill_id / "SKILL.md", name=manifest_name)

        summary = promote_finance_builtins(
            upstream_root=_UPSTREAM_ROOT,
            builtin_root=tmp_path,
            apply=True,
        )

        assert summary["totals"]["errors"] == 0
        assert summary["totals"]["imports"] == 52
        assert summary["totals"]["merged_overlaps"] == 2
        assert summary["totals"]["merged_blocked"] == 1

        sample_md = (
            tmp_path / "finance" / "private_equity" / "ic_memo" / "SKILL.md"
        )
        assert sample_md.is_file()
        assert SkillManifest.from_skill_md(sample_md).id == "finance.private_equity.ic_memo"

        assert (
            tmp_path / "dcf_valuation_skill" / "references"
            / "financial-services-financial_analysis-dcf_model.md"
        ).is_file()
        assert (
            tmp_path / "equity_research_skill" / "references"
            / "financial-services-equity_research-initiating_coverage.md"
        ).is_file()
        assert (
            tmp_path / "evolve" / "references"
            / "financial-services-financial_analysis-skill_creator.md"
        ).is_file()


# ---------------------------------------------------------------------------
# command_to_route
# ---------------------------------------------------------------------------


class TestCommandToRoute:
    def test_parse_command_md(self) -> None:
        text = (
            "---\n"
            "description: Draft an investment committee memo\n"
            'argument-hint: "[company name]"\n'
            "---\n\n"
            "Load the `ic-memo` skill ...\n"
        )
        description, argument_hint, body = parse_command_md(text)
        assert description == "Draft an investment committee memo"
        assert argument_hint == "[company name]"
        assert "Load the `ic-memo`" in body

    @_skip_no_upstream
    def test_derive_route_from_pe_ic_memo(self) -> None:
        assert _PE_DIR is not None
        cmd_md = _PE_DIR / "commands" / "ic-memo.md"
        target = resolve_target("private-equity", "ic-memo")
        route = derive_route(upstream_command_md=cmd_md, skill_target=target)
        assert route.namespace_id == "finance.private_equity.ic_memo"
        assert route.route_id == "finance_cmd_private_equity_ic_memo"
        assert "investment committee" in route.description.lower()

    @_skip_no_upstream
    def test_render_routes_yml_emits_user_command_match(self) -> None:
        assert _PE_DIR is not None
        cmd_md = _PE_DIR / "commands" / "ic-memo.md"
        target = resolve_target("private-equity", "ic-memo")
        route = derive_route(upstream_command_md=cmd_md, skill_target=target)
        rendered = render_routes_yml([route])
        assert "version: 1" in rendered
        assert "kind: user.command" in rendered
        assert "payload.command: finance.private_equity.ic_memo" in rendered
        assert "target: main" in rendered


# ---------------------------------------------------------------------------
# agent_to_subagent
# ---------------------------------------------------------------------------


@_skip_no_upstream
class TestAgentToSubagent:
    def test_market_researcher_round_trip(self, tmp_path: Path) -> None:
        upstream_md = (
            _UPSTREAM_ROOT / "plugins" / "agent-plugins" / "market-researcher"
            / "agents" / "market-researcher.md"
        )
        assert upstream_md.is_file()

        sub = derive_subagent(upstream_md, vertical_namespace="financial_analysis")
        assert sub.nerya_name == "market_researcher"
        assert sub.tier in {"medium", "high"}

        agent_md, role_yaml = write_subagent(
            sub=sub, workspace_root=tmp_path, dry_run=False
        )
        assert agent_md.is_file()
        assert role_yaml.is_file()

        # Subagent .agent.md MUST NOT have YAML frontmatter — Nerya's
        # SubAgentRegistry treats the entire file as the prompt body and
        # would render a stray ``---`` block as the lane's first user-
        # facing markdown otherwise.
        prompt_text = agent_md.read_text(encoding="utf-8")
        assert prompt_text.lstrip().startswith("# "), (
            "agent.md must start with an H1 title (no YAML frontmatter)"
        )

        # role.yaml must carry the structured fields the subagent
        # registry reads back via _load_role_meta.
        role_text = role_yaml.read_text(encoding="utf-8")
        assert "name: market_researcher" in role_text
        assert "tier:" in role_text
        assert "allowed_skills:" in role_text
        # Finance skills referenced inline by the upstream agent should
        # be namespaced when ``vertical_namespace`` is provided.
        assert "finance.financial_analysis." in role_text


# ---------------------------------------------------------------------------
# cli — promote (Phase D one-shot)
# ---------------------------------------------------------------------------


@_skip_no_upstream
class TestPromoteCli:
    """End-to-end check that ``promote --apply`` lands the full
    PE+ER+WM+IB+FA+fund-admin+ops bundle into a fresh workspace.

    The intent is *not* to re-run every per-vertical assertion (those are
    already covered by ``TestApplyToDirectory`` and the smoke harness)
    but to lock in:

    1. ``promote`` reaches every known vertical that exists in the
       upstream checkout (no silent regression if a new vertical is
       added or deleted upstream).
    2. The exit code is 0 with no errors when run on a clean workspace.
    3. The diff-overlap report is rendered into the workspace.
    4. Each per-vertical staging routes file lives under ``triggers/``.
    """

    def test_promote_apply_full_bundle(self, tmp_path: Path) -> None:
        """Run promote --apply against a temp workspace and confirm
        every vertical lands skills + routes (or is recorded as 'skipped'
        because the upstream checkout no longer ships it)."""

        from scripts.finance_skills_importer.cli import cmd_promote
        import argparse as _argparse

        nerya_root = Path(__file__).resolve().parents[1]
        builtin_root = nerya_root / "nerya" / "skills" / "builtin"

        ns = _argparse.Namespace(
            upstream_root=_UPSTREAM_ROOT,
            workspace=tmp_path,
            apply=True,
            include_overlaps=False,
            license="Apache-2.0",
            author="Anthropic",
            nerya_builtin_root=builtin_root,
        )
        rc = cmd_promote(ns)
        assert rc == 0

        skills_root = tmp_path / "skills" / "finance"
        assert skills_root.is_dir()

        # The seven verticals are the source of truth; any vertical
        # missing from upstream gets skipped, but all *present* ones
        # MUST produce at least one skill.
        from scripts.finance_skills_importer.name_map import KNOWN_VERTICALS

        for vertical in KNOWN_VERTICALS:
            upstream_dir = (
                _UPSTREAM_ROOT / "plugins" / "vertical-plugins" / vertical
                / "skills"
            )
            if not upstream_dir.is_dir():
                continue
            ws_dir = skills_root / vertical.replace("-", "_")
            assert ws_dir.is_dir(), f"missing workspace dir for {vertical}"
            md_files = list(ws_dir.rglob("SKILL.md"))
            assert md_files, f"no SKILL.md landed for {vertical}"

        # Diff-overlap is always rendered when at least one overlap pair
        # exists. The fixed two are dcf-model + initiating-coverage.
        diff_md = tmp_path / "diff_overlap.md"
        assert diff_md.is_file()
        diff_text = diff_md.read_text(encoding="utf-8")
        assert "dcf-model" in diff_text
        assert "initiating-coverage" in diff_text

        # Per-vertical staging routes — verticals without commands/
        # (fund-admin, operations) are silently absent; verticals with
        # commands/ MUST have a corresponding routes.yml file.
        triggers_dir = tmp_path / "triggers"
        for vertical in [
            "private-equity", "equity-research", "wealth-management",
            "investment-banking", "financial-analysis",
        ]:
            cmds_dir = (
                _UPSTREAM_ROOT / "plugins" / "vertical-plugins" / vertical
                / "commands"
            )
            if not cmds_dir.is_dir():
                continue
            ws_routes = (
                triggers_dir
                / f"finance.{vertical.replace('-', '_')}.routes.yml"
            )
            assert ws_routes.is_file(), (
                f"missing routes.yml for {vertical}"
            )
            text = ws_routes.read_text(encoding="utf-8")
            assert "kind: user.command" in text
            assert (
                f"payload.command: finance.{vertical.replace('-', '_')}."
                in text
            )

        # Subagent bundle — at least the canonical 10 land in
        # workspace/subagents/. Each one has a paired .role.yaml.
        subagents_dir = tmp_path / "subagents"
        assert subagents_dir.is_dir()
        agent_files = sorted(subagents_dir.glob("*.agent.md"))
        role_files = sorted(subagents_dir.glob("*.role.yaml"))
        assert len(agent_files) >= 10, (
            f"expected ≥10 subagents, got {len(agent_files)}"
        )
        assert len(agent_files) == len(role_files), (
            "every .agent.md must have a sibling .role.yaml"
        )

        # Spot-check one agent prompt body has no YAML frontmatter.
        sample = agent_files[0].read_text(encoding="utf-8").lstrip()
        assert sample.startswith("# "), (
            ".agent.md must be pure markdown (H1 first line)"
        )

    def test_promote_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        """A dry-run promote MUST NOT touch the workspace at all."""

        from scripts.finance_skills_importer.cli import cmd_promote
        import argparse as _argparse

        nerya_root = Path(__file__).resolve().parents[1]
        builtin_root = nerya_root / "nerya" / "skills" / "builtin"

        ns = _argparse.Namespace(
            upstream_root=_UPSTREAM_ROOT,
            workspace=tmp_path,
            apply=False,
            include_overlaps=False,
            license="Apache-2.0",
            author="Anthropic",
            nerya_builtin_root=builtin_root,
        )
        rc = cmd_promote(ns)
        assert rc == 0
        # Workspace should remain empty (or contain only the empty
        # directory pytest created).
        children = [p for p in tmp_path.iterdir()]
        assert children == [], (
            f"dry-run leaked files into workspace: {children}"
        )


def _write_minimal_builtin(path: Path, *, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "references").mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                FRONTMATTER_START_MARKER,
                "---",
                f"name: {name}",
                'description: "test builtin"',
                "version: 0.0.1",
                "license: MIT",
                "author: tests",
                "---",
                FRONTMATTER_END_MARKER,
                "",
                f"# {name}",
                "",
                "## Lazy References",
                "",
                "- `references/full-playbook.md` for baseline details.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (path.parent / "references" / "full-playbook.md").write_text(
        "# Full Playbook\n", encoding="utf-8"
    )
