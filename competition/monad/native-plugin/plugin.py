"""Public-market reads only, exposed through Nerya's normal native tool path."""
import json
import os
from pathlib import Path
import subprocess

from nerya.harness import Plugin
from nerya.tools.types import PermissionScope, RiskLevel, ToolDescriptor, ToolResult, ToolError, ToolErrorKind


class MonadMarket(Plugin):
    name = "monad_market_review"
    requires = ("paths",)

    def setup(self, ctx):
        root = Path(os.environ.get("NERYA_MONAD_SOURCE", "")).resolve()
        expected = Path(os.environ.get("NERYA_MONAD_WORKSPACE", "")).resolve()
        if not (root / "competition/monad/scripts/market.mjs").is_file() or ctx.get_service("paths").root.resolve() != expected:
            raise RuntimeError("Monad tool requires the isolated competition workspace")

        def read_market(call):
            try:
                result = subprocess.run(["node", str(root / "competition/monad/scripts/market.mjs"), json.dumps(call.arguments)],
                    capture_output=True, text=True, timeout=50,
                    env={key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if key in os.environ})
                if result.returncode:
                    raise RuntimeError("Public source failed; no cached success substituted")
                data = json.loads(result.stdout)
                return ToolResult.from_json(tool_use_id=call.id, name="monad_perpl_market", data=data, semantic_success=True)
            except Exception:
                return ToolResult.from_error(tool_use_id=call.id, name="monad_perpl_market",
                    error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message="Fresh Monad/Perpl data unavailable. No order or transaction was submitted.", retryable=True))

        props = {"symbol": {"type": "string", "enum": ["MON", "BTC", "ETH"]}}
        props.update({key: {"type": "number", "minimum": minimum} for key, minimum in [("capital", .01), ("notional", 0), ("adversePct", 0), ("maxLossPct", .01), ("slippageBps", 0)]})
        ctx.register_tool(ToolDescriptor(name="monad_perpl_market",
            description="Read current Perpl market/candles on Monad mainnet (143), verify public RPC identity, and calculate a hypothetical position loss-budget scenario including fees and assumed slippage. Public data only, no wallet, signing or orders.",
            input_schema={"type": "object", "properties": props, "additionalProperties": False}, handler=read_market,
            risk=RiskLevel.READ, permission_scope=PermissionScope.NETWORK, read_only=True, is_concurrency_safe=True,
            tags=("monad", "perpl", "research")))


PLUGIN = MonadMarket()
