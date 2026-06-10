/**
 * Playwright globalSetup — runs ONCE before any test in this run.
 *
 *  1) Ensures NERYA_WORKSPACE is set to an *isolated* test workspace
 *     (under ./.nerya-test-workspace by default), so production data
 *     is never touched.
 *  2) Resets that workspace by invoking tools/reset_workspace.py,
 *     including agent-authored memory so CSV cases start from a clean slate.
 *  3) Sanity-pings the runtime API and dashboard URL.
 *
 * Required env (optional, all have safe defaults):
 *   NERYA_WORKSPACE                — override isolated workspace path
 *   NERYA_API                      — runtime URL (default http://127.0.0.1:18318)
 *   NERYA_DASHBOARD_URL            — dashboard URL (default http://127.0.0.1:3001)
 *   NERYA_TEST_RESET=full          — pass --full to reset_workspace (CAUTION)
 *   NERYA_TEST_SKIP_RESET=1        — skip reset (useful for local debugging)
 *   NERYA_PERMISSION_MODE=yolo     — default for E2E; use default to test approvals
 *   NERYA_E2E_ALLOW_WORKSPACE_MISMATCH=1
 *                                   — explicitly allow wrong runtime workspace
 *   NERYA_E2E_ALLOW_MOCK_LLM=1     — explicitly allow mock LLM tiers
 *   NERYA_E2E_SKIP_LLM_PROBE=1     — skip live provider readiness probe
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, existsSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const DEFAULT_RUNTIME_API = "http://127.0.0.1:18318";

function defaultTestWorkspace(): string {
  const here = resolve(__dirname, "..", "..");
  return join(here, ".nerya-test-workspace");
}

async function pingUrl(url: string, label: string, timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(3_000) });
      if (res.ok || res.status === 404) {
        console.log(`[setup] ${label} reachable: ${url} (${res.status})`);
        return;
      }
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 1_000));
  }
  throw new Error(
    `[setup] ${label} not reachable at ${url} after ${timeoutMs} ms. ` +
      `Start it first (see tests/e2e/README.md).`,
  );
}

export default async function globalSetup() {
  process.env.NERYA_PERMISSION_MODE =
    process.env.NERYA_PERMISSION_MODE?.trim() || "yolo";
  process.env.NEXT_PUBLIC_NERYA_PERMISSION_MODE =
    process.env.NEXT_PUBLIC_NERYA_PERMISSION_MODE?.trim() ||
    process.env.NERYA_PERMISSION_MODE;
  const workspace =
    process.env.NERYA_WORKSPACE && process.env.NERYA_WORKSPACE.trim()
      ? process.env.NERYA_WORKSPACE
      : defaultTestWorkspace();
  process.env.NERYA_WORKSPACE = workspace;

  mkdirSync(workspace, { recursive: true });
  const marker = join(workspace, "nerya.yml");
  if (!existsSync(marker)) {
    writeFileSync(
      marker,
      "# Playwright test workspace — auto-created by global-setup.ts\n" +
        "runtime:\n  live_trading_enabled: false\n",
    );
  }

  if (process.env.NERYA_TEST_SKIP_RESET === "1") {
    console.log("[setup] NERYA_TEST_SKIP_RESET=1, skipping workspace reset");
  } else {
    const repoRoot = resolve(__dirname, "..", "..", "..", "..");
    const resetScript = join(repoRoot, "Nerya", "tools", "reset_workspace.py");
    const fullArg = process.env.NERYA_TEST_RESET === "full" ? ["--full"] : [];
    const args = [
      resetScript,
      "--workspace",
      workspace,
      ...fullArg,
      "--clear-memory",
      "--sync-prompt-bundle",
      "--log",
      join(workspace, ".reset-log.json"),
    ];
    console.log(`[setup] python ${args.join(" ")}`);
    const r = spawnSync("python", args, { stdio: "inherit" });
    if (r.status !== 0) {
      throw new Error(
        `[setup] reset_workspace.py exited ${r.status}. Aborting test run.`,
      );
    }
  }
  ensureApprovedScriptFixtures(workspace);

  const api = process.env.NERYA_API ?? DEFAULT_RUNTIME_API;
  const uiPort = process.env.NERYA_DASHBOARD_PORT ?? process.env.PORT ?? "3001";
  const ui = process.env.NERYA_DASHBOARD_URL ?? `http://127.0.0.1:${uiPort}`;
  await pingUrl(`${api}/health`, "runtime", 60_000).catch((e) => {
    console.warn(String(e));
  });
  await pingUrl(`${ui}/`, "dashboard", 60_000).catch((e) => {
    console.warn(String(e));
  });

  // Hard readiness gates. A green GET /health is NOT enough: the dashboard's
  // Next.js proxy POST route can be 500-broken (corrupted .next) while GETs
  // still pass, and the runtime can be bound to the WRONG workspace. Both
  // produced multi-hour opaque-timeout runs before (see tests/e2e/notes.md
  // RC1/RC2/RC6). Catch them here and fail loud BEFORE 100+ cases run.
  await assertProxyPostHealthy(ui);
  const proxyApi = `${ui.replace(/\/$/, "")}/api/proxy`;
  await assertRuntimeWorkspace(api, workspace, "runtime API");
  await assertRuntimeWorkspace(proxyApi, workspace, "dashboard proxy");
  await assertPromptLlmIsNotMock(api, "runtime API");
  await assertPromptLlmIsNotMock(proxyApi, "dashboard proxy");
  await assertPromptLlmProbe(api, "runtime API");
  if (process.env.NERYA_E2E_PROBE_PROXY_LLM === "1") {
    await assertPromptLlmProbe(proxyApi, "dashboard proxy");
  } else {
    console.log(
      "[setup] dashboard proxy duplicate LLM probe skipped " +
        "(set NERYA_E2E_PROBE_PROXY_LLM=1 to force it)",
    );
  }
  await warnDevLogBloat(api);

  console.log(`[setup] workspace ready: ${workspace}`);
  console.log(`[setup] api=${api}  ui=${ui}`);
  console.log(
    `[setup] permission_mode=${process.env.NERYA_PERMISSION_MODE ?? "default"}`,
  );
}

function ensureApprovedScriptFixtures(workspace: string): void {
  const scriptDir = join(
    workspace,
    "scripts",
    "approved",
    "eth_btc_ratio_chart",
  );
  mkdirSync(scriptDir, { recursive: true });
  writeFileSync(
    join(scriptDir, "manifest.yml"),
    [
      "id: eth_btc_ratio_chart",
      "version: 0.1.0",
      "title: ETH/BTC Ratio Chart",
      "description: Approved E2E fixture script for ETH/BTC ratio reporting.",
      "entry: run",
      "state: approved",
      "",
    ].join("\n"),
    "utf8",
  );
  writeFileSync(
    join(scriptDir, "eth_btc_ratio_chart.py"),
    [
      "def run(days=7, output='summary'):",
      "    days = int(days or 7)",
      "    return {",
      "        'ok': True,",
      "        'script_id': 'eth_btc_ratio_chart',",
      "        'days': days,",
      "        'summary': f'ETH/BTC ratio chart fixture for {days} days',",
      "        'output': output,",
      "    }",
      "",
    ].join("\n"),
    "utf8",
  );
}

/**
 * The dashboard proxies chat turns via `POST /api/proxy/agent/run_turn`. A
 * corrupted `.next` build makes that route 500 in ~200ms while every GET proxy
 * still returns 200 — the exact signature behind the logged run.
 *
 * We probe a cheap POST proxy route (`agent/turn_state`). NOTE the runtime
 * *legitimately* answers that probe with a structured JSON error (the bogus
 * turn_id has no journal), so status code alone is NOT a health signal. We
 * classify by ORIGIN instead:
 *   - parseable JSON whose `error` is NOT a proxy-origin label  → runtime
 *     answered → proxy HEALTHY (even on a runtime 4xx/5xx).
 *   - `error` ∈ {upstream_unreachable, proxy_handler_error}     → proxy/upstream
 *     broken → abort.
 *   - non-JSON body with status ≥ 500                            → Next.js
 *     framework 500 (corrupted `.next`) → abort.
 */
