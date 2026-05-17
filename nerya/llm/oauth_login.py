"""Codex / Claude Code OAuth login helpers.

These helpers let operators sign into ChatGPT (Codex) or Claude
Code-compatible accounts and reuse that subscription for inference
through the dashboard.

The full OAuth dance (browser callback, PKCE, refresh) is owned by the
upstream CLIs (`codex login` and `claude /login`). Rather than re-derive
those flows we provide three import paths:

1. **Import from CLI** — read the JSON file the CLI wrote (``~/.codex/auth.json``
   or ``~/.claude/.credentials.json``) and copy the access/refresh tokens
   into Nerya's :class:`ProviderAuthStore`. This covers the common case
   where the operator already runs Codex CLI / Claude Code on the same
   workstation.
2. **Paste token** — operator pastes a token they obtained out-of-band
   (e.g. ``CLAUDE_CODE_OAUTH_TOKEN`` from a CI secret).
3. **Env passthrough** — when no record is registered, the model router
   transparently falls back to the env vars listed in the catalogue
   (e.g. ``CLAUDE_CODE_OAUTH_TOKEN``, ``ANTHROPIC_TOKEN``). This is the
   default for headless deployments.

For each approach we write a record to ``workspace/security/provider_auth.json``
under the appropriate provider id (``openai-codex`` / ``claude-code``)
with the secret material stashed in the SecretVault. The router then
prefers that record over env-var fallbacks when present.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.errors import NeryaError
from ..security.provider_auth import (
    ProviderAuthError,
    ProviderAuthRecord,
    ProviderAuthStore,
)
from ..security.secrets import SecretVault


__all__ = [
    "OAUTH_PROVIDERS",
    "OAuthProviderSpec",
    "OAuthLoginError",
    "list_oauth_providers",
    "import_codex_credentials",
    "import_claude_code_credentials",
    "import_gemini_cli_credentials",
    "import_copilot_credentials",
    "import_provider_credentials",
    "set_paste_token",
    "revoke",
    "status_for",
    "all_status",
    "resolve_oauth_token",
    "login_directive",
    "device_code_start",
    "device_code_poll",
    "DEVICE_CODE_PROVIDERS",
]


# --------------------------------------------------------------------- #
# Login directives (per-provider how-to)
# --------------------------------------------------------------------- #

# For each OAuth provider we tell the dashboard the *recommended way*
# to obtain a token. ``flow`` is ``"cli"`` (operator runs a CLI
# command, then clicks Import), ``"device_code"`` (we drive a standard
# device-code flow ourselves), or ``"paste"`` (operator pastes a
# token from anywhere). This is intentionally declarative so the
# dashboard can render the right UI without hard-coding strings.
_LOGIN_DIRECTIVES: dict[str, dict[str, Any]] = {
    "openai-codex": {
        "flow": "cli",
        "command": "codex login",
        "instruction": (
            "Run `codex login` in your shell. The Codex CLI opens a browser "
            "for the ChatGPT login + PKCE handshake and writes "
            "`~/.codex/auth.json`. Once the browser confirms, come back here "
            "and click 'Import from CLI'."
        ),
    },
    "claude-code": {
        "flow": "cli",
        "command": "claude /login",
        "instruction": (
            "Run `claude /login` in your shell (or `claude setup-token` for "
            "headless boxes). Claude Code writes "
            "`~/.claude/.credentials.json`. Then click 'Import from CLI'. "
            "If you only have a token string (e.g. from `claude setup-token`), "
            "paste it in the field below."
        ),
    },
    "google-gemini-cli": {
        "flow": "cli",
        "command": "gemini auth login",
        "instruction": (
            "Run `gemini auth login` (Google's PKCE flow opens in the "
            "browser). The Gemini CLI saves "
            "`~/.gemini/oauth_creds.json`; click 'Import from CLI' to copy "
            "the access + refresh token into Nerya."
        ),
    },
    "copilot": {
        "flow": "device_code",
        "verification_uri": "https://github.com/login/device",
        "instruction": (
            "Click 'Start device-code login'. Nerya talks to GitHub directly: "
            "you'll get a short code to type at https://github.com/login/device. "
            "Once you approve, the token is fetched and stored automatically — "
            "no CLI required."
        ),
    },
}


# --------------------------------------------------------------------- #
# Device-code provider config (Copilot today; extensible for future)
# --------------------------------------------------------------------- #

DEVICE_CODE_PROVIDERS: dict[str, dict[str, Any]] = {
    # GitHub Copilot uses GitHub's standard device-code flow with a
    # public client id assigned to the Copilot family of editors. The
    # client id is documented in the Copilot.vim / Copilot for Neovim
    # plugins and is the same one third-party clients use to obtain a
    # Copilot-eligible OAuth token for an authenticated GitHub user.
    "copilot": {
        "client_id": "Iv1.b507a08c87ecfe98",
        "device_code_url": "https://github.com/login/device/code",
        "token_url": "https://github.com/login/oauth/access_token",
        "scope": "read:user",
        "verification_uri": "https://github.com/login/device",
    },
}


class OAuthLoginError(NeryaError):
    """Raised when an OAuth import / status / revoke operation fails."""


@dataclass(frozen=True)
class OAuthProviderSpec:
    """Static metadata for a Nerya-managed OAuth login.

    The dashboard renders one card per spec, so this is also the shape
    the ``GET /llm/oauth/status`` endpoint serialises.
    """

    id: str
    display_name: str
    cli_name: str
    cli_paths: tuple[Path, ...]
    env_keys: tuple[str, ...]
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "cli_name": self.cli_name,
            "cli_paths": [str(p) for p in self.cli_paths],
            "cli_paths_present": [str(p) for p in self.cli_paths if p.exists()],
            "env_keys": list(self.env_keys),
            "env_keys_present": [k for k in self.env_keys if os.environ.get(k)],
            "description": self.description,
        }


def _home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


OAUTH_PROVIDERS: dict[str, OAuthProviderSpec] = {
    "openai-codex": OAuthProviderSpec(
        id="openai-codex",
        display_name="OpenAI Codex (ChatGPT login)",
        cli_name="codex",
        cli_paths=(
            _home_path(".codex", "auth.json"),
        ),
        env_keys=("CODEX_AUTH_TOKEN", "OPENAI_CODEX_TOKEN"),
        description=(
            "Reuse your ChatGPT Plus/Pro/Team subscription via the Codex "
            "Responses API. Run `codex login` first, then click 'Import "
            "credentials' to copy the token Nerya."
        ),
    ),
    "claude-code": OAuthProviderSpec(
        id="claude-code",
        display_name="Claude Code (Claude Pro/Max)",
        cli_name="claude",
        cli_paths=(
            _home_path(".claude", ".credentials.json"),
            _home_path(".config", "claude", "credentials.json"),
        ),
        env_keys=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_TOKEN"),
        description=(
            "Reuse your Claude Pro/Max subscription via the Claude Code "
            "OAuth token. Run `claude /login` first, then click 'Import "
            "credentials'. You can also paste a token retrieved with "
            "`claude setup-token`."
        ),
    ),
    "google-gemini-cli": OAuthProviderSpec(
        id="google-gemini-cli",
        display_name="Gemini CLI (Google OAuth)",
        cli_name="gemini",
        cli_paths=(
            _home_path(".gemini", "oauth_creds.json"),
            _home_path(".config", "google-gemini-cli", "oauth_creds.json"),
        ),
        env_keys=("GEMINI_OAUTH_TOKEN", "GOOGLE_OAUTH_TOKEN"),
        description=(
            "Reuse your Google account session from the Gemini CLI "
            "(`gemini auth login`). Imports the access + refresh token "
            "Google's PKCE flow wrote to disk; Nerya routes traffic "
            "through the cloudcode-pa endpoint just like hermes does. "
            "Paste-token fallback supports headless deployments where "
            "the operator has a service-account or short-lived token."
        ),
    ),
    "copilot": OAuthProviderSpec(
        id="copilot",
        display_name="GitHub Copilot (device code)",
        cli_name="gh",
        # GH Copilot caches its OAuth token in a few different paths
        # depending on the install (gh CLI extension, Neovim plugin, VS
        # Code extension, JetBrains, ...). We probe the union of the
        # most common locations and use whichever exists first.
        cli_paths=(
            _home_path(".config", "github-copilot", "apps.json"),
            _home_path(".config", "github-copilot", "hosts.json"),
            _home_path("AppData", "Local", "github-copilot", "apps.json"),
            _home_path("AppData", "Local", "github-copilot", "hosts.json"),
        ),
        env_keys=("COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN"),
        description=(
            "Reuse your GitHub Copilot subscription via the device-code "
            "flow. Easiest path: run the gh CLI's Copilot extension or "
            "VS Code at least once so the OAuth token lands in "
            "`~/.config/github-copilot/`, then click 'Import "
            "credentials'. You can also paste a fresh device-flow token."
        ),
    ),
}


def list_oauth_providers() -> list[dict[str, Any]]:
    """Public catalogue of Nerya-managed OAuth logins."""

    return [spec.to_dict() for spec in OAUTH_PROVIDERS.values()]


# --------------------------------------------------------------------- #
# Store helpers
# --------------------------------------------------------------------- #


def _open_store(config: Config, *, vault_passphrase: str | None = None) -> ProviderAuthStore:
    """Open the per-workspace provider-auth store, attaching the vault."""

    vault: SecretVault | None = None
    try:
        vault = SecretVault.open(config.paths.vault_enc, passphrase=vault_passphrase)
    except Exception:
        # The store accepts ``vault=None`` for tests / headless mode and
        # then keeps token strings inside the JSON record. We log nothing
        # — the caller will see ``vault_attached=False`` in status.
        vault = None
    return ProviderAuthStore.open(config.paths.provider_auth, vault=vault)


def _resolve_token(record: ProviderAuthRecord, store: ProviderAuthStore) -> str:
    """Return the live token for ``record`` (vault-resolved when present).

    Used by the router to satisfy LLM calls; never logged. Returns ``""``
    when the record is revoked or the vault entry is missing.
    """

    if not record.is_active():
        return ""
    secret = record.token_secret or ""
    if secret.startswith("vault://"):
        if store.vault is None:
            return ""
        try:
            return store.vault.resolve(
                secret.removeprefix("vault://"),
                required_scope=f"provider:{record.provider}",
            )
        except Exception:
            return ""
    return secret


# --------------------------------------------------------------------- #
# CLI import: Codex
# --------------------------------------------------------------------- #


def _read_codex_auth_file(path: Path) -> dict[str, Any]:
    """Decode ``~/.codex/auth.json`` written by ``codex login``.

    The current Codex CLI writes a JSON document with at least:

    .. code-block:: json

        {
          "OPENAI_API_KEY": "sk-...",
          "tokens": {
            "id_token":     "eyJ...",
            "access_token": "...",
            "refresh_token":"...",
            "account_id":   "..."
          },
          "last_refresh": "2025-..."
        }

    We do not parse or trust the JWT payloads — we just store the raw
    ``access_token`` (or fall back to ``OPENAI_API_KEY`` when only the
    static API key is present).
    """

    if not path.exists():
        raise OAuthLoginError(
            f"codex credentials file not found at {path} — run `codex login` first"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OAuthLoginError(f"failed to parse {path}: {exc}") from exc
    return raw if isinstance(raw, dict) else {}


def import_codex_credentials(
    config: Config,
    *,
    actor_id: str = "default",
    path: Path | str | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Copy Codex CLI credentials into the Nerya provider-auth store.

    Returns the redacted record on success.
    """

    spec = OAUTH_PROVIDERS["openai-codex"]
    candidates: tuple[Path, ...]
    if path is not None:
        candidates = (Path(path),)
    else:
        candidates = spec.cli_paths
    last_error: Exception | None = None
    parsed: dict[str, Any] | None = None
    used_path: Path | None = None
    for candidate in candidates:
        try:
            parsed = _read_codex_auth_file(candidate)
            used_path = candidate
            break
        except OAuthLoginError as exc:
            last_error = exc
            continue
    if parsed is None:
        raise last_error or OAuthLoginError("no codex credentials file found")

    tokens = parsed.get("tokens") or {}
    access_token = (
        str(tokens.get("access_token") or "").strip()
        or str(parsed.get("OPENAI_API_KEY") or "").strip()
    )
    if not access_token:
        raise OAuthLoginError(
            f"{used_path}: no access_token / OPENAI_API_KEY found in file"
        )
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    account_id = str(tokens.get("account_id") or parsed.get("account_id") or "").strip()
    expires_at = _parse_codex_expiry(parsed)

    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.register(
        provider="openai-codex",
        kind="oauth",
        actor_id=actor_id,
        scopes=["codex:chat"],
        token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        metadata={
            "source": "codex_cli_import",
            "source_path": str(used_path),
            "account_id": account_id,
            "imported_at": time.time(),
        },
    )
    return {
        "ok": True,
        "provider": "openai-codex",
        "actor_id": actor_id,
        "record": rec.public_view(),
        "source_path": str(used_path),
        "vault_attached": store.vault is not None,
    }


