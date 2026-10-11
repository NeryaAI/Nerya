from __future__ import annotations

from copy import copy, deepcopy
import time

from ..llm.gateway import LLMGateway
from ..llm import ops as _llm_ops
from ..core.errors import LLMTaskNotAllowed, LLMTierDenied


def _probe_failure_kind(exc):
    """Expose an actionable category without returning provider bodies or keys."""
    message = str(exc).lower()
    status = int(getattr(exc, "status_code", 0) or 0)
    for marker, kind in (
        ("reasoning_off_unsupported", "unsupported_setting"),
        ("context_length_exceeded", "context_limit"),
        ("missing_api_key", "missing_credentials"),
        ("requires api_key", "missing_credentials"),
        ("probe_output_truncated", "output_truncated"),
        ("probe_empty_response", "empty_response"),
        ("probe_tool_not_called", "tool_not_called"),
    ):
        if marker in message:
            return kind
    if isinstance(exc, (LLMTaskNotAllowed, LLMTierDenied)):
        return "task_not_allowed"
    if isinstance(exc, TimeoutError) or "timed out" in message or "deadline" in message:
        return "timeout"
    if status in (401, 403):
        return "authentication"
    if status == 404:
        return "model_not_found"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "provider_unavailable"
    if status == 400 or status == 422:
        return "invalid_request"
    return "unknown"


