/**
 * Shared Playwright fixtures + helpers for Nerya E2E.
 *
 * This file provides:
 *  - `nerya`: a typed fixture with chat / API / workspace helpers
 *  - `chatSend()`: type into the chat input and press send
 *  - `waitForAssistant()`: wait until a new assistant turn is rendered
 *  - `api.get/post`: lightweight HTTP helper against the runtime API
 *
 * Selectors target stable DOM produced by `components/chat/ChatInput.tsx`
 * (a single visible <textarea> + a send <button> with aria-label `Send`/`发送`).
 */
import { test as base, expect, type Page, type APIRequestContext } from "@playwright/test";

const NERYA_API = process.env.NERYA_API ?? "http://127.0.0.1:18318";

// localStorage key the dashboard reads run settings from (see lib/chat.ts
// SETTINGS_KEY). Keep in sync with that constant.
const CHAT_RUN_SETTINGS_KEY = "nerya.chat.runSettings.v2";

// Unattended E2E must not stall waiting for human approval. Dangerous tools
// (e.g. trade_intent_submit, "dangerous classification") create a
// `tool_permission` approval in the default mode and the turn hangs forever
// (see notes.md RC5). "yolo" mode auto-allows tool calls (nerya/tools/
// permissions.py -> "yolo mode"), so seed it before the page loads. The
// dashboard only honours "default" | "yolo"; anything else clamps to default.
const PERMISSION_MODE: "default" | "yolo" =
  process.env.NERYA_PERMISSION_MODE === "default" ? "default" : "yolo";

// A turn that ends in an error card whose text matches this is a *successful
// defense* (prompt-guard / policy / safety refusal), not a harness/infra
// failure. Redline cases assert against this text, so the fixture returns it
// rather than throwing (RC10). Keep broad but specific to safety outcomes.
const SAFETY_BLOCK_RE =
  /prompt[_ ]?guard|guard[_ ]?blocked|blocked this input|content[_ ]?policy|policy[_ ]?violation|refus|disallow|not (allowed|permitted)|safety|拒绝|阻止|不能|无法(提供|执行)/i;

export interface NeryaFixture {
  /** Open a fresh chat session and return the route */
  openChat: () => Promise<{ page: Page; sessionId: string }>;
  /** Type+send a prompt in the active chat, return assistant message text */
  chatSend: (page: Page, prompt: string, opts?: { timeoutMs?: number }) => Promise<string>;
  /** Wait for the most recent assistant bubble to settle */
  waitForAssistant: (page: Page, opts?: { timeoutMs?: number }) => Promise<string>;
  /** Read journal jsonl tail (server-side check via API) */
  journalTail: (topic: string, lines?: number) => Promise<Record<string, unknown>[]>;
  /** Raw API helper */
  api: {
    request: APIRequestContext;
    get: <T = unknown>(path: string) => Promise<T>;
    post: <T = unknown>(path: string, body: unknown) => Promise<T>;
  };
}

export const test = base.extend<NeryaFixture>({
  api: async ({ playwright }, use) => {
    const request = await playwright.request.newContext({
      baseURL: NERYA_API,
      extraHTTPHeaders: { "content-type": "application/json", "x-nerya-test": "playwright" },
    });
    await use({
      request,
      get: async <T,>(path: string) => {
        const r = await request.get(path);
        if (!r.ok()) throw new Error(`GET ${path} -> ${r.status()}: ${await r.text()}`);
        return (await r.json()) as T;
      },
      post: async <T,>(path: string, body: unknown) => {
        const r = await request.post(path, { data: body });
        if (!r.ok()) throw new Error(`POST ${path} -> ${r.status()}: ${await r.text()}`);
        return (await r.json()) as T;
      },
    });
    await request.dispose();
  },

  openChat: async ({ page }, use) => {
    // Seed run settings BEFORE any page script runs so the very first turn
    // already uses a non-gating permission mode (RC5). addInitScript re-runs on
    // every navigation in this page context, so it survives the goto below.
    await page.addInitScript(
      ([key, mode]) => {
        try {
          const raw = window.localStorage.getItem(key);
          const prev = raw ? JSON.parse(raw) : {};
          window.localStorage.setItem(
            key,
            JSON.stringify({ ...prev, permission_mode: mode }),
          );
        } catch {
          /* localStorage may be unavailable on about:blank; goto re-runs this */
        }
      },
      [CHAT_RUN_SETTINGS_KEY, PERMISSION_MODE] as const,
    );
    await use(async () => {
      await page.goto("/chat");
      // Wait for empty-state OR existing thread to be ready
      await page.waitForSelector("textarea", { state: "visible", timeout: 60_000 });
      const sessionId = page.url().split("/chat/")[1] ?? "ephemeral";
      return { page, sessionId };
    });
  },

  chatSend: async ({}, use) => {
    await use(async (page: Page, prompt: string, opts) => {
      const ta = page.locator("textarea").first();
      await ta.click();
      await ta.fill(prompt);
      // Send button — match by aria-label in EN or ZH
      const sendBtn = page.getByRole("button", { name: /^(Send|发送)$/ });
      await sendBtn.click();
      return waitAssistantText(page, opts?.timeoutMs ?? 120_000);
    });
  },

  waitForAssistant: async ({}, use) => {
    await use(async (page: Page, opts) => waitAssistantText(page, opts?.timeoutMs ?? 120_000));
  },

  journalTail: async ({ playwright }, use) => {
    const req = await playwright.request.newContext({ baseURL: NERYA_API });
    await use(async (topic: string, lines = 20) => {
      // Use /agent/tasks/timeline as a generic event window since /journal isn't public.
      // Topic-specific evidence is verified via dedicated endpoints in each spec.
      const r = await req.get(
        `/agent/tasks/timeline?session_id=latest&topic=${encodeURIComponent(topic)}&limit=${lines}`,
      );
      if (!r.ok()) return [];
      const j = await r.json();
      return (j?.events ?? j?.items ?? []) as Record<string, unknown>[];
    });
    await req.dispose();
  },
});

