"""Protocol market identity used by authoring and static validation.

Only structured market segments select a contract, never strategy prose or
the AAVE spot token (e.g. BINANCE:AAVE/USDT).
"""

import ast
from pathlib import Path

from ..core.errors import TradingError


class IncompleteStrategyError(TradingError):
    """Producer evidence that source is still an unfinished template."""

    def __init__(self, path: str, template: str, detail: str = ''):
        self.path, self.template = path, template
        super().__init__(f"strategy_implementation_incomplete: {path} still matches {template}. "
                         "Implement the requested inputs and decisions in this same candidate, then validate. "
                         "Scaffold checks do not establish strategy implementation or replay readiness."
                         + (f' {detail}' if detail else ''))


def incomplete_scaffold(source: str) -> str | None:
    """Exact code identity ignoring formatting/comments; absence is not completion.

    Only host templates marked incomplete participate. Candidate prose/markers
    and a deliberately authored hold strategy never establish a match.
    """
    try:
        candidate = ast.dump(ast.parse(source), include_attributes=False)
    except SyntaxError:
        return None
    root = Path(__file__).parents[1] / 'skills/builtin/strategy_author/templates'
    for path in sorted(root.glob('*.py.j2')):
        template = path.read_text(encoding='utf-8')
        if '# nerya-template: implementation-incomplete' not in template.splitlines():
            continue
        if candidate == ast.dump(ast.parse(template), include_attributes=False):
            return 'templates/' + path.name
    return None


def require_implemented_candidate(package) -> None:
    path = package.manifest.entrypoint_module
    source = (package.root / path).read_text(encoding='utf-8')
    template = incomplete_scaffold(source)
    if template:
        raise IncompleteStrategyError(path, template)
    lp_template = 'skills/builtin/strategy_author/templates/lp_range_strategy.py.j2'
    builtin = Path(__file__).parents[1] / lp_template
    try:
        if ast.dump(ast.parse(source)) != ast.dump(ast.parse(builtin.read_text(encoding='utf-8'))):
            return
    except SyntaxError:
        return  # The normal syntax validator owns this diagnostic.
    gaps = _lp_configuration_gaps(package.manifest.extras.get('params'))
    if gaps:
        raise IncompleteStrategyError(path, 'templates/lp_range_strategy.py.j2',
            'Complete saved configuration: ' + ', '.join(gaps) + '. '
            'This is configuration structure only, not historical coverage or live readiness; '
            'paper validation does not require live credentials.')


def _lp_configuration_gaps(params) -> list[str]:
    """Required saved shape only; policy values/resource readiness stay runtime-owned."""
    gaps = []

    def fields(value, keys, prefix, *, nullable=()):
        if not isinstance(value, dict):
            gaps.append(prefix + ' (mapping required)')
            return {}
        for key in keys.split():
            item = value.get(key)
            if key not in value or (key not in nullable and (item is None or item == ''
                    or isinstance(item, str) and not item.strip())):
                gaps.append(prefix + '.' + key)
        return value

    params = fields(params, 'lp_policy lp_source lp_execution', 'params')
    fields(params.get('lp_policy'), 'max_age_seconds persistence_seconds cooldown_seconds '
           'cost_multiple enter_when_empty exit_below_principal_usd collect_min_fees_usd', 'params.lp_policy')
    source = fields(params.get('lp_source'), 'source_id wallet_id chain protocol pool_id token_id '
                    'token0 token1 decimals fee tick_spacing component_binding account_request',
                    'params.lp_source', nullable=('token_id',))
    fields(source.get('component_binding'), 'id revision abi_version', 'params.lp_source.component_binding')
    execution = fields(params.get('lp_execution'), 'operation_key', 'params.lp_execution')

    def request(value, prefix):
        value = fields(value, 'kind component_id wallet_id chain protocol parameters', prefix)
        fields(value.get('parameters'), 'pool_id', prefix + '.parameters')

    request(source.get('account_request'), 'params.lp_source.account_request')
    requests, plan = execution.get('requests'), execution.get('rebalance_plan')
    if requests is not None:
        if not isinstance(requests, dict) or not requests:
            gaps.append('params.lp_execution.requests (nonempty mapping required)')
        else:
            for action, value in requests.items():
                request(value, 'params.lp_execution.requests.' + str(action))
    if plan is not None:
        plan = fields(plan, 'exit entry', 'params.lp_execution.rebalance_plan')
        for leg in ('exit', 'entry'):
            request(plan.get(leg), 'params.lp_execution.rebalance_plan.' + leg)
    if not requests and not plan:
        gaps.append('params.lp_execution.requests or rebalance_plan')
    return gaps


def is_aave_market(market: str) -> bool:
    return any(
        segment.strip().lower().replace('_', '').replace('-', '') in {'aave', 'aavev3'}
        for segment in market.split(':')[:-1]
    )
