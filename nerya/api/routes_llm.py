from __future__ import annotations

from ..llm import ops as _llm_ops


def routes():
    def classify(client, payload):
        return client.llm.classify(
            prompt=payload["prompt"],
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
        ("GET", "/llm/capabilities", capabilities),
        ("POST", "/llm/capabilities", capabilities),
        ("GET", "/llm/providers", providers),
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
