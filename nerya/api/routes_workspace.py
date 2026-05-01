from __future__ import annotations


def routes():
    def workspace_info(client, _p):
        paths = client.config.paths
        return {
            "root": str(paths.root),
            "live_trading_enabled": client.config.live_trading_enabled,
            "kill_switch": client.config.kill_switch(),
        }

    return [
        ("GET", "/workspace", workspace_info),
    ]
