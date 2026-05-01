"""HMAC policy signer. Used to sign operator-issued policies
(kill switch, live trading flags)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass


@dataclass
class PolicySigner:
    secret: bytes

    def sign(self, payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hmac.new(self.secret, raw, hashlib.sha256).hexdigest()

    def verify(self, payload: dict, signature: str) -> bool:
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature)
