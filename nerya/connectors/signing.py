"""HMAC signing helpers shared by CEX connectors.

With ccxt now handling all exchange-specific REST + signing, Nerya's
remaining hand-rolled signer is OKX — still used by the ``okx_os``
wallet provider to hit `/api/v5/wallet` endpoints that ccxt doesn't
cover. Binance/Bybit/Hyperliquid signing used to live here too and
has been removed in favour of ``CcxtConnector``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


# ---------------------------------------------------------------- OKX
def okx_sign(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    *,
    method: str,
    path: str,
    body: Any | None = None,
    now_iso: str | None = None,
) -> tuple[dict[str, str], str | None]:
    """Sign OKX REST request.

    Returns (headers, body_json_or_None).

    Reference: https://www.okx.com/docs-v5/en/#overview-rest-authentication
    """
    from datetime import datetime, timezone

    ts = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}Z"
    body_s: str | None = None
    if method.upper() != "GET" and body is not None:
        body_s = json.dumps(body, separators=(",", ":"))
    payload = (ts + method.upper() + path + (body_s or "")).encode("utf-8")
    sig = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": api_passphrase,
        "Content-Type": "application/json",
    }
    return headers, body_s


__all__ = ["okx_sign"]