const PROXY_ORIGIN_ERRORS = new Set(["upstream_unreachable", "proxy_handler_error"]);

async function assertProxyPostHealthy(ui: string): Promise<void> {
  const url = `${ui.replace(/\/$/, "")}/api/proxy/agent/turn_state`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ turn_id: "__setup_probe__" }),
      signal: AbortSignal.timeout(15_000),
    });
  } catch (e) {
    throw new Error(
      `[setup] dashboard POST proxy probe failed: ${url} (${String(e)}). ` +
        `Is the dashboard up on ${ui}?`,
    );
  }
  const body = await res.text().catch(() => "");
  let parsed: { error?: unknown } | null = null;
  try {
    parsed = body ? (JSON.parse(body) as { error?: unknown }) : null;
  } catch {
    parsed = null;
  }
  const errLabel = parsed && typeof parsed.error === "string" ? parsed.error : "";
  const brokenProxy = PROXY_ORIGIN_ERRORS.has(errLabel);
  const frameworkError = !parsed && res.status >= 500;
  if (brokenProxy || frameworkError) {
    throw new Error(
      `[setup] dashboard POST proxy is BROKEN: ${url} -> ${res.status}` +
        (errLabel ? ` (${errLabel})` : " (non-JSON framework error)") +
        `. This is the corrupted-.next signature (GET proxies pass, POST fails). ` +
        `Fix: stop the dashboard, 'npm run clean:next', restart with ` +
        `'npm run dev:e2e', then re-run.\n  body: ${body.slice(0, 300)}`,
    );
  }
  console.log(
    `[setup] proxy POST healthy: ${url} -> ${res.status} ` +
      `(runtime answered${errLabel ? ` with error=${errLabel}` : ""})`,
  );
}