def _messages_probe(client, payload):
    """POST /llm/messages/probe — lightweight real messages-backend probe.

    This is intentionally separate from ``/llm/config``: config only proves a
    provider/key ref is present, while prompt E2E needs to know the selected
    provider can actually complete a provider-native messages call right now.
    """
    body = payload or {}
    revision = _llm_ops.config_revision(client.config)
    if body.get("revision") and body["revision"] != revision:
        return {"_status": 409, "ok": False, "error": "llm_config_changed", "revision": revision}
    probe_config = copy(client.config)
    if hasattr(client.config, "data"):
        probe_config.data = deepcopy(client.config.data)
    tier = str(body.get("tier") or _llm_ops._get_cfg(probe_config, "llm.default_tier", "medium")).strip() or "medium"
    model_provider = str(body.get("model_provider") or body.get("provider") or "").strip()
    model_id = str(body.get("model_id") or body.get("model") or "").strip()
    tool_probe = bool(body.get("tool_probe") or body.get("require_tool"))
    prompt = str(body.get("prompt") or "Reply with E2E_READY only.").strip()
    if not prompt:
        prompt = "Reply with E2E_READY only."
    # Reasoning consumes the output budget even for a one-word answer. Use
    # the saved tier's normal policy rather than a synthetic 128-token/off
    # request that working reasoning and unknown compatibility models reject.
    max_tokens = int(body.get("max_tokens") or _llm_ops._get_cfg(
        probe_config, f"llm.tiers.{tier}.max_tokens", 4096,
    ) or 4096)
    temperature = _llm_ops._get_cfg(probe_config, f"llm.tiers.{tier}.temperature")
    tools = []
    tool_choice = None
    system = "You are a readiness probe. Reply briefly and do not call tools."
    if tool_probe:
        prompt = "Call e2e_probe_tool with value E2E_TOOL_READY."
        system = (
            "You are a tool-call readiness probe. Call exactly the provided "
            "tool and do not answer in prose."
        )
        tools = [
            {
                "name": "e2e_probe_tool",
                "description": "Return readiness by calling this tool.",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]
        tool_choice = {"type": "tool", "name": "e2e_probe_tool"}
    try:
        response = LLMGateway(probe_config).call_messages(
            task=str(body.get("task") or "agent.loop"),
            caller=str(body.get("caller") or "e2e:llm_messages_probe"),
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=tools,
            tool_choice=tool_choice,
            tier=tier,
            max_tokens=max(1, min(max_tokens, 8192)),
            temperature=float(temperature if temperature is not None else 0.2),
            reasoning_effort=None,
            reasoning_summary=None,
            model_provider=model_provider or None,
            model_id=model_id or None,
            deadline=time.time() + 90,
        )
    except Exception as exc:  # noqa: BLE001 - expose provider readiness failure
        return {
            "_status": 503,
            "ok": False,
            "error": "llm_messages_probe_failed",
            "revision": revision,
            "message": f"{type(exc).__name__}: saved model connection test failed",
            "status_code": int(getattr(exc, "status_code", 0) or 0),
            "request_id": str(getattr(exc, "request_id", "") or ""),
            "failure_kind": _probe_failure_kind(exc),
            "connection_ok": False,
            "tier": tier,
        }
    if _llm_ops.config_revision(client.config) != revision:
        return {"_status": 409, "ok": False, "error": "llm_config_changed", "revision": revision}
    text = response.text()
    tool_uses = response.tool_uses()
    failure_kind = ""
    if tool_probe and not any(tool.get("name") == "e2e_probe_tool" for tool in tool_uses):
        failure_kind = "tool_not_called"
    elif not text.strip() and not tool_uses:
        failure_kind = "output_truncated" if response.stop_reason in {"max_tokens", "length", "max_output_tokens"} else "empty_response"
    return {
        **({"_status": 503, "error": "llm_messages_probe_failed", "failure_kind": failure_kind} if failure_kind else {}),
        "ok": not bool(failure_kind),
        "connection_ok": True,
        "revision": revision,
        "tier": tier,
        "provider": response.provider,
        "model": response.model,
        "provider_override": model_provider,
        "model_override": model_id,
        "stop_reason": response.stop_reason,
        "latency_ms": response.latency_ms,
        "text_preview": text[:200],
        "tool_uses_count": len(tool_uses),
        "tool_uses_preview": tool_uses[:3],
    }


def routes():
    def classify(client, payload):
        return client.llm.classify(
            prompt=payload.get("prompt") or payload["text"],
            labels=payload.get("labels") or [],
            caller=payload.get("caller", "http"),
        )

    def extract(client, payload):
        return client.llm.extract_json(
            prompt=payload["prompt"],
            schema=payload.get("schema"),
            tier=payload.get("tier", "light"),
            caller=payload.get("caller", "http"),
        )

    def capabilities(client, _payload):
        """GET /llm/capabilities — live provider support matrix. operator surface: dashboards use this to render a
        per-provider capability grid and detect ``unsupported``/
        ``metadata-only`` cells so operators never silently rely on a
        claim that isn't evidence-backed.
        """
        return client.llm.capabilities()

    # ---------------------------------------------- operator control plane
    def providers(client, _payload):
        return _llm_ops.provider_readiness(client.config)

    def tiers(client, _payload):
        return _llm_ops.tier_list(client.config)

    def catalog_endpoint(client, _payload):
        return _llm_ops.catalog(client.config)

    def config_get(client, _payload):
        return _llm_ops.llm_config(client.config)

    def config_set(client, payload):
        try:
            return _llm_ops.llm_config_set(
                client.config,
                default_tier=(payload or {}).get("default_tier"),
                intent_tier=(payload or {}).get("intent_tier"),
                providers=(payload or {}).get("providers"),
                tiers=(payload or {}).get("tiers"),
                vault_passphrase=(payload or {}).get("vault_passphrase"),
                explicit_overrides=(payload or {}).get("explicit_overrides") is True,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def models_list(client, _payload):
        return _llm_ops.models_list(client.config)

    def models_refresh(client, payload):
        return _llm_ops.models_refresh(
            client.config,
            vault_passphrase=(payload or {}).get("vault_passphrase"),
        )

    def models_discover(client, payload):
        body = payload or {}
        try:
            return _llm_ops.models_discover(
                client.config,
                provider=str(body.get("provider") or ""),
                base_url=body.get("base_url"),
                provider_key=body.get("provider_key"),
                provider_key_ref=body.get("provider_key_ref"),
                vault_passphrase=body.get("vault_passphrase"),
                # Forward the compat-shape hint so a custom (non-catalog)
                # provider id can dispatch through the right list-models
                # adapter. ``"chat_completions"`` for OpenAI-compat,
                # ``"anthropic_messages"`` for Anthropic-compat.
                api_mode=body.get("api_mode"),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def models_import(client, payload):
        body = payload or {}
        try:
            return _llm_ops.models_import(
                client.config,
                provider=str(body.get("provider") or ""),
                base_url=body.get("base_url"),
                models=body.get("models") or [],
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def models_provider_refresh(client, payload):
        body = payload or {}
        try:
            return _llm_ops.models_provider_refresh(
                client.config, provider=str(body.get("provider") or ""),
                vault_passphrase=body.get("vault_passphrase"),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def validate_assignment(client, payload):
        return _llm_ops.validate_tier_assignment(
            client.config,
            provider=str((payload or {}).get("provider") or ""),
            model=str((payload or {}).get("model") or ""),
        )

    def messages_probe(client, payload):
        return _messages_probe(client, payload)

    def routing_get(client, _payload):
        return _llm_ops.provider_routing_get(client.config)

    def routing_set(client, payload):
        payload = payload or {}
        return _llm_ops.provider_routing_set(
            client.config,
            default=payload.get("default"),
            per_provider=payload.get("per_provider"),
        )

    return [
        ("POST", "/llm/classify", classify),
        ("POST", "/llm/extract_json", extract),
        ("POST", "/llm/messages/probe", messages_probe),
        ("GET", "/llm/capabilities", capabilities),
        ("POST", "/llm/capabilities", capabilities),
        ("GET", "/llm/providers", providers),
        ("GET", "/llm/catalog", catalog_endpoint),
        ("GET", "/llm/tiers", tiers),
        ("GET", "/llm/config", config_get),
        ("POST", "/llm/config", config_set),
        ("GET", "/llm/models", models_list),
        ("POST", "/llm/models/refresh", models_refresh),
        ("POST", "/llm/models/provider-refresh", models_provider_refresh),
        ("POST", "/llm/models/discover", models_discover),
        ("POST", "/llm/models/import", models_import),
        ("POST", "/llm/models/validate", validate_assignment),
        ("GET", "/llm/provider_routing", routing_get),
        ("POST", "/llm/provider_routing", routing_set),
    ]
