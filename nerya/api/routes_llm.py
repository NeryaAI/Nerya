from __future__ import annotations

from ..llm.gateway import LLMGateway
from ..llm import ops as _llm_ops


def _messages_probe(client, payload):
    """POST /llm/messages/probe — lightweight real messages-backend probe.

    This is intentionally separate from ``/llm/config``: config only proves a
    provider/key ref is present, while prompt E2E needs to know the selected
    provider can actually complete a provider-native messages call right now.
    """
    body = payload or {}
    tier = str(body.get("tier") or "medium").strip() or "medium"
    model_provider = str(body.get("model_provider") or body.get("provider") or "").strip()
    model_id = str(body.get("model_id") or body.get("model") or "").strip()
    tool_probe = bool(body.get("tool_probe") or body.get("require_tool"))
    prompt = str(body.get("prompt") or "Reply with E2E_READY only.").strip()
    if not prompt:
        prompt = "Reply with E2E_READY only."
    # Some reasoning-style OpenAI-compatible models emit an empty
    # ``max_tokens`` finish at 32 tokens even for the one-word readiness
    # prompt. Keep this probe lightweight, but large enough to verify the
    # real provider path instead of failing on probe truncation.
    max_tokens = int(body.get("max_tokens") or 128)
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
        response = LLMGateway(client.config).call_messages(
            task=str(body.get("task") or "agent.loop"),
            caller=str(body.get("caller") or "e2e:llm_messages_probe"),
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=tools,
            tool_choice=tool_choice,
            tier=tier,
            max_tokens=max(1, min(max_tokens, 128)),
            temperature=0.0,
            reasoning_effort="none",
            reasoning_summary=None,
            model_provider=model_provider or None,
            model_id=model_id or None,
        )
    except Exception as exc:  # noqa: BLE001 - expose provider readiness failure
        return {
            "_status": 503,
            "ok": False,
            "error": "llm_messages_probe_failed",
            "message": f"{type(exc).__name__}: {exc}",
            "status_code": int(getattr(exc, "status_code", 0) or 0),
            "request_id": str(getattr(exc, "request_id", "") or ""),
            "raw_body": str(getattr(exc, "raw_body", "") or "")[:600],
        }
    return {
        "ok": True,
        "tier": tier,
        "provider": response.provider,
        "model": response.model,
        "provider_override": model_provider,
        "model_override": model_id,
        "stop_reason": response.stop_reason,
        "latency_ms": response.latency_ms,
        "text_preview": response.text()[:200],
        "tool_uses_count": len(response.tool_uses()),
        "tool_uses_preview": response.tool_uses()[:3],
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
        ("POST", "/llm/models/discover", models_discover),
        ("POST", "/llm/models/import", models_import),
        ("POST", "/llm/models/validate", validate_assignment),
        ("GET", "/llm/provider_routing", routing_get),
        ("POST", "/llm/provider_routing", routing_set),
    ]