/**
 * Verify the *running* runtime is actually serving the isolated test workspace.
 * The runtime is started separately, so `process.env.NERYA_WORKSPACE` never
 * reaches it; nothing else asserts the binding. `/dev/status` returns the
 * dev-log dir (`<root>/dev_logs`), from which we recover the root.
 *
 * Default: hard-fail so tests never silently pollute the real ~/.nerya
 * workspace. Set NERYA_E2E_ALLOW_WORKSPACE_MISMATCH=1 only for manual
 * debugging when you intentionally point tests at a non-isolated runtime.
 */
async function assertRuntimeWorkspace(
  api: string,
  workspace: string,
  label: string,
): Promise<void> {
  const norm = (p: string) =>
    p.replace(/[\\/]+/g, "/").replace(/\/+$/, "").toLowerCase();
  let runtimeRoot = "";
  try {
    const res = await fetch(`${api}/dev/status`, { signal: AbortSignal.timeout(10_000) });
    if (res.ok) {
      const j = (await res.json()) as { dir?: string };
      if (j?.dir) runtimeRoot = j.dir.replace(/[\\/]+dev_logs\/?$/i, "");
    }
  } catch {
    /* /dev/status may be disabled; skip the assertion rather than block */
  }
  if (!runtimeRoot) {
    console.warn(
      `[setup] could not determine ${label} workspace (/dev/status unavailable); ` +
        "skipping isolation check.",
    );
    return;
  }
  if (norm(runtimeRoot) === norm(workspace)) {
    console.log(`[setup] ${label} workspace OK: ${runtimeRoot}`);
    return;
  }
  const msg =
    `[setup] ${label.toUpperCase()} WORKSPACE MISMATCH — tests will hit the wrong data!\n` +
    `  checked url   : ${api}/dev/status\n` +
    `  runtime root : ${runtimeRoot}\n` +
    `  expected     : ${workspace}\n` +
    `  The runtime was started against a different workspace, so reset_workspace.py ` +
    `cleans an unused dir and tests read/write real data. Restart the runtime with ` +
    `NERYA_WORKSPACE="${workspace}" (or set the workspace in its launch command).`;
  if (process.env.NERYA_E2E_ALLOW_WORKSPACE_MISMATCH === "1") {
    console.warn(msg);
    return;
  }
  throw new Error(msg);
}

