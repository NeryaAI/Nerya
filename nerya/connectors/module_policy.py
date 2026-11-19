"""Workspace operator policy for bundled on-demand providers; never writes config."""
from pathlib import Path

from ..core import yaml_io
from ..core.errors import TradingError


def module_enabled(workspace, module):
    if not module or workspace is None:
        return True
    config = yaml_io.load(Path(workspace) / 'nerya.yml', {})
    mode = config.get('modules', {}).get(module, 'on_demand')
    if mode not in ('on_demand', 'disabled'):
        raise TradingError(f'invalid module policy for {module}')
    return mode == 'on_demand'


def require_module(workspace, module):
    if not module_enabled(workspace, module):
        raise TradingError(f'module_disabled: {module}')


def require_order_modules(workspace, profile, market):
    """Check both bindings before admission; return the execution provider."""
    from .provider_spec import get_registry
    registry = get_registry(workspace)
    account_spec = registry.find(profile.venue)
    market_spec = registry.find(market.partition(':')[0]) if ':' in market else None
    for module in {spec.module for spec in (account_spec, market_spec) if spec and spec.module}:
        require_module(workspace, module)
    if not account_spec or profile.kind != account_spec.kind:
        if any(spec.kind == profile.kind and spec.order_rules for spec in registry.list_specs()):
            raise TradingError(f'provider order rules unavailable for account kind: {profile.kind}')
    return account_spec
