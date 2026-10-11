"""Pinned unified SDK binding with explicit signing and no wallet provisioning.

The connector's wire-normalization interface stays private to Nerya. SDK
private hooks are pinned to 0.12.0 and covered by real-signature contract tests.
They avoid create()'s wallet deployment and place_*()'s allowance recovery.
"""
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace

from ..core.errors import TradingError


@dataclass
class OrderArgs:
    token_id: str
    price: float
    size: float
    side: str
    expiration: int = 0


def PartialCreateOrderOptions(**kwargs):
    return SimpleNamespace(**kwargs)


def ApiCreds(*, api_key, api_secret, api_passphrase):
    @dataclass(frozen=True, repr=False)
    class Credentials:
        key: str
        secret: str
        passphrase: str
    return Credentials(api_key, api_secret, api_passphrase)


BalanceAllowanceParams = PartialCreateOrderOptions
TradeParams = PartialCreateOrderOptions
OrderPayload = PartialCreateOrderOptions
OpenOrderParams = PartialCreateOrderOptions
AssetType = SimpleNamespace(COLLATERAL="COLLATERAL", CONDITIONAL="CONDITIONAL")


def wire(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return value


class ClobClient:
    def __init__(self, *, host, chain_id, key, creds, signature_type=0, funder=None, retry_on_error=False):
        import importlib.metadata
        if importlib.metadata.version("polymarket-client") != "0.12.0":
            raise TradingError("polymarket-client 0.12.0 is required for the verified signer binding")
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from eth_utils import keccak
        from polymarket import SecureClient
        from polymarket.models import ApiKeyCreds
        from polymarket._internal.environment import PRODUCTION_CONFIG, create_environment
        from polymarket._internal.actions.orders.orders import create_unsigned_order, create_signed_order
        from polymarket._internal.actions.orders.typed_data import build_order_typed_data, build_order_signature, _build_standard_typed_data
        from polymarket._internal.wallet import signature_type_for, wrap_deposit_wallet_signature

        class DurableClient(SecureClient):
            def _sign_order(self, draft, *, post_only):
                unsigned = create_unsigned_order(draft, wallet=self._ctx.wallet, wallet_type=self._ctx.wallet_type)
                message = self._ctx.signer.sign_typed_data(full_message=build_order_typed_data(unsigned))
                signature = "0x" + message.signature.hex().removeprefix("0x")
                final = wrap_deposit_wallet_signature(signer=self._ctx.signer.address, signer_type=self._ctx.signer_type,
                                                      signature=build_order_signature(unsigned, signature))
                signed = create_signed_order(unsigned, final, post_only=post_only)
                encoded = encode_typed_data(full_message=_build_standard_typed_data(unsigned, protocol_version=unsigned.protocol_version))
                if not hasattr(self, "nerya_order_hashes"):
                    self.nerya_order_hashes = {}
                self.nerya_order_hashes[signed] = "0x" + keccak(b"\x19" + encoded.version + encoded.header + encoded.body).hex()
                return signed

        wallet = funder or Account.from_key(key).address
        env = create_environment(name="nerya", config=replace(PRODUCTION_CONFIG, clob_url=host, chain_id=chain_id))
        self.client = DurableClient._create(private_key=key, wallet=wallet, environment=env,
                                             credentials=ApiKeyCreds(key=creds.key, secret=creds.secret, passphrase=creds.passphrase),
                                             validate_credentials=False)
        if signature_type_for(self.client._ctx.wallet_type) != signature_type:
            self.client.close()
            raise TradingError("prediction signature type does not match the configured wallet")

    def create_order(self, args, options=None):
        return self.client.create_limit_order(asset_id=args.token_id, price=Decimal(str(args.price)),
                                              size=Decimal(str(args.size)), side=args.side,
                                              expiration=args.expiration or None)

    def signed_order_hash(self, signed):
        value = getattr(self.client, "nerya_order_hashes", {}).get(signed)
        if not value:
            raise TradingError("signed prediction order hash unavailable")
        return value

    @property
    def signer_address(self):
        return self.client.signer

    def post_order(self, signed, *, order_type="GTC", post_only=False):
        response = wire(self.client.post_order(replace(signed, order_type=order_type, post_only=post_only)))
        return {**response, "orderID": response.get("order_id"), "success": response.get("ok", False),
                "errorMsg": response.get("message") if response.get("ok") is False else None}

    def get_balance_allowance(self, params):
        return wire(self.client.get_balance_allowance(asset_type=params.asset_type, asset_id=getattr(params, "token_id", None)))

    def get_order(self, oid):
        return wire(self.client.get_order(order_id=oid))

    def get_open_orders(self, params=None):
        values = self.client.list_open_orders(asset_id=getattr(params, "asset_id", None), market=getattr(params, "market", None))
        return [wire(item) for item in values]

    def get_trades(self, params=None):
        return [wire(item) for item in self.client.list_account_trades(id=getattr(params, "id", None))]

    def cancel_order(self, payload):
        return wire(self.client.cancel_order(order_id=payload.orderID))

    def close(self):
        self.client.close()
