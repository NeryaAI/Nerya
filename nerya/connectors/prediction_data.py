"""Polymarket Data API v2 reads. Missing data is not a zero-valued portfolio.

Contract: https://data-api.polymarket.com/v2/docs
Cursor walks retain their wallet anchor; cursors are opaque, never offsets.
"""
from decimal import Decimal, InvalidOperation

from ..core.errors import TradingError


def nonnegative_number(value, name):
    if value is None or isinstance(value, bool):
        raise TradingError(f"prediction {name} unavailable")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TradingError(f"prediction {name} invalid") from exc
    if not number.is_finite() or number < 0:
        raise TradingError(f"prediction {name} invalid")
    return number


def portfolio_value(get, base, owner):
    document = get(base, "v2/value", params={"user": owner})
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        raise TradingError("prediction portfolio value unavailable")
    if str(data.get("proxy_wallet", "")).lower() != owner.lower():
        raise TradingError("prediction portfolio wallet mismatch")
    # The canonical portfolio endpoint also accounts for combo positions.
    # Summing the first page of /positions silently understates that exposure.
    return float(nonnegative_number(data.get("value"), "portfolio value"))


def positions(get, base, owner, *, status="OPEN", max_pages=100):
    if status not in {"OPEN", "CLOSED", "REDEEMABLE", "REDEEMABLE_LOST", "MERGEABLE"}:
        raise TradingError("invalid prediction position status")
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise TradingError("invalid prediction pagination limit")
    filters = {"user": owner, "status": status, "filter_amount": 0}
    if status != "CLOSED":
        filters["include_archived"] = "true"
    cursor = None
    seen_cursors = set()
    rows = []
    for _ in range(max_pages):
        params = {**filters, **({"cursor": cursor} if cursor else {"limit": 500})}
        document = get(base, "v2/positions", params=params)
        data = document.get("data") if isinstance(document, dict) else None
        page = document.get("pagination") if isinstance(document, dict) else None
        if not isinstance(data, list) or not isinstance(page, dict) or "next_cursor" not in page:
            raise TradingError("prediction positions pagination unavailable")
        if any(not isinstance(row, dict) or str(row.get("proxy_wallet", "")).lower() != owner.lower() for row in data):
            raise TradingError("prediction position wallet mismatch")
        rows.extend(data)
        cursor = page["next_cursor"]
        if cursor is None:
            if page.get("has_more") is True:
                raise TradingError("prediction positions pagination inconsistent")
            return rows
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors or page.get("has_more") is False:
            raise TradingError("prediction positions pagination inconsistent")
        seen_cursors.add(cursor)
    raise TradingError("prediction positions incomplete: pagination limit reached")
