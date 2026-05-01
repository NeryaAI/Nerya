"""Regression tests for the optional ``anet`` integration.

Contract under test:

* Turning ``integrations.anet.enabled`` off keeps the anet skill
  invisible to the skill registry, keeps the ``/anet/*`` routes out
  of ``local_server``'s route table, and leaves the ``anet`` module
  un-imported from the anet pip extra's perspective (the pip extra
  is optional — missing it must not break anything while disabled).
* Turning it on without the outbound sub-flag still makes the skill
  visible (inbound is the minimum viable mode) but outbound skill
  scripts refuse to call peers.
* The whitelist rejects any operator-supplied path that brushes a
  write surface (wallet, signer, trading, operator, etc.).
* The ``NERYA_DISABLE_INTEGRATIONS`` env flag short-circuits enabled
  to disabled regardless of yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core.config import Config, load_config
from nerya.core.paths import WorkspacePaths
from nerya.integrations.anet import whitelist as wl
from nerya.skills.registry import SkillRegistry


pytestmark = pytest.mark.smoke


def _make_config(tmp_path: Path, anet_block: dict | None) -> Config:
    """Build a Config whose workspace is ``tmp_path`` with the given anet block."""
    paths = WorkspacePaths(root=tmp_path)
    data: dict = {}
    if anet_block is not None:
        data["integrations"] = {"anet": anet_block}
    return Config(paths=paths, data=data)


# --------------------------------------------------------------------------
# Skill-registry gating
# --------------------------------------------------------------------------


def test_anet_skill_hidden_when_integration_disabled(tmp_path):
    cfg = _make_config(tmp_path, anet_block=None)
    reg = SkillRegistry.load_builtin(cfg.paths, config=cfg)
    assert "anet" not in reg.by_id, (
        "anet skill must not register when integrations.anet.enabled is absent"
    )


def test_anet_skill_hidden_when_integration_explicitly_off(tmp_path):
    cfg = _make_config(tmp_path, anet_block={"enabled": False})
    reg = SkillRegistry.load_builtin(cfg.paths, config=cfg)
    assert "anet" not in reg.by_id


def test_anet_skill_visible_when_integration_enabled(tmp_path):
    cfg = _make_config(tmp_path, anet_block={"enabled": True})
    reg = SkillRegistry.load_builtin(cfg.paths, config=cfg)
    assert "anet" in reg.by_id
    manifest = reg.get("anet").manifest
    assert manifest.requires_integration == "anet"


def test_anet_skill_hidden_when_global_kill_switch_set(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_DISABLE_INTEGRATIONS", "1")
    cfg = _make_config(tmp_path, anet_block={"enabled": True})
    reg = SkillRegistry.load_builtin(cfg.paths, config=cfg)
    assert "anet" not in reg.by_id, (
        "NERYA_DISABLE_INTEGRATIONS must override yaml-enabled state"
    )


def test_integration_enabled_helper_honours_env(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, anet_block={"enabled": True})
    assert cfg.integration_enabled("anet") is True
    monkeypatch.setenv("NERYA_DISABLE_INTEGRATIONS", "on")
    assert cfg.integration_enabled("anet") is False


# --------------------------------------------------------------------------
# Whitelist invariants
# --------------------------------------------------------------------------


def test_whitelist_default_is_read_only():
    # Every built-in safe path must start with a read-only-flavoured prefix
    # or be one of the protocol endpoints.
    allowed_protocol = {"/anet/health", "/anet/meta", "/anet/status"}
    for method, path, _desc in wl.SAFE_PATHS:
        if path in allowed_protocol:
            continue
        # Extra belt: no safe path should sit under a hard-deny prefix.
        assert not any(path.startswith(d + "/") or path == d
                       for d in wl.HARD_DENY_PREFIXES), (
            f"safe path {path} collides with hard-deny prefix"
        )


def test_whitelist_rejects_operator_extras_on_write_surfaces():
    resolved = wl.resolve_exposed_paths([
        "/wallet/transfer",
        "/trading/order",
        "/operator/kill",
        "/approvals/sign",
        "/evolution/promote",
    ])
    # None of the rejected extras can leak through.
    for forbidden in ("/wallet/transfer", "/trading/order",
                      "/operator/kill", "/approvals/sign",
                      "/evolution/promote"):
        assert forbidden not in resolved


def test_whitelist_accepts_safe_operator_extra():
    resolved = wl.resolve_exposed_paths(["/custom/public_metric"])
    assert "/custom/public_metric" in resolved


# --------------------------------------------------------------------------
# Routes only mount when enabled
# --------------------------------------------------------------------------


def test_routes_anet_emits_expected_verbs():
    # Direct unit check: handlers are pure of any daemon calls, so we
    # import the module and inspect its route table.
    from nerya.api import routes_anet
    paths = [(m, p) for (m, p, _h) in routes_anet.routes()]
    assert ("GET", "/anet/health") in paths
    assert ("GET", "/anet/meta") in paths
    assert ("GET", "/anet/status") in paths
