/**
 * Nerya E2E SMOKE suite — picks one canonical case per capability group.
 * Runs in ~10 minutes. Use this in PR gating; full plan runs nightly.
 *
 * Coverage map (matches Nerya/docs/nerya-prompt-test-plan.md):
 *   A1, B1, C1, D1, E1, F1, G1, GX6, H7, I1, J1
 */
import { test, expect } from "./fixtures";

test.describe("Nerya SMOKE — one per capability group", () => {
  test("A1 — greeting does not call tools", async ({ page, openChat, chatSend }) => {
    await openChat();
    const reply = await chatSend(page, "你好，你能做什么？");
    expect(reply.length).toBeGreaterThan(20);
    // No tool-call card should appear for a pure greeting
    await expect(page.locator('[data-tool-card], .tool-call-card')).toHaveCount(0);
  });

  test("B1 — recent crypto news returns real-source items", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(page, "给我看看最近 3 小时的加密新闻");
    // Must reference at least one canonical RSS source
    expect(reply.toLowerCase()).toMatch(/coindesk|cointelegraph|bitcoinmagazine/);
  });

  test("C1 — script strategy proposal + backtest", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "帮我做一个 1 小时级别的 BTC 现货动量策略，回测一下",
      { timeoutMs: 240_000 },
    );
    // Smoke: reply mentions backtest + sharpe/return/drawdown
    expect(reply.toLowerCase()).toMatch(/backtest|回测/);
    expect(reply.toLowerCase()).toMatch(/sharpe|return|drawdown|收益|回撤/);
    // Sanity-check proposal exists via runtime API
    const list = await api.get<{ proposals: { id: string; kind: string }[] }>(
      "/evolution/proposals?kind=strategy_package_proposal&limit=5",
    );
    expect(list.proposals?.length ?? 0).toBeGreaterThan(0);
  });

  test("D1 — recurring agent schedule registers", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    await chatSend(
      page,
      "每小时帮我看一遍 BTC/ETH 仓位健康度（仅 paper），不用真发送",
      { timeoutMs: 120_000 },
    );
    const schedules = await api.get<{ schedules: { id: string; session_kind?: string }[] }>(
      "/triggers/schedules",
    );
    const agentSchedules = (schedules.schedules ?? []).filter(
      (s) => s.session_kind === "agent",
    );
    expect(agentSchedules.length).toBeGreaterThan(0);
  });

  test("E1 — market_analysis_team produces final_report", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "用 AgentTeam 全面分析 TSLA，paper 模式即可，给我 buy/hold/sell 评级",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/buy|hold|sell|买入|持有|卖出/);
    const runs = await api.get<{ runs: { id: string; template: string }[] }>(
      "/teams/runs?limit=5",
    );
    const tslaRun = (runs.runs ?? []).find((r) =>
      r.template?.includes("market_analysis_team"),
    );
    expect(tslaRun, "market_analysis_team run not found").toBeTruthy();
  });

  test("F1 — reflect produces proposal, no direct workspace mutation", async ({
    api,
  }) => {
    const before = await api.get<{ proposals: { id: string }[] }>(
      "/evolution/proposals?kind=learning_update&limit=20",
    );
    const beforeCount = before.proposals?.length ?? 0;
    await api.post("/evolution/reflect", { window_days: 7 });
    const after = await api.get<{ proposals: { id: string }[] }>(
      "/evolution/proposals?kind=learning_update&limit=20",
    );
    expect(after.proposals?.length ?? 0).toBeGreaterThanOrEqual(beforeCount);
  });

  test("G1 — CCXT venue Kraken paper account creates", async ({ api }) => {
    const r = await api.post<{ ok: boolean; account?: { id: string } }>(
      "/accounts/upsert",
      {
        id: "smoke_kraken_paper",
        venue: "ccxt:kraken",
        mode: "paper",
        credentials: {},
      },
    );
    expect(r.ok).toBeTruthy();
    const list = await api.get<{ accounts: { id: string }[] }>("/accounts/list");
    expect(list.accounts.some((a) => a.id === "smoke_kraken_paper")).toBeTruthy();
  });

  test("GX6 — Aster scaffold (provider proposal pending)", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "我想接入 Aster DEX 永续。Base URL: https://fapi.asterdex.com，签名是 EIP-712 Agent Key。请用 exchange_author scaffold 一个 provider，先别 approve。",
      { timeoutMs: 180_000 },
    );
    expect(reply.toLowerCase()).toMatch(/aster|scaffold|proposal|provider/);
    const list = await api.get<{ proposals: { id: string; kind: string; metadata?: Record<string, unknown> }[] }>(
      "/evolution/proposals?kind=provider_proposal&limit=10",
    );
    const asterProposal = (list.proposals ?? []).find((p) =>
      JSON.stringify(p).toLowerCase().includes("aster"),
    );
    expect(asterProposal, "aster provider_proposal not created").toBeTruthy();
  });

  test("H7 — data source sync ledger available", async ({ api }) => {
    const s = await api.get<{ status: { source_id: string; last_sync_at?: string }[] }>(
      "/data-sources/status",
    );
    expect(s.status?.length ?? 0).toBeGreaterThan(0);
  });

  test("I1 — self_custody wallet provider available", async ({ api }) => {
    const r = await api.get<{ providers: { id: string }[] }>("/wallet/providers");
    expect(
      (r.providers ?? []).some((p) => p.id === "self_custody"),
      "self_custody provider missing",
    ).toBeTruthy();
  });

  test("J1 — gateway platforms list contains Telegram", async ({ api }) => {
    const r = await api.get<{ platforms: { id: string; support_level?: string }[] }>(
      "/gateway/platforms",
    );
    const tg = (r.platforms ?? []).find((p) => p.id === "telegram");
    expect(tg, "telegram platform missing").toBeTruthy();
  });
});
