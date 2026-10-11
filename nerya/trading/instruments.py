"""Units and payoff semantics shared by quotes, fills, risk and accounting."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from ..core.errors import TradingError

InstrumentKind = Literal["spot", "linear", "inverse", "option", "prediction", "lp", "lending"]


def number(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise TradingError("invalid_financial_number")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TradingError("invalid_financial_number") from exc
    return result


def position_bucket(meta: dict | None) -> str:
    data = dict(meta or {})
    params = dict(data.get("connector_params") or {})
    bucket = str(data.get("position_side") or params.get("positionSide") or params.get("posSide") or "net").lower()
    bucket = "net" if bucket == "both" else bucket
    idx = data.get("position_idx", params.get("positionIdx"))
    if idx is not None:
        if isinstance(idx, bool) or idx not in {0, 1, 2}:
            raise TradingError("invalid_position_index")
        selected = {0: "net", 1: "long", 2: "short"}[idx]
        if bucket != "net" and selected != bucket:
            raise TradingError("conflicting_position_selectors")
        bucket = selected
    if bucket not in {"net", "long", "short"}:
        raise TradingError("invalid_position_side")
    return bucket


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    venue: str
    symbol: str
    kind: InstrumentKind
    base: str
    quote: str
    settlement: str
    contract_size: Decimal = Decimal(1)
    expiry_ms: int | None = None
    strike: Decimal | None = None
    option_type: Literal["call", "put"] | None = None
    chain_id: int | None = None
    contract_address: str | None = None
    token_id: str | None = None
    price_currency: str | None = None

    def __post_init__(self):
        if self.kind not in {"spot", "linear", "inverse", "option", "prediction", "lp", "lending"}:
            raise TradingError("unsupported_instrument_kind")
        object.__setattr__(self, "contract_size", number(self.contract_size, positive=True))
        if self.price_currency is None:
            object.__setattr__(self, "price_currency", self.settlement if self.kind == "option" else self.quote)
        if not self.instrument_id or not self.symbol or not self.quote or not self.settlement:
            raise TradingError("incomplete_instrument_identity")
        if self.kind == "option":
            if self.option_type not in {"call", "put"} or not self.expiry_ms or self.strike is None:
                raise TradingError("incomplete_option_contract")
            object.__setattr__(self, "strike", number(self.strike, positive=True))

    @classmethod
    def from_ccxt(cls, venue: str, market: dict[str, Any]) -> Instrument:
        symbol = market.get("symbol")
        if not symbol or not market.get("base") or not market.get("quote"):
            raise TradingError("market_instrument_metadata_unavailable")
        kind = "option" if market.get("option") else "inverse" if market.get("inverse") else "linear" if market.get("contract") else "spot"
        price_currency=(market.get("info") or {}).get("quote_currency") if venue.lower()=="deribit" and kind=="option" else None
        if venue.lower()=="deribit" and kind=="option" and not price_currency:raise TradingError("option_premium_currency_unavailable")
        return cls(f"{venue.lower()}:{symbol}", venue.lower(), symbol, kind, market["base"], market["quote"],
                   market.get("settle") or market["quote"], number(market.get("contractSize") or 1, positive=True),
                   market.get("expiry"), market.get("strike"), market.get("optionType"),price_currency=price_currency)

    def base_quantity(self, contracts: Any, price: Any) -> Decimal:
        quantity = number(contracts) * self.contract_size
        return quantity / number(price, positive=True) if self.kind == "inverse" else quantity

    def order_quantity(self, base_quantity: Any, price: Any) -> Decimal:
        size = number(base_quantity)
        if self.kind == "inverse":
            size *= number(price, positive=True)
        return size / self.contract_size

    def quote_value(self, contracts: Any, price: Any) -> Decimal:
        quantity = abs(number(contracts)) * self.contract_size
        return quantity if self.kind == "inverse" else quantity * number(price, positive=True)

    def pnl(self, contracts: Any, entry: Any, exit: Any) -> Decimal:
        """PnL in pnl_currency units; conversion is explicit, never assumed USD."""
        with localcontext() as ctx:
            ctx.prec = 80
            quantity = number(contracts) * self.contract_size
            before, after = number(entry, positive=True), number(exit, positive=True)
            if self.kind == "inverse":
                return quantity * (Decimal(1) / before - Decimal(1) / after)
            return quantity * (after - before)

    @property
    def pnl_currency(self):
        return self.settlement if self.kind == "inverse" else self.price_currency

    def asdict(self):
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


@dataclass(frozen=True)
class Greeks:
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal

    def __post_init__(self):
        for key in ("delta", "gamma", "vega", "theta"):
            object.__setattr__(self, key, number(getattr(self, key)))

    def scaled(self, quantity: Any) -> Greeks:
        scale = number(quantity)
        return Greeks(*(getattr(self, key) * scale for key in ("delta", "gamma", "vega", "theta")))

    def asdict(self):
        return {key: str(value) for key, value in asdict(self).items()}