/**
 * Prompt E2E cases are meant to exercise the real `/agent/run_turn` path
 * against the configured LLM provider. If the runtime workspace is still on
 * the default `provider: mock` tiers, the UI returns `[mock] you said: ...`
 * and every prompt assertion becomes meaningless. Fail before the suite runs.
 */
async function assertPromptLlmIsNotMock(api: string, label: string): Promise<void> {
  if (process.env.NERYA_E2E_ALLOW_MOCK_LLM === "1") {
    console.warn(
      "[setup] NERYA_E2E_ALLOW_MOCK_LLM=1, allowing mock LLM tiers for this run",
    );
    return;
  }
  type LlmTier = {
    tier?: string;
    provider?: string;
    model?: string;
    has_key_ref?: boolean;
    base_url?: string;
  };
  type LlmConfig = {
    default_tier?: string;
    tiers?: LlmTier[];
  };
  let cfg: LlmConfig;
  try {
    const res = await fetch(`${api}/llm/config`, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) {
      throw new Error(`${res.status} ${await res.text().catch(() => "")}`);
    }
    cfg = (await res.json()) as LlmConfig;
  } catch (e) {
    throw new Error(
      `[setup] could not verify LLM config via ${api}/llm/config (${String(e)}). ` +
        "Prompt E2E needs a configured runtime LLM provider.",
    );
  }

  const tiers = cfg.tiers ?? [];
  const defaultTierName = String(cfg.default_tier || "medium");
  const selected =
    tiers.find((t) => t.tier === "medium") ??
    tiers.find((t) => t.tier === defaultTierName) ??
    tiers[0];
  const provider = String(selected?.provider || "mock").trim().toLowerCase();
  if (!selected || provider === "mock") {
    const rows = tiers
      .map((t) => `${t.tier || "?"}=${t.provider || "mock"}/${t.model || ""}`)
      .join(", ");
    throw new Error(
      "[setup] E2E runtime is configured with a mock LLM tier, so chat replies " +
        "will be `[mock] you said: ...` instead of real provider output.\n" +
        `  checked      : ${label}\n` +
        `  api          : ${api}\n` +
        `  default_tier : ${defaultTierName}\n` +
        `  tiers        : ${rows || "(none)"}\n` +
        "Fix: prepare/start the runtime with the isolated workspace that has real " +
        "LLM provider config, e.g. run tools/prepare_isolated_test_workspace.py " +
        "and start `nerya serve --workspace dashboard/.nerya-test-workspace ...`. " +
      "If this is an intentional offline mock run, set NERYA_E2E_ALLOW_MOCK_LLM=1.",
    );
  }
  const expectedBaseUrl = (process.env.NERYA_E2E_LLM_BASE_URL || "").replace(/\/+$/, "");
  const actualBaseUrl = String(selected.base_url || "").replace(/\/+$/, "");
  if (expectedBaseUrl && actualBaseUrl !== expectedBaseUrl) {
    throw new Error(
      `[setup] ${label} LLM base URL mismatch for prompt E2E.\n` +
        `  expected: ${expectedBaseUrl}\n` +
        `  actual  : ${actualBaseUrl || "(missing)"}\n` +
        "Fix the isolated workspace provider config before running real-provider cases.",
    );
  }
  console.log(
    `[setup] ${label} LLM tier ready for prompt E2E: ${selected.tier || defaultTierName} ` +
      `provider=${provider} model=${selected.model || "(default)"}` +
      (actualBaseUrl ? ` base_url=${actualBaseUrl}` : "") +
      (selected.has_key_ref ? " key_ref=yes" : ""),
  );
}

type LlmProbeBody = {
  ok?: boolean;
  provider?: string;
  model?: string;
  error?: string;
  message?: string;
  stop_reason?: string;
  text_preview?: string;
  tool_uses_count?: number;
  tool_uses_preview?: { name?: string; input?: { value?: string } }[];
  status_code?: number;
  raw_body?: string;
};