def _parse_codex_expiry(parsed: dict[str, Any]) -> float | None:
    """Best-effort: read ``last_refresh`` and assume a 28-day window.

    The Codex CLI does not write an explicit expiry. We set a soft expiry
    of 28 days from ``last_refresh`` so the dashboard can warn the
    operator before the next ``codex login`` is required. The router does
    not enforce this — it always tries the token first and reacts to
    401s, matching what the Codex CLI itself does.
    """

    raw = parsed.get("last_refresh") or parsed.get("updated_at")
    if not raw:
        return None
    try:
        # Try ISO-8601 first.
        from datetime import datetime
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        return ts + 28 * 24 * 3600
    except Exception:
        return None


# --------------------------------------------------------------------- #
# CLI import: Claude Code
# --------------------------------------------------------------------- #


def _read_claude_credentials_file(path: Path) -> dict[str, Any]:
    """Decode ``~/.claude/.credentials.json`` written by ``claude /login``.

    Recent Claude Code releases write:

    .. code-block:: json

        {
          "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-...",
            "refreshToken": "sk-ant-ort01-...",
            "expiresAt": 1760000000000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max"
          }
        }

    Older layouts store the same fields at the top level. We read both.
    """

    if not path.exists():
        raise OAuthLoginError(
            f"claude credentials file not found at {path} — run `claude /login` first"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OAuthLoginError(f"failed to parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("claudeAiOauth"), dict):
        return dict(raw["claudeAiOauth"])
    return raw


def import_claude_code_credentials(
    config: Config,
    *,
    actor_id: str = "default",
    path: Path | str | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Copy Claude Code OAuth credentials into the Nerya store."""

    spec = OAUTH_PROVIDERS["claude-code"]
    candidates: tuple[Path, ...]
    if path is not None:
        candidates = (Path(path),)
    else:
        candidates = spec.cli_paths
    last_error: Exception | None = None
    parsed: dict[str, Any] | None = None
    used_path: Path | None = None
    for candidate in candidates:
        try:
            parsed = _read_claude_credentials_file(candidate)
            used_path = candidate
            break
        except OAuthLoginError as exc:
            last_error = exc
            continue
    if parsed is None:
        raise last_error or OAuthLoginError("no claude credentials file found")

    access_token = (
        str(parsed.get("accessToken") or "").strip()
        or str(parsed.get("access_token") or "").strip()
    )
    if not access_token:
        raise OAuthLoginError(
            f"{used_path}: no accessToken found in file"
        )
    refresh_token = (
        str(parsed.get("refreshToken") or "").strip()
        or str(parsed.get("refresh_token") or "").strip()
    )
    expires_raw = parsed.get("expiresAt") or parsed.get("expires_at")
    expires_at = _parse_claude_expiry(expires_raw)
    scopes_raw = parsed.get("scopes") or parsed.get("scope") or []
    if isinstance(scopes_raw, str):
        scopes_raw = scopes_raw.split()
    scopes = [str(s) for s in scopes_raw if str(s)]
    subscription = str(
        parsed.get("subscriptionType") or parsed.get("subscription_type") or ""
    )

    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.register(
        provider="claude-code",
        kind="oauth",
        actor_id=actor_id,
        scopes=scopes or ["user:inference"],
        token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        metadata={
            "source": "claude_code_import",
            "source_path": str(used_path),
            "subscription": subscription,
            "imported_at": time.time(),
        },
    )
    return {
        "ok": True,
        "provider": "claude-code",
        "actor_id": actor_id,
        "record": rec.public_view(),
        "source_path": str(used_path),
        "vault_attached": store.vault is not None,
    }


def _parse_claude_expiry(value: Any) -> float | None:
    if value is None:
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    # Claude Code writes epoch milliseconds; normalise to seconds.
    if ms > 1e12:
        return ms / 1000.0
    return ms


# --------------------------------------------------------------------- #
# CLI import: Gemini CLI
# --------------------------------------------------------------------- #


def _read_gemini_oauth_file(path: Path) -> dict[str, Any]:
    """Decode the Gemini CLI's OAuth blob (``~/.gemini/oauth_creds.json``).

    Recent Gemini CLI builds emit Google's standard OAuth-token JSON:

    .. code-block:: json

        {
          "access_token":  "ya29...",
          "refresh_token": "1//0...",
          "scope":         "https://www.googleapis.com/auth/generative-language",
          "token_type":    "Bearer",
          "expiry_date":   1726000000000
        }

    Older builds (and the headless service-account variant) put the
    same fields directly at the top level under the older
    ``credentials`` wrapping. We read both shapes.
    """

    if not path.exists():
        raise OAuthLoginError(
            f"gemini credentials file not found at {path} — run `gemini auth login` first"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OAuthLoginError(f"failed to parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("credentials"), dict):
        return dict(raw["credentials"])
    return raw


def import_gemini_cli_credentials(
    config: Config,
    *,
    actor_id: str = "default",
    path: Path | str | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Copy Gemini CLI credentials into the Nerya provider-auth store."""

    spec = OAUTH_PROVIDERS["google-gemini-cli"]
    candidates: tuple[Path, ...] = (
        (Path(path),) if path is not None else spec.cli_paths
    )
    parsed: dict[str, Any] | None = None
    used_path: Path | None = None
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = _read_gemini_oauth_file(candidate)
            used_path = candidate
            break
        except OAuthLoginError as exc:
            last_error = exc
            continue
    if parsed is None:
        raise last_error or OAuthLoginError("no gemini credentials file found")

    access_token = (
        str(parsed.get("access_token") or "").strip()
        or str(parsed.get("accessToken") or "").strip()
    )
    if not access_token:
        raise OAuthLoginError(f"{used_path}: no access_token found in file")
    refresh_token = (
        str(parsed.get("refresh_token") or "").strip()
        or str(parsed.get("refreshToken") or "").strip()
    )
    expires_at = _parse_gemini_expiry(parsed)
    scope = str(parsed.get("scope") or "")
    scopes = [s for s in scope.split() if s] or ["generative-language"]

    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.register(
        provider="google-gemini-cli",
        kind="oauth",
        actor_id=actor_id,
        scopes=scopes,
        token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        metadata={
            "source": "gemini_cli_import",
            "source_path": str(used_path),
            "imported_at": time.time(),
        },
    )
    return {
        "ok": True,
        "provider": "google-gemini-cli",
        "actor_id": actor_id,
        "record": rec.public_view(),
        "source_path": str(used_path),
        "vault_attached": store.vault is not None,
    }


def _parse_gemini_expiry(parsed: dict[str, Any]) -> float | None:
    raw = parsed.get("expiry_date") or parsed.get("expires_at")
    if raw is None:
        return None
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return None
    return ms / 1000.0 if ms > 1e12 else ms


# --------------------------------------------------------------------- #
# CLI import: GitHub Copilot
# --------------------------------------------------------------------- #


def _read_copilot_credentials_file(path: Path) -> dict[str, Any]:
    """Decode a GitHub Copilot credential file.

    The gh CLI writes a host-keyed dict at
    ``~/.config/github-copilot/hosts.json`` of the form:

    .. code-block:: json

        {
          "github.com": {
            "user": "octocat",
            "oauth_token": "ghu_..."
          }
        }

    The VS Code extension uses ``apps.json`` with the same overall
    shape. We pluck the first entry that has an ``oauth_token``.
    """

    if not path.exists():
        raise OAuthLoginError(
            f"copilot credentials file not found at {path} — run the gh Copilot extension or VS Code at least once"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OAuthLoginError(f"failed to parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        return {}
    return raw


def import_copilot_credentials(
    config: Config,
    *,
    actor_id: str = "default",
    path: Path | str | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Copy GitHub Copilot credentials into the Nerya provider-auth store."""

    spec = OAUTH_PROVIDERS["copilot"]
    candidates: tuple[Path, ...] = (
        (Path(path),) if path is not None else spec.cli_paths
    )
    parsed: dict[str, Any] | None = None
    used_path: Path | None = None
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = _read_copilot_credentials_file(candidate)
            used_path = candidate
            break
        except OAuthLoginError as exc:
            last_error = exc
            continue
    if parsed is None:
        raise last_error or OAuthLoginError("no copilot credentials file found")

    # Walk every host entry until we find one that has a token. The
    # entries are themselves dicts; non-dict values (e.g. a top-level
    # version key the VS Code extension occasionally writes) are
    # skipped, not treated as errors.
    access_token = ""
    user_login = ""
    for key, val in parsed.items():
        if not isinstance(val, dict):
            continue
        token = (
            str(val.get("oauth_token") or "").strip()
            or str(val.get("token") or "").strip()
        )
        if token:
            access_token = token
            user_login = str(val.get("user") or val.get("login") or "")
            break
    if not access_token:
        raise OAuthLoginError(
            f"{used_path}: no oauth_token found in any host entry"
        )

    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.register(
        provider="copilot",
        kind="oauth",
        actor_id=actor_id,
        scopes=["copilot:chat"],
        token=access_token,
        refresh_token="",
        expires_at=None,  # GitHub OAuth user tokens are long-lived.
        metadata={
            "source": "copilot_cli_import",
            "source_path": str(used_path),
            "github_user": user_login,
            "imported_at": time.time(),
        },
    )
    return {
        "ok": True,
        "provider": "copilot",
        "actor_id": actor_id,
        "record": rec.public_view(),
        "source_path": str(used_path),
        "vault_attached": store.vault is not None,
    }


# --------------------------------------------------------------------- #
# Generic facade
# --------------------------------------------------------------------- #


def import_provider_credentials(
    config: Config,
    *,
    provider: str,
    actor_id: str = "default",
    path: Path | str | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Dispatch import-from-CLI to the right helper based on provider id."""

    pid = (provider or "").strip().lower()
    if pid == "openai-codex":
        return import_codex_credentials(
            config, actor_id=actor_id, path=path, vault_passphrase=vault_passphrase,
        )
    if pid == "claude-code":
        return import_claude_code_credentials(
            config, actor_id=actor_id, path=path, vault_passphrase=vault_passphrase,
        )
    if pid == "google-gemini-cli":
        return import_gemini_cli_credentials(
            config, actor_id=actor_id, path=path, vault_passphrase=vault_passphrase,
        )
    if pid == "copilot":
        return import_copilot_credentials(
            config, actor_id=actor_id, path=path, vault_passphrase=vault_passphrase,
        )
    raise OAuthLoginError(f"provider {provider!r} does not support OAuth import")


def set_paste_token(
    config: Config,
    *,
    provider: str,
    token: str,
    refresh_token: str = "",
    actor_id: str = "default",
    expires_at: float | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Persist a manually-pasted OAuth token (e.g. from `claude setup-token`)."""

    pid = (provider or "").strip().lower()
    if pid not in OAUTH_PROVIDERS:
        raise OAuthLoginError(f"provider {provider!r} is not OAuth-managed")
    secret = (token or "").strip()
    if not secret:
        raise OAuthLoginError("paste token is required")
    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.register(
        provider=pid,
        kind="oauth",
        actor_id=actor_id,
        scopes=["paste"],
        token=secret,
        refresh_token=(refresh_token or "").strip(),
        expires_at=expires_at,
        metadata={
            "source": "paste_token",
            "imported_at": time.time(),
        },
    )
    return {
        "ok": True,
        "provider": pid,
        "actor_id": actor_id,
        "record": rec.public_view(),
        "vault_attached": store.vault is not None,
    }


def revoke(
    config: Config,
    *,
    provider: str,
    actor_id: str = "default",
    reason: str = "manual",
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    pid = (provider or "").strip().lower()
    store = _open_store(config, vault_passphrase=vault_passphrase)
    if store.get(pid, actor_id) is None:
        return {"ok": False, "error": "not_configured", "provider": pid}
    store.revoke(pid, actor_id, reason=reason)
    return {"ok": True, "provider": pid, "actor_id": actor_id}


def status_for(
    config: Config,
    *,
    provider: str,
    actor_id: str = "default",
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    pid = (provider or "").strip().lower()
    spec = OAUTH_PROVIDERS.get(pid)
    if spec is None:
        raise OAuthLoginError(f"provider {provider!r} is not OAuth-managed")
    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.get(pid, actor_id)
    return {
        "provider": pid,
        "spec": spec.to_dict(),
        "configured": rec is not None,
        "active": bool(rec and rec.is_active()),
        "expired": bool(rec and rec.is_expired()),
        "record": rec.public_view() if rec else None,
        "vault_attached": store.vault is not None,
    }


def all_status(
    config: Config,
    *,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Aggregate status for every OAuth-managed provider."""

    out = []
    for pid in OAUTH_PROVIDERS:
        try:
            out.append(status_for(
                config, provider=pid, vault_passphrase=vault_passphrase,
            ))
        except OAuthLoginError as exc:
            out.append({"provider": pid, "error": str(exc)})
    return {"providers": out}


# --------------------------------------------------------------------- #
# Router integration: token resolution
# --------------------------------------------------------------------- #


def login_directive(provider: str) -> dict[str, Any]:
    """Return a declarative *how-to-log-in* descriptor for the dashboard.

    Shape::

        {
          "provider": "openai-codex",
          "flow": "cli" | "device_code" | "paste",
          "command": "codex login",       # only for flow=cli
          "verification_uri": "...",      # only for flow=device_code
          "instruction": "Run `codex login` in your shell ..."
        }

    The dashboard renders ``instruction`` verbatim and uses ``flow``
    to decide which controls (copyable command vs Start-device-code
    button) to show.
    """

    pid = (provider or "").strip().lower()
    if pid not in OAUTH_PROVIDERS:
        raise OAuthLoginError(f"provider {provider!r} is not OAuth-managed")
    base: dict[str, Any] = {"provider": pid}
    base.update(_LOGIN_DIRECTIVES.get(pid, {"flow": "paste"}))
    return base


# --------------------------------------------------------------------- #
# Device-code flow (Copilot)
# --------------------------------------------------------------------- #


def _http_post(url: str, *, data: dict[str, str], accept: str) -> dict[str, Any]:
    """Tiny stdlib POST that returns the JSON body.

    Importing requests/httpx for one POST in two places is overkill;
    ``urllib`` is already in the stdlib and adequate here.
    """

    import urllib.request
    import urllib.parse

    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Nerya/oauth-device-code",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            payload = resp.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise OAuthLoginError(f"HTTP POST {url} failed: {exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        # GitHub will return form-urlencoded if Accept is missing/ignored.
        return {"raw": payload, "decode_error": str(exc)}


def device_code_start(provider: str) -> dict[str, Any]:
    """Kick off a device-code flow for ``provider``.

    Returns ``{user_code, verification_uri, interval, expires_in,
    device_code, expires_at}``. The dashboard shows the user the
    code + verification URL, then polls :func:`device_code_poll`
    every ``interval`` seconds until the operator approves.

    ``device_code`` is the secret used for polling; we return it
    over the locally-bound dashboard API so it never leaves the
    operator's machine.
    """

    pid = (provider or "").strip().lower()
    cfg = DEVICE_CODE_PROVIDERS.get(pid)
    if cfg is None:
        raise OAuthLoginError(
            f"provider {provider!r} does not support device-code login"
        )
    payload = _http_post(
        cfg["device_code_url"],
        data={"client_id": cfg["client_id"], "scope": cfg["scope"]},
        accept="application/json",
    )
    if "device_code" not in payload:
        raise OAuthLoginError(
            f"device-code start failed: {payload.get('error') or payload}"
        )
    interval = int(payload.get("interval") or 5)
    expires_in = int(payload.get("expires_in") or 900)
    return {
        "provider": pid,
        "device_code": str(payload["device_code"]),
        "user_code": str(payload.get("user_code") or ""),
        "verification_uri": str(
            payload.get("verification_uri")
            or cfg.get("verification_uri")
            or ""
        ),
        "verification_uri_complete": str(
            payload.get("verification_uri_complete") or ""
        ),
        "interval": interval,
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
    }


def device_code_poll(
    config: Config,
    *,
    provider: str,
    device_code: str,
    actor_id: str = "default",
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Poll the OAuth token endpoint once.

    Returns one of three shapes:

    * ``{"status": "pending"}`` — keep polling.
    * ``{"status": "slow_down", "interval": N}`` — increase the poll interval.
    * ``{"status": "ok", "record": {...}}`` — token persisted; stop polling.
    * ``{"status": "error", "error": "..."}`` — terminal failure.

    The dashboard treats anything except ``"ok"`` as a continue-or-stop
    signal it shows to the operator.
    """

    pid = (provider or "").strip().lower()
    cfg = DEVICE_CODE_PROVIDERS.get(pid)
    if cfg is None:
        return {"status": "error", "error": f"provider {provider!r} unsupported"}
    payload = _http_post(
        cfg["token_url"],
        data={
            "client_id": cfg["client_id"],
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        accept="application/json",
    )
    err = str(payload.get("error") or "")
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        return {
            "status": "slow_down",
            "interval": int(payload.get("interval") or 10),
        }
    if err in {"expired_token", "access_denied", "unsupported_grant_type"}:
        return {"status": "error", "error": err}
    if err:
        # Unknown error code; surface verbatim to the operator.
        return {"status": "error", "error": err}

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return {"status": "error", "error": "no access_token in response"}
    scope = str(payload.get("scope") or cfg.get("scope") or "")
    scopes = [s for s in scope.replace(",", " ").split() if s]
    store = _open_store(config, vault_passphrase=vault_passphrase)
    rec = store.register(
        provider=pid,
        kind="oauth",
        actor_id=actor_id,
        scopes=scopes or [cfg.get("scope") or "device_code"],
        token=access_token,
        refresh_token="",
        expires_at=None,  # GitHub OAuth tokens are long-lived.
        metadata={
            "source": "device_code",
            "imported_at": time.time(),
            "token_type": str(payload.get("token_type") or "bearer"),
        },
    )
    return {
        "status": "ok",
        "provider": pid,
        "actor_id": actor_id,
        "record": rec.public_view(),
    }


def resolve_oauth_token(
    config: Config,
    *,
    provider: str,
    actor_id: str = "default",
    vault_passphrase: str | None = None,
) -> str:
    """Return the live OAuth token for ``provider`` (or empty string).

    Preference order: stored record > env var. The model router calls
    this before falling back to the catalogue's env-var chain.
    """

    pid = (provider or "").strip().lower()
    if pid not in OAUTH_PROVIDERS:
        return ""
    try:
        store = _open_store(config, vault_passphrase=vault_passphrase)
    except (ProviderAuthError, Exception):
        return ""
    rec = store.get(pid, actor_id)
    if rec is None:
        return ""
    token = _resolve_token(rec, store)
    if token:
        store.mark_used(pid, actor_id)
    return token
