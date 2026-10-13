"""Rebuild checked-in SDK-only templates; no runtime or network writes."""
from pathlib import Path
from nerya.core import yaml_io
from configure import ROOT, IDS


def main():
    specs = {
        IDS[0]: ('LP range', 'lp', 'builtin:uniswap', 'wmon_nusdc', {'range_ticks': 200, 'amount0_max': '0.01', 'amount1_max': '0.01',
            'minimum_liquidity': 1, 'collect0_max': '1', 'collect1_max': '1', 'slippage_bps': 100}),
        IDS[1]: ('Perpl MON SMA6/18', 'perp', 'user:perpl:perpl', None, {'fixed_base': '100', 'slippage_bps': 50, 'stop_bps': 200, 'max_negative_pnl_bps': 100}),
        IDS[2]: ('nMEME pool momentum', 'meme', 'builtin:v3_swap', 'nmeme_wmon', {'test_token_symbol': 'nMEME', 'test_token': '', 'quote_token': '',
            'entry_amount': '0.001', 'slippage_bps': 100, 'stop_bps': 200, 'momentum_bps': 10, 'max_momentum_bps': 300,
            'min_sample_seconds': 10, 'max_sample_seconds': 300}),
    }
    for sid, (title, template, component, pool, extra) in specs.items():
        root = ROOT/sid
        root.mkdir(exist_ok=True)
        params = {'chain_id': 10143, 'chain': 'monad-testnet', 'account_id': 'monad_perpl_testnet' if template=='perp' else 'monad_demo_testnet',
            'execution_enabled': False, 'cycle_id': 'demo_1', 'component_id': component,
            'observation_source': 'monad_'+template+'_public', 'max_data_age_seconds': 30, 'max_hold_seconds': 180, **extra}
        if pool:
            params.update(wallet_id='monad_demo_testnet', protocol='uniswap_v3', pool_id=pool, pool={}, deployment_sha256='')
        manifest = {'version': 1, 'strategy_id': sid, 'title': title+' · Monad testnet', 'mode': 'paper',
            'entrypoint': 'main.py:run', 'execution_mode': 'script', 'agent_task': {'enabled': False},
            'markets': ['PERPL:MON/AUSD'] if template=='perp' else [], 'accounts': [params['account_id']],
            'schedule': {'type': 'interval', 'every_seconds': 15, 'enabled': False},
            'policy': {'max_single_order_usd': 10, 'max_daily_notional_usd': 30, 'max_open_positions': 1,
                'allow_direct_order': True, 'min_confidence': 1, 'max_run_seconds': 60},
            'llm_policy': {'default_tier': 'light', 'allowed_tiers': ['light'], 'max_calls_per_run': 1},
            'tuning': {'enabled': False}, 'news_sources': [params['observation_source']], 'params': params}
        (root/'strategy.yml').write_text(yaml_io.dumps(manifest))
        (root/'main.py').write_text((ROOT/(template+'_main.py.txt')).read_text())
        (root/'monad_runtime.py').write_text((ROOT/'runtime_template.py.txt').read_text())
        (root/'limits.yml').write_text(yaml_io.dumps({'enabled': True, 'max_single_order_usd': 10, 'max_daily_notional_usd': 30, 'max_open_positions': 1}))


if __name__ == '__main__':
    main()