function isTransientLlmProbeFailure(
  parsed: LlmProbeBody | null,
  body: string,
  status: number,
): boolean {
  const providerStatus = Number(parsed?.status_code || 0);
  const text = [
    parsed?.error,
    parsed?.message,
    parsed?.raw_body,
    body,
  ].join(" ").toLowerCase();
  return (
    status === 429 ||
    providerStatus === 429 ||
    ([500, 502, 503, 504].includes(status) &&
      /(rate.?limit|tpm|too many requests|timeout|timed out|temporar|overloaded)/i.test(text))
  );
}

function llmProbeRetryDelayMs(
  parsed: LlmProbeBody | null,
  body: string,
  status: number,
  attempt: number,
): number {
  if (!isTransientLlmProbeFailure(parsed, body, status)) {
    return 750;
  }
  return Math.min(30_000, 5_000 * Math.max(1, attempt - 1));
}

/**
 * `/llm/config` only proves that a non-mock provider is configured. It does not
 * prove the key, quota, provider endpoint, or messages adapter works right now.
 * Prompt E2E eventually reaches the same provider-native messages backend via
 * `/agent/run_turn`; this cheap no-tool probe catches 401/429/5xx failures
 * before the first chat case burns its whole timeout.
 */
async function assertPromptLlmProbe(api: string, label: string): Promise<void> {
  if (process.env.NERYA_E2E_ALLOW_MOCK_LLM === "1") {
    return;
  }
  if (process.env.NERYA_E2E_SKIP_LLM_PROBE === "1") {
    console.warn("[setup] NERYA_E2E_SKIP_LLM_PROBE=1, skipping live LLM probe");
    return;
  }
  const url = `${api.replace(/\/$/, "")}/llm/messages/probe`;
  let res: Response;
  let body = "";
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        tier: "medium",
        prompt: "Reply with E2E_READY only.",
        max_tokens: 128,
      }),
      signal: AbortSignal.timeout(60_000),
    });
    body = await res.text().catch(() => "");
  } catch (e) {
    body = JSON.stringify({
      ok: false,
      error: String(e),
      message: String(e),
      status_code: 503,
    });
    res = new Response(body, { status: 503 });
  }
  let parsed: LlmProbeBody | null = null;
  try {
    parsed = body ? JSON.parse(body) : null;
  } catch {
    parsed = null;
  }
  let provider = String(parsed?.provider || "").trim().toLowerCase();
  let textPreview = String(parsed?.text_preview || "").trim();
  let probeTextOk = /\bE2E_READY\b/i.test(textPreview);
  for (let attempt = 2; (!res.ok || !parsed?.ok || provider === "mock" || !probeTextOk) && attempt <= 6; attempt++) {
    const delayMs = llmProbeRetryDelayMs(parsed, body, res.status, attempt);
    if (delayMs > 1_000) {
      console.warn(
        `[setup] ${label} live LLM probe transient failure; retrying in ` +
          `${Math.round(delayMs / 1000)}s (attempt ${attempt}/6)`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        tier: "medium",
        prompt: "Reply with E2E_READY only.",
        max_tokens: 128,
      }),
      signal: AbortSignal.timeout(60_000),
    });
    body = await res.text().catch(() => "");
    try {
      parsed = body ? JSON.parse(body) : null;
    } catch {
      parsed = null;
    }
    provider = String(parsed?.provider || "").trim().toLowerCase();
    textPreview = String(parsed?.text_preview || "").trim();
    probeTextOk = /\bE2E_READY\b/i.test(textPreview);
  }
  if (!res.ok || !parsed?.ok || provider === "mock" || !probeTextOk) {
    throw new Error(
      `[setup] ${label} live LLM probe failed: ${url} -> ${res.status}\n` +
        `  provider    : ${provider || "(unknown)"}\n` +
        `  model       : ${parsed?.model || "(unknown)"}\n` +
        `  stop_reason : ${parsed?.stop_reason || "(unknown)"}\n` +
        `  text_preview: ${textPreview || "(empty)"}\n` +
        `  error       : ${parsed?.error || "(none)"}\n` +
        `  message     : ${parsed?.message || body.slice(0, 300)}\n` +
        `  status_code : ${parsed?.status_code || 0}\n` +
        `  raw_body    : ${parsed?.raw_body || ""}\n` +
        "Fix the isolated workspace LLM config/provider quota/context window before running prompt E2E.",
    );
  }
  console.log(
    `[setup] ${label} live LLM probe OK: provider=${provider} ` +
      `model=${parsed.model || "(default)"}`,
  );

  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        tier: "medium",
        tool_probe: true,
        max_tokens: 128,
      }),
      signal: AbortSignal.timeout(60_000),
    });
    body = await res.text().catch(() => "");
    parsed = body ? (JSON.parse(body) as typeof parsed) : null;
  } catch (e) {
    body = JSON.stringify({
      ok: false,
      error: String(e),
      message: String(e),
      status_code: 503,
    });
    res = new Response(body, { status: 503 });
    parsed = JSON.parse(body) as typeof parsed;
  }
  let toolCount = Number(parsed?.tool_uses_count || 0);
  let firstTool = parsed?.tool_uses_preview?.[0];
  let toolValue = String(firstTool?.input?.value || "");
  for (let attempt = 2; (!res.ok || !parsed?.ok || provider === "mock" || toolCount < 1 || toolValue !== "E2E_TOOL_READY") && attempt <= 6; attempt++) {
    const delayMs = llmProbeRetryDelayMs(parsed, body, res.status, attempt);
    if (delayMs > 1_000) {
      console.warn(
        `[setup] ${label} live LLM tool probe transient failure; retrying in ` +
          `${Math.round(delayMs / 1000)}s (attempt ${attempt}/6)`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        tier: "medium",
        tool_probe: true,
        max_tokens: 128,
      }),
      signal: AbortSignal.timeout(60_000),
    });
    body = await res.text().catch(() => "");
    try {
      parsed = body ? (JSON.parse(body) as typeof parsed) : null;
    } catch {
      parsed = null;
    }
    provider = String(parsed?.provider || provider || "").trim().toLowerCase();
    toolCount = Number(parsed?.tool_uses_count || 0);
    firstTool = parsed?.tool_uses_preview?.[0];
    toolValue = String(firstTool?.input?.value || "");
  }
  if (!res.ok || !parsed?.ok || provider === "mock" || toolCount < 1 || toolValue !== "E2E_TOOL_READY") {
    throw new Error(
      `[setup] ${label} live LLM tool probe failed: ${url} -> ${res.status}\n` +
        `  provider       : ${provider || "(unknown)"}\n` +
        `  model          : ${parsed?.model || "(unknown)"}\n` +
        `  stop_reason    : ${parsed?.stop_reason || "(unknown)"}\n` +
        `  tool_uses_count: ${toolCount}\n` +
        `  first_tool     : ${JSON.stringify(firstTool ?? null)}\n` +
        `  error          : ${parsed?.error || "(none)"}\n` +
        `  message        : ${parsed?.message || body.slice(0, 300)}\n` +
        `  status_code    : ${parsed?.status_code || 0}\n` +
        `  raw_body       : ${parsed?.raw_body || ""}\n` +
        "Fix the isolated workspace LLM provider/tool-call support before running prompt E2E.",
    );
  }
  console.log(
    `[setup] ${label} live LLM tool probe OK: provider=${provider} ` +
      `model=${parsed.model || "(default)"}`,
  );
}

/**
 * Dev HTTP logging appends to `<root>/dev_logs/http.jsonl` on every request.
 * We saw a 429 MB file slow the runtime to a crawl. Warn when it's large so the
 * operator runs `POST /dev/clear` (or disables dev logging) before a long run.
 */
async function warnDevLogBloat(api: string): Promise<void> {
  try {
    const res = await fetch(`${api}/dev/status`, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) return;
    const j = (await res.json()) as { files?: { name: string; size: number }[] };
    const big = (j?.files ?? []).filter((f) => f.size > 64 * 1024 * 1024);
    for (const f of big) {
      console.warn(
        `[setup] dev log bloat: ${f.name} = ${(f.size / 1048576).toFixed(0)} MB. ` +
          `Run 'curl -X POST ${api}/dev/clear' (or disable dev logging) for speed.`,
      );
    }
  } catch {
    /* best effort */
  }
}
