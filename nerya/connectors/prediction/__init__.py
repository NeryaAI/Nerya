"""Discoverable provider metadata. Import the adapter only on use."""

def _polymarket(cfg, *, workspace=None, vault_passphrase=None):
    from ..polymarket import PolymarketConnector, CLOB_URL, GAMMA_URL, DATA_URL
    from ..registry import _resolve_cex_creds

    creds = _resolve_cex_creds(cfg, workspace, vault_passphrase,with_passphrase=True)
    options={**cfg,**dict(cfg.get('provider_config') or {})}
    return PolymarketConnector(
        credentials=creds, live=bool(cfg.get("live", False)),
        clob_url=options.get("clob_url") or CLOB_URL,
        gamma_url=options.get("gamma_url") or GAMMA_URL,
        data_url=options.get("data_url") or DATA_URL,
        workspace=workspace,account_id=str(cfg.get('id') or ''),
        signature_type=int(options.get('signature_type') or 0),funder=str(options.get('funder') or options.get('wallet_address') or ''),
        max_slippage_bps=int(options.get('max_slippage_bps',50)),
        settlement_rpc_url=str(options.get('settlement_rpc_url') or ''),
        settlement_exchange_code_hashes=dict(options.get('settlement_exchange_code_hashes') or {}),
        settlement_exchange_implementations=dict(options.get('settlement_exchange_implementations') or {}),
        settlement_confirmations=int(options.get('settlement_confirmations',3)),
    )

def _order_rules():
    from . import order_rules
    return order_rules


def prediction_spec():
    from ..provider_spec import ExchangeProviderSpec, CredentialField

    return ExchangeProviderSpec(
        id="polymarket", label="Polymarket (CLOB v2)",
        kind="prediction_market", runtime="python", module="prediction_market",
        aliases=("polymarket_v2", "pm"), factory=_polymarket, order_rules=_order_rules,
        install_hint="pip install 'polymarket-client==0.12.0' (Python >=3.11)",
        install_command="pip install 'polymarket-client==0.12.0'",
        docs_url="https://docs.polymarket.com/developers/CLOB/overview",
        links={"docs": "https://docs.polymarket.com/",
               "api": "https://docs.polymarket.com/developers/CLOB/overview"},
        description="Prediction outcome shares on Polygon, collateral pUSD. "
                    "Explicit asset or slug#outcome; share-sized limit BUY, market/limit SELL, cancel and confirmed-fill polling.",
        instrument_types=('prediction_outcome',),
        instrument_defaults={'type': 'prediction_outcome', 'quantity_unit': 'shares'},
        supports={"ticker": True, "klines": True, "order_book": True, "public_discovery": True,
                  "price_samples": True, "strategy_pnl_scope": True,
                  "balances": True, "place_order": True,'cancel_order':True,'get_order':True,
                  'public_ticker':True,'public_klines':True,'public_order_book':True,
                  'market_order':True,'limit_order':True,'market_buy':False,'market_sell':True,
                  'native_tp_sl':False,'instrument_metadata':True},
        credential_fields=(
            CredentialField(
                name="api_key", label="CLOB API Key", kind="secret",
                sensitive=True, required=True,
                description="CLOB L2 API key, not a wallet address.",
                vault_scope="exchange",
            ),
            CredentialField(
                name="api_secret", label="CLOB API Secret",
                kind="secret", required=True,
                description="CLOB L2 HMAC secret, separate from private_key.",
                vault_scope="exchange",
            ),
            CredentialField(name='api_passphrase',label='CLOB API Passphrase',kind='secret',required=True,vault_scope='exchange'),
            CredentialField(name='private_key',label='Polygon signer private key',kind='secret',required=True,vault_scope='exchange'),
            CredentialField(name='funder',label='Funder / proxy wallet',kind='public',sensitive=False,required=False),
            CredentialField(name='signature_type',label='Signature type (0/1/2/3)',kind='public',sensitive=False,required=False),
        ),
    )
