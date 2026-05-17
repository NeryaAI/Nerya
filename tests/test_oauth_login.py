"""Tests for ``nerya.llm.oauth_login`` — login directive + device-code helpers.

The CLI-import paths and paste-token flows are exercised indirectly by
the dashboard end-to-end suites; here we focus on the new
``login_directive`` / ``device_code_*`` surface added for the
"Generate login link" dashboard control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


pytestmark = pytest.mark.smoke


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def workspace(tmp_path: Path):
    """Minimal Config that is sufficient for ProviderAuthStore.open."""

    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "security").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({}), encoding="utf-8",
    )
    from nerya.core.config import load_config
    return load_config(workspace=tmp_path)


# --------------------------------------------------------------------- #
# login_directive
# --------------------------------------------------------------------- #


class TestLoginDirective:
    def test_codex_returns_cli_command(self):
        from nerya.llm import oauth_login as oa

        d = oa.login_directive("openai-codex")
        assert d["provider"] == "openai-codex"
        assert d["flow"] == "cli"
        assert d["command"] == "codex login"
        assert "codex login" in d["instruction"]

    def test_claude_code_returns_cli_command(self):
        from nerya.llm import oauth_login as oa

        d = oa.login_directive("claude-code")
        assert d["flow"] == "cli"
        assert d["command"].startswith("claude")

    def test_gemini_cli_returns_cli_command(self):
        from nerya.llm import oauth_login as oa

        d = oa.login_directive("google-gemini-cli")
        assert d["flow"] == "cli"
        assert "gemini" in (d["command"] or "")

    def test_copilot_returns_device_code(self):
        from nerya.llm import oauth_login as oa

        d = oa.login_directive("copilot")
        assert d["flow"] == "device_code"
        assert d["verification_uri"].startswith("https://github.com/login/device")
        assert "device-code" in d["instruction"]

    def test_unknown_provider_rejected(self):
        from nerya.llm import oauth_login as oa

        with pytest.raises(oa.OAuthLoginError):
            oa.login_directive("nope")

    def test_case_insensitive(self):
        from nerya.llm import oauth_login as oa

        d = oa.login_directive("OPENAI-CODEX")
        assert d["provider"] == "openai-codex"


# --------------------------------------------------------------------- #
# Device-code provider config
# --------------------------------------------------------------------- #


class TestDeviceCodeConfig:
    def test_copilot_config_present(self):
        from nerya.llm import oauth_login as oa

        cfg = oa.DEVICE_CODE_PROVIDERS["copilot"]
        assert cfg["client_id"]
        assert cfg["device_code_url"].startswith("https://github.com/")
        assert cfg["token_url"].startswith("https://github.com/")
        assert cfg["scope"]

    def test_unknown_device_code_provider_rejected(self):
        from nerya.llm import oauth_login as oa

        with pytest.raises(oa.OAuthLoginError):
            oa.device_code_start("openai-codex")  # CLI-only, not device-code


# --------------------------------------------------------------------- #
# device_code_start (HTTP mocked)
# --------------------------------------------------------------------- #


class TestDeviceCodeStart:
    def test_returns_user_code_and_device_code(self, monkeypatch):
        from nerya.llm import oauth_login as oa

        captured: dict[str, Any] = {}

        def fake_post(url, *, data, accept):
            captured["url"] = url
            captured["data"] = data
            captured["accept"] = accept
            return {
                "device_code": "DEV-123",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "verification_uri_complete": "https://github.com/login/device?user_code=ABCD-EFGH",
                "interval": 5,
                "expires_in": 900,
            }

        monkeypatch.setattr(oa, "_http_post", fake_post)

        out = oa.device_code_start("copilot")
        assert out["device_code"] == "DEV-123"
        assert out["user_code"] == "ABCD-EFGH"
        assert out["verification_uri"] == "https://github.com/login/device"
        assert out["verification_uri_complete"].endswith("ABCD-EFGH")
        assert out["interval"] == 5
        assert out["expires_in"] == 900
        assert out["expires_at"] > 0
        # Verify we hit the right endpoint with the documented payload.
        assert captured["url"] == oa.DEVICE_CODE_PROVIDERS["copilot"]["device_code_url"]
        assert captured["data"]["client_id"] == oa.DEVICE_CODE_PROVIDERS["copilot"]["client_id"]
        assert captured["data"]["scope"] == oa.DEVICE_CODE_PROVIDERS["copilot"]["scope"]
        assert captured["accept"] == "application/json"

    def test_missing_device_code_field_raises(self, monkeypatch):
        from nerya.llm import oauth_login as oa

        monkeypatch.setattr(
            oa, "_http_post",
            lambda *args, **kwargs: {"error": "invalid_client"},
        )
        with pytest.raises(oa.OAuthLoginError):
            oa.device_code_start("copilot")


# --------------------------------------------------------------------- #
# device_code_poll (HTTP mocked)
# --------------------------------------------------------------------- #


class TestDeviceCodePoll:
    def test_pending_keeps_polling(self, workspace, monkeypatch):
        from nerya.llm import oauth_login as oa

        monkeypatch.setattr(
            oa, "_http_post",
            lambda *args, **kwargs: {"error": "authorization_pending"},
        )
        out = oa.device_code_poll(
            workspace, provider="copilot", device_code="DEV-1",
        )
        assert out["status"] == "pending"

    def test_slow_down_returns_new_interval(self, workspace, monkeypatch):
        from nerya.llm import oauth_login as oa

        monkeypatch.setattr(
            oa, "_http_post",
            lambda *args, **kwargs: {"error": "slow_down", "interval": 12},
        )
        out = oa.device_code_poll(
            workspace, provider="copilot", device_code="DEV-1",
        )
        assert out["status"] == "slow_down"
        assert out["interval"] == 12

    def test_terminal_errors_surface(self, workspace, monkeypatch):
        from nerya.llm import oauth_login as oa

        for err in ("expired_token", "access_denied", "unsupported_grant_type"):
            monkeypatch.setattr(
                oa, "_http_post",
                lambda *args, e=err, **kwargs: {"error": e},
            )
            out = oa.device_code_poll(
                workspace, provider="copilot", device_code="DEV-1",
            )
            assert out["status"] == "error"
            assert out["error"] == err

    def test_success_persists_token(self, workspace, monkeypatch):
        from nerya.llm import oauth_login as oa

        monkeypatch.setattr(
            oa, "_http_post",
            lambda *args, **kwargs: {
                "access_token": "ghu_abc123",
                "scope": "read:user copilot",
                "token_type": "bearer",
            },
        )
        out = oa.device_code_poll(
            workspace,
            provider="copilot",
            device_code="DEV-1",
            actor_id="default",
        )
        assert out["status"] == "ok"
        assert out["provider"] == "copilot"
        assert out["actor_id"] == "default"
        assert out["record"]
        # The record's public view does NOT include the raw token.
        assert "ghu_abc123" not in str(out["record"])
        # And resolving the token via the public helper works.
        token = oa.resolve_oauth_token(
            workspace, provider="copilot", actor_id="default",
        )
        assert token == "ghu_abc123"

    def test_unknown_provider_returns_error_envelope(self, workspace):
        from nerya.llm import oauth_login as oa

        out = oa.device_code_poll(
            workspace, provider="not-a-provider", device_code="DEV-1",
        )
        assert out["status"] == "error"
        assert "unsupported" in out["error"]


# --------------------------------------------------------------------- #
# Routes wiring
# --------------------------------------------------------------------- #


class TestOauthRoutesWired:
    def test_login_directive_route_present(self):
        from nerya.api.routes_oauth import routes

        urls = [r[1] for r in routes()]
        assert "/llm/oauth/login_directive" in urls
        assert "/llm/oauth/device_code/start" in urls
        assert "/llm/oauth/device_code/poll" in urls
