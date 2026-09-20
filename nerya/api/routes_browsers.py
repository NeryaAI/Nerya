"""The managed Chromium runtime is the only browser exposed by Nerya."""
from .routes_browser_desktop import routes as desktop_routes
from ..integrations.managed_browser import capabilities


def routes():
    def status(_client, _payload):
        return {'ok': True, 'selected': 'chromium', 'engines': [], 'managed': True,
                'capabilities': capabilities()}
    # Inventory and status remain read-only compatibility endpoints. Engine
    # selection, installation, uninstallation and legacy probing are retired.
    return [('GET', '/browsers/status', status), ('GET', '/browsers/registry', status)] + desktop_routes()
