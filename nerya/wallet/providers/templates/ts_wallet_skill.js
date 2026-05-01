/**
 * Reference implementation for a Nerya-compatible TS/Node wallet skill.
 *
 * Nerya invokes this file with `node ts_wallet_skill.js` and writes a
 * single JSON line on stdin describing the command it wants you to run.
 * Your code is expected to write a single JSON line on stdout with the
 * result (any preceding lines are treated as debug logs and ignored).
 *
 * Wire this into your preferred TS wallet library (e.g. goat-sdk,
 * @coinbase/cdp-sdk, bitget-wallet-skill, binance-agentic-wallet) by
 * filling in the `dispatch` switch below. Do NOT install anything from
 * here — shipping dependencies is an operator decision; `package.json`
 * and `npm install` are up to them.
 *
 * Build a production copy with:
 *   tsc --outDir dist
 * and point `wallet.<provider>.skill_path` at the directory.
 */

const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", async () => {
  const raw = Buffer.concat(chunks).toString("utf-8").trim();
  let input;
  try {
    input = JSON.parse(raw || "{}");
  } catch (err) {
    process.stdout.write(JSON.stringify({
      ok: false, reason: `invalid_json_stdin: ${err.message}`,
    }) + "\n");
    process.exit(2);
  }
  try {
    const out = await dispatch(input.command, input.payload || {});
    process.stdout.write(JSON.stringify(out) + "\n");
  } catch (err) {
    process.stdout.write(JSON.stringify({
      ok: false, reason: String(err && err.stack ? err.stack : err),
    }) + "\n");
    process.exit(1);
  }
});

async function dispatch(command, payload) {
  switch (command) {
    case "balance":
      return {
        balance: 0,
        symbol: payload.token || "NATIVE",
        decimals: 18,
        note: "stub — wire your TS wallet lib here",
      };
    case "quote":
      return {
        expected_out: 0,
        min_out: 0,
        price_impact_bps: 0,
        gas_cost_usd: 0,
      };
    case "swap":
      return {
        ok: false,
        tx_hash: "",
        amount_out: 0,
        reason: "stub — implement via your TS wallet lib",
      };
    default:
      return { ok: false, reason: `unknown_command: ${command}` };
  }
}
