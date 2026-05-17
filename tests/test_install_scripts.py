"""File-shape coverage for the installer / uninstaller scripts.

These tests don't *execute* the shell scripts — they just lock in the
operator-visible contracts so a refactor can't silently delete a flag,
a hint, or the summary block. The actual end-to-end install is
exercised by humans + the CI matrix.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.smoke


ROOT = Path(__file__).resolve().parents[1]
INSTALL_DIR = ROOT / "install"


# ---------------------------------------------------------------------------
# uninstall.sh (Linux / macOS)
# ---------------------------------------------------------------------------


def test_uninstall_sh_exists_and_is_a_shell_script():
    path = INSTALL_DIR / "uninstall.sh"
    assert path.exists(), f"missing {path}"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("#!/usr/bin/env bash"), "uninstall.sh must be a bash script"
    # `set -euo pipefail` is mandatory: silent failures here are
    # particularly bad because the user is expecting deletion.
    assert "set -euo pipefail" in body


def test_uninstall_sh_supports_purge_and_keep_shim_flags():
    body = (INSTALL_DIR / "uninstall.sh").read_text(encoding="utf-8")
    for needle in ("--purge", "--keep-shim", "--yes", "NERYA_NO_PROMPT"):
        assert needle in body, f"uninstall.sh must accept {needle}"


def test_uninstall_sh_removes_service_shim_and_source():
    body = (INSTALL_DIR / "uninstall.sh").read_text(encoding="utf-8")
    # Service removal entry points (one per platform).
    assert "systemctl --user disable --now nerya.service" in body
    assert "launchctl unload" in body
    # CLI shim removal.
    assert ".local/bin/nerya" in body
    # Source tree removal.
    assert "$NERYA_HOME/src" in body
    # Optional purge path also removes the workspace.
    assert "$NERYA_WORKSPACE" in body


def test_uninstall_sh_refuses_non_interactive_without_yes():
    """Safety: piping `curl … | sh` must NOT silently nuke a workspace."""
    body = (INSTALL_DIR / "uninstall.sh").read_text(encoding="utf-8")
    assert (
        'non-interactive run' in body
        and '--yes' in body
    ), "uninstall.sh must refuse to run non-interactively without --yes"


# ---------------------------------------------------------------------------
# uninstall.ps1 (Windows)
# ---------------------------------------------------------------------------


def test_uninstall_ps1_exists_and_declares_param_block():
    path = INSTALL_DIR / "uninstall.ps1"
    assert path.exists(), f"missing {path}"
    body = path.read_text(encoding="utf-8")
    # PowerShell convention: top-level param() declaration for the
    # script-level switches.
    assert "[switch]$Purge" in body
    assert "[switch]$KeepShim" in body
    assert "[switch]$Yes" in body
    assert "$ErrorActionPreference = \"Stop\"" in body


def test_uninstall_ps1_removes_nssm_service_and_shim():
    body = (INSTALL_DIR / "uninstall.ps1").read_text(encoding="utf-8")
    assert "nssm stop    $Service" in body
    assert "nssm remove  $Service confirm" in body
    assert "nerya.cmd" in body
    # The workspace must be preserved by default; -Purge opts in.
    assert "(KEPT -- pass -Purge to also remove)" in body


def test_uninstall_ps1_refuses_non_interactive_without_yes():
    body = (INSTALL_DIR / "uninstall.ps1").read_text(encoding="utf-8")
    assert "non-interactive run" in body and "-Yes" in body


# ---------------------------------------------------------------------------
# install.sh — Phase 10b/c/d contracts
# ---------------------------------------------------------------------------


def test_install_sh_honors_nerya_src_env_var():
    """`NERYA_SRC=<local path>` must skip the git clone and point the
    shim at the supplied checkout. Critical for offline / dev installs."""
    body = (INSTALL_DIR / "install.sh").read_text(encoding="utf-8")
    assert 'NERYA_SRC="${NERYA_SRC:-}"' in body
    # Validates pyproject presence so we fail fast instead of trying to
    # uv-sync a garbage directory.
    assert "pyproject.toml" in body
    assert "NERYA_RESOLVED_SRC=" in body


def test_install_sh_emits_service_restart_hints():
    body = (INSTALL_DIR / "install.sh").read_text(encoding="utf-8")
    # Linux
    assert "systemctl --user restart nerya" in body
    assert "journalctl --user -u nerya -f" in body
    # macOS
    assert "launchctl kickstart" in body


def test_install_sh_runs_post_install_smoke():
    body = (INSTALL_DIR / "install.sh").read_text(encoding="utf-8")
    assert "post_install_smoke" in body
    assert "--version" in body
    # The smoke output must surface ok / warn flavoured lines so the
    # operator can see it scrolled past during the install.
    assert 'ok "smoke:' in body


def test_install_sh_includes_uninstall_pointer():
    body = (INSTALL_DIR / "install.sh").read_text(encoding="utf-8")
    assert "uninstall.sh" in body
    assert "--purge" in body


# ---------------------------------------------------------------------------
# install.ps1 — same contracts on Windows
# ---------------------------------------------------------------------------


def test_install_ps1_honors_nerya_src_env_var():
    body = (INSTALL_DIR / "install.ps1").read_text(encoding="utf-8")
    assert "$NeryaSrc" in body
    assert "pyproject.toml" in body


def test_install_ps1_emits_service_restart_hints():
    body = (INSTALL_DIR / "install.ps1").read_text(encoding="utf-8")
    assert "sc query nerya-agent" in body
    assert "nssm restart nerya-agent" in body


def test_install_ps1_runs_post_install_smoke():
    body = (INSTALL_DIR / "install.ps1").read_text(encoding="utf-8")
    assert "Post-Install-Smoke" in body
    # The PowerShell script must guard the auto-setup behind the smoke
    # result so a broken shim doesn't keep prompting.
    assert "if ($Script:SmokeOk)" in body


def test_install_ps1_includes_uninstall_pointer():
    body = (INSTALL_DIR / "install.ps1").read_text(encoding="utf-8")
    assert "uninstall.ps1" in body
    assert "-Purge" in body


# ---------------------------------------------------------------------------
# Syntax validation (best-effort, skipped when the shell isn't available)
# ---------------------------------------------------------------------------


def test_install_sh_passes_bash_n(monkeypatch):
    """Run `bash -n install.sh` if bash is on PATH; skip otherwise."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this host")
    for name in ("install.sh", "uninstall.sh"):
        path = INSTALL_DIR / name
        res = subprocess.run(
            [bash, "-n", str(path)],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, f"{name} failed bash -n: {res.stderr}"


def test_install_ps1_parses_as_powershell():
    """Run a PowerShell tokenisation pass on install.ps1 + uninstall.ps1
    when pwsh / powershell is available. Skip on Linux CI without PS."""
    import shutil
    import subprocess

    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        pytest.skip("no powershell available on this host")
    for name in ("install.ps1", "uninstall.ps1"):
        path = INSTALL_DIR / name
        # `[scriptblock]::Create` parses without executing. Errors
        # propagate via a non-zero exit code.
        check = (
            f"$ErrorActionPreference='Stop'; "
            f"$src = Get-Content -Raw -Path '{path}'; "
            f"$null = [scriptblock]::Create($src); "
            "Write-Host OK"
        )
        res = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", check],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, f"{name} failed parse: {res.stderr or res.stdout}"
