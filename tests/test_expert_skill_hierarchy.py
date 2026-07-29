from __future__ import annotations

import pytest

from nerya.skills.registry import SkillRegistry, _enabled_ok, _walk_skill_dirs
from nerya.subagents.registry import (
    DEFAULT_SUBAGENT_PROMPTS,
    DEFAULT_SUBAGENT_SKILLS,
    DEFAULT_TIERS,
    canonical_subagent_name,
)


pytestmark = pytest.mark.smoke

_EXPERT_IDS = {
    "expert_investors.buffett",
    "expert_investors.damodaran",
    "expert_investors.marks",
    "expert_investors.mauboussin",
    "expert_investors.druckenmiller",
}

_CREATOR_IDS = {
    "finance-creators.serenity",
    "finance-creators.unusual_whales",
    "finance-creators.kobeissi",
}

_LENS_ROLE_SKILLS = {
    "buffett_lens": "expert_investors.buffett",
    "damodaran_lens": "expert_investors.damodaran",
    "marks_lens": "expert_investors.marks",
    "mauboussin_lens": "expert_investors.mauboussin",
    "druckenmiller_lens": "expert_investors.druckenmiller",
    "serenity_lens": "finance-creators.serenity",
    "unusual_whales_lens": "finance-creators.unusual_whales",
    "kobeissi_lens": "finance-creators.kobeissi",
}


def test_walker_yields_hub_and_nested_sub_skills(tmp_path) -> None:
    hub = tmp_path / "expert_hub"
    hub.mkdir()
    (hub / "SKILL.md").write_text("---\nname: hub\ndescription: d\n---\n")
    sub = hub / "alpha"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\nname: hub.alpha\ndescription: d\n---\n")
    # SKILL.md inside an asset dir must stay invisible.
    refs = hub / "references"
    refs.mkdir()
    (refs / "SKILL.md").write_text("---\nname: bogus\ndescription: d\n---\n")

    found = {md for _d, md in _walk_skill_dirs(tmp_path)}
    assert hub / "SKILL.md" in found
    assert sub / "SKILL.md" in found
    assert refs / "SKILL.md" not in found


def test_enabled_allowlist_covers_namespaced_children() -> None:
    enabled = {"expert_investors", "finance.operations.kyc_rules"}
    assert _enabled_ok("expert_investors", enabled)
    assert _enabled_ok("expert_investors.buffett", enabled)
    assert _enabled_ok("finance.operations.kyc_rules", enabled)
    # No hub entry -> sibling namespaces stay gated.
    assert not _enabled_ok("finance.operations.kyc_doc_parse", enabled)
    # Prefix must be a namespace boundary, not a substring.
    assert not _enabled_ok("expert_investors2", enabled)
    assert _enabled_ok("anything", None)


def test_builtin_registry_ships_expert_sub_skills() -> None:
    ids = {e.manifest.id for e in SkillRegistry.load_builtin().list()}
    assert "expert_investors" in ids
    assert _EXPERT_IDS <= ids
    assert "finance-creators" in ids
    assert _CREATOR_IDS <= ids


def test_expert_hub_stays_light_and_routes_to_sub_skills() -> None:
    from pathlib import Path

    import nerya.skills as skills_pkg

    hub_md = Path(skills_pkg.__file__).parent / "builtin" / "expert_investors" / "SKILL.md"
    text = hub_md.read_text(encoding="utf-8")
    for expert_id in _EXPERT_IDS:
        assert expert_id in text
    # The hub is a router — the full lens bodies must not live in it.
    assert "Owner earnings" not in text
    assert "Story-to-number bridge" not in text


def test_expert_lens_default_roles_are_wired() -> None:
    for role, skill_id in _LENS_ROLE_SKILLS.items():
        assert role in DEFAULT_SUBAGENT_SKILLS
        assert skill_id in DEFAULT_SUBAGENT_SKILLS[role]
        assert role in DEFAULT_TIERS
        prompt = DEFAULT_SUBAGENT_PROMPTS[role]
        assert f'skill_view("{skill_id}")' in prompt
        assert "Never submit orders" in prompt


def test_canonical_name_routes_expert_synonyms() -> None:
    # Exact lens names and bare expert names resolve via the data-driven
    # DEFAULT_SUBAGENT_PROFILES table — no token scanning involved.
    assert canonical_subagent_name("buffett_lens") == "buffett_lens"
    assert canonical_subagent_name("buffett") == "buffett_lens"
    assert canonical_subagent_name("Howard Marks") == "marks_lens"
    assert canonical_subagent_name("druckenmiller") == "druckenmiller_lens"
    assert canonical_subagent_name("serenity") == "serenity_lens"
    assert canonical_subagent_name("unusual_whales") == "unusual_whales_lens"
    assert canonical_subagent_name("kobeissi") == "kobeissi_lens"
    # Generic investor language still routes to the fundamentals profile.
    assert canonical_subagent_name("investor_perspective") == "fundamentals_analyst"
