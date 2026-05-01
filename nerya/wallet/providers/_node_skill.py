"""Shared helper for wallet providers backed by a Node/TypeScript skill.

Bitget, Binance-Web3 agentic wallet, and the Coinbase CDP TS SDK all ship
as Node modules. We invoke them via ``node <entry>`` with a JSON command
on stdin and a JSON response on stdout. Nothing is installed for the
operator — if the skill directory is absent we raise
:class:`WalletDependencyError` with the exact ``git clone`` / ``npm
install`` line they should run.

Wire protocol (identical for every TS wallet):

    # --- stdin (JSON, one line) ---
    {"command": "balance",
     "payload": {"chain": "bsc", "address": "0xabc...", "token": ""}}

    # --- stdout (last JSON line) ---
    {"balance": 1.2345, "symbol": "BNB", "decimals": 18}

Supported commands (every provider should implement at least these):

* ``balance``  → {"balance": float, "symbol": str, "decimals": int}
* ``quote``    → {"expected_out": float, "min_out": float,
                  "price_impact_bps": int, "gas_cost_usd": float,
                  "tx_unsigned": any}
* ``swap``     → {"ok": bool, "tx_hash": str, "amount_out": float,
                  "reason": str}

Minimal skill template (``dist/index.js``)::

    const chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", async () => {
      const { command, payload } = JSON.parse(Buffer.concat(chunks).toString());
      try {
        const out = await dispatch(command, payload);  // your SDK calls
        process.stdout.write(JSON.stringify(out) + "\\n");
      } catch (err) {
        process.stdout.write(JSON.stringify({ok: false, reason: String(err)}) + "\\n");
        process.exit(1);
      }
    });

Nerya only parses the **last line** of stdout as JSON so operators can
``console.log`` debug info freely above the result without breaking the
integration.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import WalletDependencyError, WalletPolicyDenied


@dataclass
class NodeSkillRef:
    """Describes an external Node skill we can invoke as a subprocess."""

    id: str
    label: str
    repo: str            # https clone URL for install hint
    entry: str           # default entry path inside the skill directory
    package: str         # npm package (falls back to node if absent)
    skill_path: str = "" # where the user cloned it

    def install_hint(self) -> str:
        pieces = []
        pieces.append("1. install node 20+: https://nodejs.org/ (`node --version`)")
        pieces.append(
            f"2. clone the skill: `git clone {self.repo} <skills-dir>/{self.id}`"
        )
        pieces.append(
            f"3. install deps inside that directory: `npm install`"
        )
        pieces.append(
            f"4. set `wallet.{self.id}.skill_path` in nerya.yml to the "
            f"absolute path."
        )
        return " ".join(pieces)

    def node_available(self) -> bool:
        return shutil.which("node") is not None

    def skill_ready(self) -> tuple[bool, list[str]]:
        missing: list[str] = []
        if not self.node_available():
            missing.append("bin:node")
        p = Path(self.skill_path) if self.skill_path else None
        if not p or not p.exists():
            missing.append(f"skill:{self.repo}")
        else:
            if not (p / "node_modules").exists():
                missing.append("skill:node_modules (run `npm install` in the skill dir)")
            if not (p / self.entry).exists():
                missing.append(f"skill:entry({self.entry})")
        return (len(missing) == 0), missing

    def invoke(self, command: str, payload: dict[str, Any],
               *, timeout_s: float = 25.0) -> dict[str, Any]:
        ok, missing = self.skill_ready()
        if not ok:
            raise WalletDependencyError(self.id, missing, self.install_hint())

        entry = Path(self.skill_path) / self.entry
        proc_input = json.dumps({
            "command": command,
            "payload": payload or {},
        })
        try:
            r = subprocess.run(
                ["node", str(entry)],
                input=proc_input.encode("utf-8"),
                cwd=self.skill_path,
                env=os.environ.copy(),
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WalletDependencyError(
                self.id, ["bin:node"],
                "install node: https://nodejs.org/",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WalletPolicyDenied(
                f"{self.id} skill timed out after {timeout_s}s"
            ) from exc
        if r.returncode != 0:
            stderr = (r.stderr or b"").decode("utf-8", "replace").strip()
            raise WalletPolicyDenied(
                f"{self.id} skill exited with code {r.returncode}: {stderr[:512]}"
            )
        stdout = (r.stdout or b"").decode("utf-8", "replace").strip()
        if not stdout:
            return {}
        try:
            doc = json.loads(stdout.splitlines()[-1])
        except Exception as exc:
            raise WalletPolicyDenied(
                f"{self.id} skill returned invalid JSON: {stdout[:256]}"
            ) from exc
        if isinstance(doc, dict):
            return doc
        return {"result": doc}
