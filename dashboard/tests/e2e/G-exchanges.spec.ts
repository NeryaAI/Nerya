/**
 * Group G + G-EXT — Exchanges, options venues, Aster, decentralized perps.
 * Maps to G1..G10 and GX1..GX14 in nerya-prompt-test-plan.md.
 */
import { test, expect } from "./fixtures";

test.describe("G — Exchange providers", () => {
  test("G1 — Kraken paper account upsert via API", async ({ api }) => {
    const r = await api.post<{ ok: boolean }>("/accounts/upsert", {
      id: "playwright_kraken_paper",
      venue: "ccxt:kraken",
      mode: "paper",
      credentials: {},
    });
    expect(r.ok).toBeTruthy();
    const list = await api.get<{ accounts: { id: string }[] }>(
      "/accounts/list",
    );
    expect(
      list.accounts.some((a) => a.id === "playwright_kraken_paper"),
    ).toBeTruthy();
  });

  test("G2 — OKX credential_schema includes passphrase", async ({ api }) => {
    const r = await api.post<{
      schema: { fields: { name: string; required?: boolean }[] };
    }>("/exchanges/credential_schema", { venue: "okx" });
    const names = (r.schema?.fields ?? []).map((f) => f.name.toLowerCase());
    expect(names).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/api.?key/),
        expect.stringMatching(/secret/),
        expect.stringMatching(/passphrase/),
      ]),
    );
  });

  test("G10 — providers list contains CCXT + native venues", async ({
    api,
  }) => {
    const r = await api.get<{ providers: { id: string; aliases?: string[] }[] }>(
      "/exchanges/providers",
    );
    const ids = (r.providers ?? []).map((p) => p.id.toLowerCase());
    expect(ids).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^ccxt|binance|kraken|okx/),
      ]),
    );
    expect(ids).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/polymarket|ibkr|alpaca|mt5/),
      ]),
    );
  });
});

test.describe("G-EXT — Options chains, Aster DEX, decentralized perps", () => {
  test("GX1 — Deribit BTC option chain via CCXT", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "用 Deribit (paper) 列一下 BTC 当周到期的期权链，按 strike 排序。",
      { timeoutMs: 180_000 },
    );
    expect(reply.toLowerCase()).toMatch(/deribit/);
    expect(reply.toLowerCase()).toMatch(/strike|call|put|期权|到期/);
  });

  test("GX2 — IBKR option chain claim is grounded (paper)", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "用 IBKR paper 拉 SPY 今日到期期权链，给我 ATM 上下 5 档。",
      { timeoutMs: 180_000 },
    );
    // Must mention IBKR / SPY / 0DTE OR honestly disclaim gateway missing
    expect(reply.toLowerCase()).toMatch(
      /spy|ibkr|0dte|gateway|tws|无法连接|connect/i,
    );
  });

  test("GX6 — Aster exchange_author scaffold creates provider_proposal", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "我想接入 Aster DEX 永续。Base URL: https://fapi.asterdex.com。",
        "签名方式: EIP-712 Agent Key，user 字段=主钱包地址，signer=Agent 私钥。",
        "请用 exchange_author 做 scaffold_http，先别 approve。",
      ].join(" "),
      { timeoutMs: 240_000 },
    );
    expect(reply.toLowerCase()).toMatch(
      /aster|scaffold|proposal|provider/,
    );
    const r = await api.get<{
      proposals: { id: string; kind: string; metadata?: unknown }[];
    }>("/evolution/proposals?kind=provider_proposal&limit=20");
    const aster = (r.proposals ?? []).find((p) =>
      JSON.stringify(p).toLowerCase().includes("aster"),
    );
    expect(aster, "no aster provider_proposal").toBeTruthy();
  });

  test("GX7 — Aster public ticker after approval (skip if not approved)", async ({
    api,
  }) => {
    const providers = await api.get<{ providers: { id: string }[] }>(
      "/exchanges/providers",
    );
    const hasAster = (providers.providers ?? []).some((p) =>
      p.id.toLowerCase().includes("aster"),
    );
    test.skip(!hasAster, "Aster provider not approved yet — run GX6 first");
    const ping = await api.post<{ ok: boolean; latency_ms?: number }>(
      "/exchanges/ping",
      { venue: "aster" },
    );
    expect(ping.ok).toBeTruthy();
  });

  test("GX8 — Aster credentials never echoed in plaintext", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const fakeKey =
      "0xdeadbeefcafebabe1234567890abcdef0123456789abcdef0123456789abcdef";
    const reply = await chatSend(
      page,
      `把 Aster Agent Key 存进 vault: ${fakeKey}，user 钱包是 0xabc...，并确认存好。`,
    );
    // The plaintext key must NEVER be returned in the assistant reply.
    expect(reply).not.toContain(fakeKey);
    expect(reply.toLowerCase()).toMatch(/vault|存储|已保存|stored/);
  });

  test("GX10 — Hyperliquid paper account via CCXT", async ({ api }) => {
    const r = await api
      .post<{ ok: boolean; error?: unknown }>("/accounts/upsert", {
        id: "playwright_hyperliquid_paper",
        venue: "ccxt:hyperliquid",
        mode: "paper",
        credentials: {},
      })
      .catch((e) => ({ ok: false, error: String(e) }));
    // Either succeeds OR errors with venue-not-supported (still graceful)
    expect(r.ok || /not.?supported|unknown.?venue/i.test(JSON.stringify(r))).toBeTruthy();
  });

  test("GX14 — cross-venue cash-and-carry proposal mentions funding", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "做一个 Binance 现货 + Aster 永续的 cash-and-carry 套利策略 (paper)，回测 30 天。",
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(
      /funding|资金费|cash.?and.?carry|spread/,
    );
    const r = await api.get<{
      proposals: { id: string; metadata?: unknown }[];
    }>("/evolution/proposals?kind=strategy_package_proposal&limit=10");
    const cnc = (r.proposals ?? []).find((p) =>
      /(cash.?and.?carry|funding|aster)/i.test(JSON.stringify(p)),
    );
    expect(cnc).toBeTruthy();
  });
});
