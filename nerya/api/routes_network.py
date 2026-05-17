from __future__ import annotations

from ..core.proxy import (
    apply_network_proxy,
    public_proxy_status,
    save_proxy_config,
    test_proxy_request,
)
from ..core.dashboard import (
    public_dashboard_config,
    save_dashboard_config,
)
from ..core.tunnels import (
    install_provider,
    launch_tunnel_restore_on_start,
    public_tunnel_status,
    save_tunnel_config,
    start_tunnel,
    stop_tunnel,
)


def launch_configured_tunnels_on_start(client) -> dict:
    return launch_tunnel_restore_on_start(client.config)


def routes():
    def proxy_get(client, _payload):
        return public_proxy_status(client.config)

    def proxy_set(client, payload):
        try:
            result = save_proxy_config(
                client.config,
                payload or {},
                vault_passphrase=(payload or {}).get("vault_passphrase"),
            )
            applied = apply_network_proxy(client.config)
            result["applied"] = {
                **applied,
                "env": result.get("applied", {}).get("env", {}),
            }
            return public_proxy_status(client.config)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_proxy_config", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "proxy_config_failed", "detail": str(exc)}

    def proxy_test(client, payload):
        return test_proxy_request(
            client.config,
            url=str((payload or {}).get("url") or "https://httpbin.org/ip"),
        )

    def dashboard_get(client, _payload):
        return public_dashboard_config(client.config)

    def dashboard_set(client, payload):
        try:
            return save_dashboard_config(client.config, payload or {})
        except ValueError as exc:
            return {"ok": False, "error": "invalid_dashboard_config", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "dashboard_config_failed", "detail": str(exc)}

    def tunnels_get(client, _payload):
        return public_tunnel_status(client.config)

    def tunnels_config(client, payload):
        return save_tunnel_config(client.config, payload or {})

    def tunnels_install(client, payload):
        body = payload or {}
        return install_provider(
            client.config,
            str(body.get("provider") or ""),
            approve=bool(body.get("approve")),
        )

    def tunnels_start(client, payload):
        return start_tunnel(client.config, str((payload or {}).get("provider") or ""))

    def tunnels_stop(client, payload):
        return stop_tunnel(client.config, str((payload or {}).get("provider") or ""))

    return [
        ("GET", "/network/proxy", proxy_get),
        ("POST", "/network/proxy", proxy_set),
        ("POST", "/network/proxy/test", proxy_test),
        ("GET", "/network/dashboard", dashboard_get),
        ("POST", "/network/dashboard", dashboard_set),
        ("GET", "/network/tunnels", tunnels_get),
        ("POST", "/network/tunnels/config", tunnels_config),
        ("POST", "/network/tunnels/install", tunnels_install),
        ("POST", "/network/tunnels/start", tunnels_start),
        ("POST", "/network/tunnels/stop", tunnels_stop),
    ]
