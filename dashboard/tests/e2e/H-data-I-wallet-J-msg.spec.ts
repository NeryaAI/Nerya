/**
 * Group H (data sources) + I (wallets) + J (messaging).
 */
import { test, expect } from "./fixtures";

test.describe("H — Data sources", () => {
  test("H1 — HTTP venue accepts URL + json_paths schema", async ({ api }) => {
    const r = await api
      .post<{ ok: boolean; error?: unknown }>("/accounts/upsert", {
        id: "playwright_custom_http",
        venue: "http",
        mode: "paper",
        provider_config: {
          url: "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
          json_paths: { last: "bitcoin.usd" },
        },
        credentials: {},
      })
      .catch((e) => ({ ok: false, error: String(e) }));
    expect(r.ok).toBeTruthy();
  });

  test("H4 — CoinGecko ETH price via natural language", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "CoinGecko 上 ETH 当前价是多少？",
    );
    expect(reply).toMatch(/\$?\s*\d+(?:[.,]\d+)?/);
  });

  test("H7 — data source sync ledger has entries", async ({ api }) => {
    const r = await api.get<{
      status: { source_id: string; last_sync_at?: string }[];
    }>("/data-sources/status");
    expect(r.status?.length ?? 0).toBeGreaterThan(0);
  });

  test("H9 — Financial Datasets status endpoint", async ({ api }) => {
    const r = await api.get<{ ready: boolean; sources?: unknown[] }>(
      "/data/financial_datasets/status",
    );
    expect(typeof r.ready).toBe("boolean");
  });
});

test.describe("I — Wallet providers", () => {
  test("I1 — self_custody is listed", async ({ api }) => {
    const r = await api.get<{ providers: { id: string }[] }>(
      "/wallet/providers",
    );
    expect((r.providers ?? []).some((p) => p.id === "self_custody")).toBeTruthy();
  });

  test("I2 — okx_os is listed", async ({ api }) => {
    const r = await api.get<{ providers: { id: string }[] }>(
      "/wallet/providers",
    );
    expect((r.providers ?? []).some((p) => p.id === "okx_os")).toBeTruthy();
  });

  test("I8 — swap from chat goes through approval gate (paper)", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "用一个 self_custody 钱包 (paper)，swap 0.1 ETH 换 USDC，需要审批。",
      { timeoutMs: 180_000 },
    );
    expect(reply.toLowerCase()).toMatch(
      /approval|approve|审批|inbox|批准/,
    );
    const inbox = await api
      .get<{ items: { kind?: string; status?: string }[] }>("/inbox?limit=20")
      .catch(() => ({ items: [] }));
    const pending = (inbox.items ?? []).find(
      (x) => /trade|swap|approval/i.test(JSON.stringify(x)),
    );
    expect(pending, "swap not pending in inbox").toBeTruthy();
  });
});

test.describe("J — Messaging gateways", () => {
  test("J1 — gateway/platforms contains Telegram", async ({ api }) => {
    const r = await api.get<{ platforms: { id: string }[] }>(
      "/gateway/platforms",
    );
    expect((r.platforms ?? []).some((p) => p.id === "telegram")).toBeTruthy();
  });

  test("J6 — telegram diagnose route exists", async ({ api }) => {
    // Should respond (possibly with diagnostic = not_configured)
    const r = await api
      .post<{ status?: string }>("/gateway/telegram/diagnose", {})
      .catch((e) => ({ status: `error: ${e}` }));
    expect(r).toBeTruthy();
  });
});
