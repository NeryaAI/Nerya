"""Central provider catalogue for the LLM control plane.

Historically Nerya's provider knowledge was spread across three places:

* ``nerya/llm/adapters/openai.py`` — ``DEFAULT_BASE_URLS`` (OpenAI-compat
  family only).
* ``nerya/llm/adapters/__init__.py`` — ``builtin_providers()`` (which
  adapter to dispatch to).
* ``dashboard/app/settings/page.tsx`` — hardcoded ``KNOWN_LLM_PROVIDERS``
  + ``DEFAULT_PROVIDER_BASE_URLS`` lists.

That meant adding a provider needed three coordinated edits and the
dashboard would fall behind silently. This module owns the single
source of truth: provider id, display name, default base URL,
auth metadata, default env-var fallbacks, API shape, and aliases.

The dashboard fetches the same data from ``GET /llm/catalog`` so adding
a provider here automatically lights it up in the UI.

The provider catalogue covers the same providers as hermes-agent's
``hermes_cli/auth.py`` ``PROVIDER_REGISTRY`` plus Nerya's existing set
(stepfun, ollama-local, mock).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


__all__ = [
    "ProviderEntry",
    "PROVIDER_CATALOG",
    "PROVIDER_ALIASES",
    "DEFAULT_BASE_URLS",
    "REASONING_EFFORT_LEVELS",
    "lookup",
    "resolve_alias",
    "default_base_url",
    "catalog_for_dashboard",
    "default_env_keys_for",
    "anthropic_compat_provider_ids",
    "openai_compat_provider_ids",
]


# Reasoning effort levels exposed to operators.
# * ``none`` disables reasoning even on capable models (cheap fast tiers).
# * ``minimal``/``low``/``medium``/``high`` map 1:1 to OpenAI Responses
#   ``reasoning.effort`` and Anthropic adaptive ``output_config.effort``.
# * ``extra_high`` maps to OpenAI's ``xhigh`` rung (Codex / o-series) and
#   Anthropic's adaptive ``xhigh`` (claude-opus-4-7+, claude-4.6+).
REASONING_EFFORT_LEVELS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "extra_high",
)


@dataclass(frozen=True)
class ProviderEntry:
    """Single provider description shared across Python + dashboard.

    ``api_mode`` decides which adapter family handles the call:
    ``chat_completions`` is the OpenAI-compat family, ``anthropic_messages``
    is the Anthropic Messages family, ``codex_responses`` is the OpenAI
    Codex/Responses API, ``gemini_v1beta`` is Google AI Studio,
    ``cloudcode_pa`` is the Gemini-OAuth Cloud Code Assist API, and
    ``ollama_native`` is local Ollama.
    """

    id: str
    name: str
    api_mode: str = "chat_completions"
    auth_type: str = "api_key"  # api_key | oauth_external | oauth_device_code | external_process | aws_sdk
    base_url: str = ""
    env_keys: tuple[str, ...] = ()
    base_url_env: str = ""
    aliases: tuple[str, ...] = ()
    description: str = ""
    family: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "api_mode": self.api_mode,
            "auth_type": self.auth_type,
            "base_url": self.base_url,
            "env_keys": list(self.env_keys),
            "base_url_env": self.base_url_env,
            "aliases": list(self.aliases),
            "description": self.description,
            "family": self.family,
            "extra": dict(self.extra),
        }


# --------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------- #
# Order matters for the UI: items appear in this order in dropdowns.

PROVIDER_CATALOG: tuple[ProviderEntry, ...] = (
    # ---------- OpenAI family --------------------------------------
    ProviderEntry(
        id="openai",
        name="OpenAI",
        family="openai",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.openai.com/v1",
        env_keys=("OPENAI_API_KEY",),
        base_url_env="OPENAI_BASE_URL",
        description="OpenAI Chat Completions + Responses (gpt-4o, gpt-4.1, o3, gpt-5).",
    ),
    ProviderEntry(
        id="openai-codex",
        name="OpenAI Codex (ChatGPT login)",
        family="openai",
        api_mode="codex_responses",
        auth_type="oauth_external",
        base_url="https://chatgpt.com/backend-api/codex",
        description="ChatGPT plan via Codex Responses API. Use 'Sign in with ChatGPT' or import ~/.codex/auth.json.",
    ),
    ProviderEntry(
        id="anthropic",
        name="Anthropic",
        family="anthropic",
        api_mode="anthropic_messages",
        auth_type="api_key",
        base_url="https://api.anthropic.com",
        env_keys=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        base_url_env="ANTHROPIC_BASE_URL",
        description="Anthropic Claude (sonnet/opus/haiku). Supports Claude Code OAuth token via env or import.",
    ),
    ProviderEntry(
        id="claude-code",
        name="Claude Code (subscription)",
        family="anthropic",
        api_mode="anthropic_messages",
        auth_type="oauth_external",
        base_url="https://api.anthropic.com",
        env_keys=("CLAUDE_CODE_OAUTH_TOKEN",),
        description="Claude Pro/Max subscription via Claude Code OAuth. Import ~/.claude/.credentials.json or paste token.",
    ),
    # ---------- Google / Gemini ------------------------------------
    ProviderEntry(
        id="gemini",
        name="Google AI Studio (Gemini)",
        family="google",
        api_mode="gemini_v1beta",
        auth_type="api_key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        env_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        base_url_env="GEMINI_BASE_URL",
        description="Google AI Studio Gemini 2.0 Flash, 1.5 Pro/Flash via API key.",
    ),
    ProviderEntry(
        id="google-gemini-cli",
        name="Google Gemini (OAuth, free tier)",
        family="google",
        api_mode="cloudcode_pa",
        auth_type="oauth_external",
        base_url="https://cloudcode-pa.googleapis.com",
        description="Cloud Code Assist via Google OAuth (PKCE). Free tier supported. (deferred — login flow not yet wired)",
        extra={"deferred": True},
    ),
    # ---------- Aggregators ----------------------------------------
    ProviderEntry(
        id="openrouter",
        name="OpenRouter",
        family="openrouter",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://openrouter.ai/api/v1",
        env_keys=("OPENROUTER_API_KEY",),
        base_url_env="OPENROUTER_BASE_URL",
        description="Multi-provider router. Supports per-call provider preference block.",
    ),
    ProviderEntry(
        id="ai-gateway",
        name="Vercel AI Gateway",
        family="ai-gateway",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://ai-gateway.vercel.sh/v1",
        env_keys=("AI_GATEWAY_API_KEY",),
        base_url_env="AI_GATEWAY_BASE_URL",
        description="Vercel AI Gateway routes to many providers via one OpenAI-shaped key.",
    ),
    # ---------- Chinese providers ----------------------------------
    ProviderEntry(
        id="deepseek",
        name="DeepSeek",
        family="deepseek",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.deepseek.com/v1",
        env_keys=("DEEPSEEK_API_KEY",),
        base_url_env="DEEPSEEK_BASE_URL",
        description="DeepSeek V3 + R1 (reasoning).",
    ),
    ProviderEntry(
        id="moonshot",
        name="Moonshot (Kimi, Intl)",
        family="moonshot",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.moonshot.ai/v1",
        env_keys=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        base_url_env="KIMI_BASE_URL",
        aliases=("kimi-coding", "kimi"),
        description="Kimi K2 family (international endpoint).",
    ),
    ProviderEntry(
        id="moonshot-cn",
        name="Moonshot (Kimi, China)",
        family="moonshot",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.moonshot.cn/v1",
        env_keys=("KIMI_CN_API_KEY", "MOONSHOT_CN_API_KEY"),
        aliases=("kimi-coding-cn", "kimi-cn"),
        description="Kimi K2 family (China endpoint).",
    ),
    ProviderEntry(
        id="zai",
        name="Z.AI / GLM",
        family="zhipu",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.z.ai/api/paas/v4",
        env_keys=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env="GLM_BASE_URL",
        aliases=("glm", "zhipu"),
        description="Zhipu AI / GLM family (glm-4-plus, glm-4-flash, glm-z1).",
    ),
    ProviderEntry(
        id="alibaba",
        name="Alibaba DashScope (Qwen)",
        family="qwen",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env_keys=("DASHSCOPE_API_KEY",),
        base_url_env="DASHSCOPE_BASE_URL",
        aliases=("dashscope", "qwen"),
        description="Qwen 2.5/3 family via Alibaba DashScope.",
    ),
    ProviderEntry(
        id="qwen-oauth",
        name="Qwen OAuth",
        family="qwen",
        api_mode="chat_completions",
        auth_type="oauth_external",
        base_url="https://chat.qwen.ai/api",
        description="Qwen OAuth via the Qwen-CLI flow. (deferred — login flow not yet wired)",
        extra={"deferred": True},
    ),
    ProviderEntry(
        id="minimax",
        name="MiniMax (Intl)",
        family="minimax",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.minimax.io/v1",
        env_keys=("MINIMAX_API_KEY",),
        base_url_env="MINIMAX_BASE_URL",
        description="MiniMax family via the OpenAI-compatible Chat Completions API.",
    ),
    ProviderEntry(
        id="minimax-cn",
        name="MiniMax (China)",
        family="minimax",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.minimaxi.com/v1",
        env_keys=("MINIMAX_CN_API_KEY",),
        base_url_env="MINIMAX_CN_BASE_URL",
        description="MiniMax China endpoint via the OpenAI-compatible Chat Completions API.",
    ),
    ProviderEntry(
        id="stepfun",
        name="StepFun (阶跃星辰)",
        family="stepfun",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.stepfun.com/v1",
        env_keys=("STEPFUN_API_KEY", "STEP_API_KEY"),
        base_url_env="STEPFUN_BASE_URL",
        description="StepFun Step-1/Step-2/Step-1V family.",
    ),
    ProviderEntry(
        id="xiaomi",
        name="Xiaomi MiMo",
        family="xiaomi",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.xiaomimimo.com/v1",
        env_keys=("XIAOMI_API_KEY",),
        base_url_env="XIAOMI_BASE_URL",
        aliases=("mimo", "xiaomi-mimo"),
        description="Xiaomi MiMo family.",
    ),
    # ---------- B.AI (AI-agent financial infra gateway) -------------
    ProviderEntry(
        id="bai",
        name="B.AI (Agent Compute Gateway)",
        family="bai",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.b.ai/v1",
        env_keys=("BAI_API_KEY", "B_AI_API_KEY"),
        base_url_env="BAI_BASE_URL",
        aliases=("b.ai", "b-ai", "bai-gateway"),
        description=(
            "B.AI unified LLM gateway \u2014 permissionless access to frontier "
            "models with x402 agent payments and 8004 on-chain agent identity "
            "(HTX Genesis ecosystem AI compute)."
        ),
    ),
    # ---------- xAI / Grok -----------------------------------------
    ProviderEntry(
        id="xai",
        name="xAI (Grok)",
        family="xai",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.x.ai/v1",
        env_keys=("XAI_API_KEY",),
        base_url_env="XAI_BASE_URL",
        description="xAI Grok 2 / Grok 3 / Grok 4.",
    ),
    # ---------- Mistral / Cohere / etc. ----------------------------
    ProviderEntry(
        id="mistral",
        name="Mistral",
        family="mistral",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.mistral.ai/v1",
        env_keys=("MISTRAL_API_KEY",),
        base_url_env="MISTRAL_BASE_URL",
        description="Mistral Large 2 / Codestral / Pixtral.",
    ),
    ProviderEntry(
        id="together",
        name="Together AI",
        family="together",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.together.xyz/v1",
        env_keys=("TOGETHER_API_KEY",),
        base_url_env="TOGETHER_BASE_URL",
        description="Together AI hosted open-weights inference.",
    ),
    ProviderEntry(
        id="groq",
        name="Groq",
        family="groq",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.groq.com/openai/v1",
        env_keys=("GROQ_API_KEY",),
        base_url_env="GROQ_BASE_URL",
        description="Groq fast LPU inference (Llama 3.3, Mixtral, DeepSeek-R1-distill).",
    ),
    ProviderEntry(
        id="cerebras",
        name="Cerebras",
        family="cerebras",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.cerebras.ai/v1",
        env_keys=("CEREBRAS_API_KEY",),
        base_url_env="CEREBRAS_BASE_URL",
        description="Cerebras wafer-scale inference.",
    ),
    ProviderEntry(
        id="huggingface",
        name="Hugging Face Router",
        family="huggingface",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://router.huggingface.co/v1",
        env_keys=("HF_TOKEN",),
        base_url_env="HF_BASE_URL",
        aliases=("hf",),
        description="Hugging Face inference router (multi-provider).",
    ),
    ProviderEntry(
        id="nvidia",
        name="NVIDIA NIM",
        family="nvidia",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://integrate.api.nvidia.com/v1",
        env_keys=("NVIDIA_API_KEY",),
        base_url_env="NVIDIA_BASE_URL",
        description="NVIDIA NIM hosted endpoints.",
    ),
    ProviderEntry(
        id="arcee",
        name="Arcee AI",
        family="arcee",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.arcee.ai/api/v1",
        env_keys=("ARCEEAI_API_KEY",),
        base_url_env="ARCEE_BASE_URL",
        aliases=("arcee-ai", "arceeai"),
        description="Arcee AI agent models.",
    ),
    ProviderEntry(
        id="kilocode",
        name="Kilo Code",
        family="kilocode",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://api.kilo.ai/api/gateway",
        env_keys=("KILOCODE_API_KEY",),
        base_url_env="KILOCODE_BASE_URL",
        description="Kilo Code multi-model gateway.",
    ),
    ProviderEntry(
        id="opencode-zen",
        name="OpenCode Zen",
        family="opencode",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://opencode.ai/zen/v1",
        env_keys=("OPENCODE_ZEN_API_KEY",),
        base_url_env="OPENCODE_ZEN_BASE_URL",
        description="OpenCode Zen subscription gateway.",
    ),
    ProviderEntry(
        id="opencode-go",
        name="OpenCode Go",
        family="opencode",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://opencode.ai/zen/go/v1",
        env_keys=("OPENCODE_GO_API_KEY",),
        base_url_env="OPENCODE_GO_BASE_URL",
        description="OpenCode Go subscription gateway. Mixes OpenAI-shaped + Anthropic-shaped per model.",
    ),
    # ---------- GitHub Copilot -------------------------------------
    ProviderEntry(
        id="copilot",
        name="GitHub Copilot",
        family="copilot",
        api_mode="chat_completions",
        auth_type="oauth_device_code",
        base_url="https://api.githubcopilot.com",
        env_keys=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        base_url_env="COPILOT_API_BASE_URL",
        description="GitHub Copilot OAuth device-code. (deferred — device-code flow not yet wired)",
        extra={"deferred": True},
    ),
    # ---------- Local / open-source --------------------------------
    ProviderEntry(
        id="ollama",
        name="Ollama (local)",
        family="ollama",
        api_mode="ollama_native",
        auth_type="api_key",
        base_url="http://127.0.0.1:11434",
        env_keys=(),
        base_url_env="OLLAMA_BASE_URL",
        description="Local Ollama runtime. No API key required.",
        extra={"key_optional": True},
    ),
    ProviderEntry(
        id="ollama-cloud",
        name="Ollama Cloud",
        family="ollama",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="https://ollama.com",
        env_keys=("OLLAMA_API_KEY",),
        base_url_env="OLLAMA_CLOUD_BASE_URL",
        description="Ollama Cloud hosted runtime (OpenAI-compatible).",
    ),
    # ---------- AWS Bedrock ----------------------------------------
    ProviderEntry(
        id="bedrock",
        name="AWS Bedrock",
        family="bedrock",
        api_mode="bedrock",
        auth_type="aws_sdk",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        env_keys=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        base_url_env="BEDROCK_BASE_URL",
        description="AWS Bedrock (Anthropic / Cohere / Meta models). Auth via AWS SDK chain.",
    ),
    # ---------- Custom / catch-all ---------------------------------
    ProviderEntry(
        id="compat",
        name="Custom OpenAI-compatible",
        family="custom",
        api_mode="chat_completions",
        auth_type="api_key",
        base_url="",
        env_keys=(),
        description="Any OpenAI-compatible endpoint (vLLM, SGLang, LiteLLM, custom proxies).",
    ),
    ProviderEntry(
        id="anthropic-compat",
        name="Custom Anthropic-compatible",
        family="custom",
        api_mode="anthropic_messages",
        auth_type="api_key",
        base_url="",
        env_keys=(),
        description="Any Anthropic-Messages-shaped endpoint (claude proxies, Bedrock-compat servers).",
    ),
    # ---------- Mock (tests) ---------------------------------------
    ProviderEntry(
        id="mock",
        name="Deterministic mock",
        family="mock",
        api_mode="mock",
        auth_type="api_key",
        base_url="",
        description="Deterministic offline tier for tests / paper mode.",
        extra={"key_optional": True, "hidden": True},
    ),
)


# Reverse alias map: alias → canonical id.
PROVIDER_ALIASES: dict[str, str] = {}
for _entry in PROVIDER_CATALOG:
    for _alias in _entry.aliases:
        PROVIDER_ALIASES[_alias.lower()] = _entry.id


# --------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------- #


def lookup(provider_id: str) -> ProviderEntry | None:
    """Return the catalog entry for ``provider_id`` (alias-aware)."""
    if not provider_id:
        return None
    pid = resolve_alias(provider_id)
    for entry in PROVIDER_CATALOG:
        if entry.id == pid:
            return entry
    return None


def resolve_alias(provider_id: str) -> str:
    """Map a provider id (or alias) back to its canonical catalog id."""
    pid = (provider_id or "").strip().lower()
    return PROVIDER_ALIASES.get(pid, pid)


def default_base_url(provider_id: str) -> str:
    entry = lookup(provider_id)
    return entry.base_url if entry else ""


def default_env_keys_for(provider_id: str) -> tuple[str, ...]:
    entry = lookup(provider_id)
    return entry.env_keys if entry else ()


def anthropic_compat_provider_ids() -> tuple[str, ...]:
    """Provider ids whose API shape is Anthropic Messages.

    The model_router uses this set to dispatch to the AnthropicAdapter
    instead of the OpenAI-compat adapter.
    """
    return tuple(
        e.id for e in PROVIDER_CATALOG
        if e.api_mode == "anthropic_messages"
    )


def openai_compat_provider_ids() -> tuple[str, ...]:
    """Provider ids served by the OpenAI Chat Completions adapter family."""
    return tuple(
        e.id for e in PROVIDER_CATALOG
        if e.api_mode == "chat_completions"
    )


def catalog_for_dashboard() -> list[dict[str, Any]]:
    """Serialised catalog for the ``/llm/catalog`` endpoint.

    Hidden entries (``extra.hidden=True``) are excluded — the mock
    provider is internal-only and not something an operator should pick
    in the UI.
    """
    rows = []
    for entry in PROVIDER_CATALOG:
        if entry.extra.get("hidden"):
            continue
        rows.append(entry.to_dict())
    return rows


# --------------------------------------------------------------------- #
# Backwards-compat re-export
# --------------------------------------------------------------------- #
# Some legacy callers import ``DEFAULT_BASE_URLS`` from
# ``nerya.llm.adapters.openai``. We keep that map intact there but also
# expose the same dict from this module so new code can pull a flat
# id→url map without enumerating the dataclass.

DEFAULT_BASE_URLS: dict[str, str] = {
    e.id: e.base_url for e in PROVIDER_CATALOG if e.base_url
}