function chatSessionIdFromPage(page: Page): string | null {
  try {
    const url = new URL(page.url());
    const match = url.pathname.match(/\/chat\/([^/?#]+)/);
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

async function interruptBackendTurn(page: Page, reason: string): Promise<string[]> {
  const sessionId = chatSessionIdFromPage(page);
  if (!sessionId) return ["  cleanup: no chat session id found; cannot interrupt backend turn"];

  const url = `${NERYA_API.replace(/\/$/, "")}/agent/interrupt`;
  const waitMs = Number(process.env.NERYA_E2E_CANCEL_WAIT_MS ?? 120_000);
  const deadline = Date.now() + Math.max(0, waitMs);
  const lines: string[] = [];

  async function signal(): Promise<boolean | null> {
    try {
      const res = await page.request.post(url, {
        data: { session_id: sessionId, reason },
        timeout: 8_000,
      });
      const body = (await res.json().catch(() => ({}))) as { cancelled?: unknown };
      if (!res.ok()) {
        lines.push(`  cleanup: POST /agent/interrupt -> ${res.status()}`);
        return null;
      }
      return body.cancelled === true;
    } catch (e) {
      lines.push(`  cleanup: POST /agent/interrupt error (${String(e)})`);
      return null;
    }
  }

  const first = await signal();
  if (first !== true) {
    lines.push(
      `  cleanup: no registered in-flight turn for session ${sessionId} ` +
        "(already stopped or never reached backend)",
    );
    return lines;
  }

  lines.push(`  cleanup: interrupt signalled for session ${sessionId}`);
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1_000));
    const active = await signal();
    if (active === false) {
      lines.push("  cleanup: backend turn token unregistered");
      return lines;
    }
  }

  lines.push(
    `  cleanup: backend turn still registered after ${waitMs}ms; ` +
      "do not trust following case results until runtime is restarted",
  );
  return lines;
}

/**
 * Wait for the latest assistant turn to *settle* and return its visible text.
 *
 * Previously this only waited for `data-turn-loading="false"`. When the dashboard
 * proxy 500s (corrupted `.next`) or hangs mid-HMR, the bubble can stay
 * `loading="true"` forever, so every failure surfaced as an opaque 120s timeout
 * (see tests/e2e/notes.md, RC1/RC2/RC4). Now we race three outcomes:
 *   1. completion   — `data-turn-loading="false"`
 *   2. error card   — `[data-turn-section="error"]` rendered inside the bubble
 *   3. timeout      — throw an *enriched* error that probes the proxy / runtime
 *                     so the operator sees the real cause (broken proxy, wrong
 *                     workspace, pending approvals) instead of just "timeout".
 *
 * On an error card we throw immediately with the raw error text, turning a 120s
 * dead wait into a sub-second, actionable failure.
 */
async function waitAssistantText(page: Page, timeoutMs: number): Promise<string> {
  const bubbles = page.locator('[data-turn-role="assistant"]');
  const last = bubbles.last();
  await expect(last).toBeVisible({ timeout: Math.min(timeoutMs, 60_000) });

  const settled = last.locator(':scope[data-turn-loading="false"]');
  const errorCard = last.locator('[data-turn-section="error"]');

  // Whichever resolves first wins. `waitFor` rejects on timeout, so we wrap
  // each branch and let Promise.race surface the first *fulfilled* outcome.
  const outcome = await Promise.race([
    settled
      .waitFor({ state: "attached", timeout: timeoutMs })
      .then(() => "settled" as const)
      .catch(() => "timeout" as const),
    errorCard
      .waitFor({ state: "visible", timeout: timeoutMs })
      .then(() => "error" as const)
      .catch(() => "timeout" as const),
  ]);

  if (outcome === "timeout") {
    const diagnostics = await describeStuckTurn(page, timeoutMs);
    const cleanup = await interruptBackendTurn(page, "playwright_timeout");
    throw new Error([diagnostics, ...cleanup].join("\n"));
  }

  // Even when the turn "settled", it may have settled into an error card.
  if (await errorCard.count()) {
    const raw = (await errorCard.innerText()).trim();
    // A SAFETY block (prompt-guard / policy refusal) is a LEGIT terminal state
    // for redline cases (A5, L*): the must_contain / must_not_contain regexes are
    // written against exactly this text (e.g. "prompt_guard_blocked", "guard",
    // "blocked"). Returning it lets the runner assert the defense worked, instead
    // of hard-failing a correctly-defended turn (see notes.md RC10). Real infra /
    // tool errors still throw so they surface as failures.
    if (SAFETY_BLOCK_RE.test(raw)) {
      return raw;
    }
    throw new Error(`Assistant turn failed:\n${raw}`);
  }

  // Prefer the dedicated reply section; fall back to the whole bubble.
  const reply = last.locator('[data-turn-section="reply"]');
  if (await reply.count()) {
    return (await reply.first().innerText()).trim();
  }
  return (await last.innerText()).trim();
}

/**
 * Build a diagnostic message when a turn never settles. Probes the dashboard
 * proxy (GET + a cheap POST) and the runtime so the failure names the real
 * culprit: broken `.next` proxy, runtime down, or an approval-gated stall.
 */
async function describeStuckTurn(page: Page, timeoutMs: number): Promise<string> {
  const lines: string[] = [
    `Assistant turn did not settle within ${timeoutMs}ms ` +
      `(data-turn-loading stayed "true" and no error card appeared).`,
  ];
  // GET proxy (should be 200 when the dashboard is healthy)
  try {
    const r = await page.request.get("/api/proxy/health", { timeout: 8_000 });
    lines.push(`  proxy GET /api/proxy/health -> ${r.status()}`);
  } catch (e) {
    lines.push(`  proxy GET /api/proxy/health -> unreachable (${String(e)})`);
  }
  // POST proxy probe. The runtime answers this with a 500 JSON envelope for a
  // bogus turn_id, so classify by ORIGIN: a proxy-origin error label
  // (upstream_unreachable / proxy_handler_error) or a non-JSON framework 500 is
  // the corrupted-.next signature; a runtime JSON error means the proxy is fine.
  try {
    const r = await page.request.post("/api/proxy/agent/turn_state", {
      data: { turn_id: "__probe__" },
      timeout: 8_000,
    });
    const text = await r.text().catch(() => "");
    let label = "";
    try {
      const j = JSON.parse(text) as { error?: unknown };
      if (typeof j?.error === "string") label = j.error;
    } catch {
      /* non-JSON */
    }
    const proxyBroken =
      label === "upstream_unreachable" ||
      label === "proxy_handler_error" ||
      (!label && r.status() >= 500);
    lines.push(
      `  proxy POST /api/proxy/agent/turn_state -> ${r.status()}` +
        (label ? ` (${label})` : "") +
        (proxyBroken
          ? "  <-- BROKEN proxy route (npm run clean:next + restart dashboard)"
          : "  (runtime answered; proxy OK)"),
    );
  } catch (e) {
    lines.push(`  proxy POST /api/proxy/agent/turn_state -> error (${String(e)})`);
  }
  // Runtime approvals — an approval-gated turn stalls forever unattended (RC5).
  try {
    const r = await page.request.get(`${NERYA_API}/operator/overview`, { timeout: 8_000 });
    if (r.ok()) {
      const j = (await r.json()) as { data?: { health?: { pending_approvals?: number } } };
      const pending = j?.data?.health?.pending_approvals;
      if (typeof pending === "number" && pending > 0) {
        lines.push(
          `  runtime pending_approvals=${pending} <-- turn may be blocked on ` +
            `approval; enable auto-approve or use a non-gating permission mode`,
        );
      }
    }
  } catch {
    /* best effort */
  }
  return lines.join("\n");
}

export { expect };
export const NERYA_API_URL = NERYA_API;
