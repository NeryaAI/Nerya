/**
 * Group K — Comprehensive multi-step user journeys.
 * These tests run longer (5-10 min each) and validate cross-capability flow.
 */
import { test, expect } from "./fixtures";

test.describe("K — Comprehensive user journeys", () => {
  test("K1 — full onboard: account → strategy → schedule → delivery", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "我是新手，帮我从 0 配置一个 paper 工作流：",
        "1) 加一个 Binance paper 账户 id=k1_binance_paper",
        "2) 做一个保守的 BTC 月度定投策略并完成 backtest",
        "3) 每月 1 号 00:00 UTC 执行",
        "（暂不要真的连 Telegram，只在最后告诉我命令）",
      ].join(" "),
      { timeoutMs: 480_000 },
    );
    // Reply should reference each of the three milestones
    expect(reply.toLowerCase()).toMatch(/account|账户/);
    expect(reply.toLowerCase()).toMatch(/dca|定投|strategy|策略/);
    expect(reply.toLowerCase()).toMatch(/月|monthly|cron/);
    // Verify side effects
    const accs = await api.get<{ accounts: { id: string }[] }>(
      "/accounts/list",
    );
    expect(
      accs.accounts.some((a) => a.id === "k1_binance_paper"),
    ).toBeTruthy();
    const schedules = await api.get<{
      schedules: { id: string; cron?: string }[];
    }>("/triggers/schedules");
    const monthly = (schedules.schedules ?? []).find((s) =>
      /^0 0 1/.test(s.cron ?? ""),
    );
    expect(monthly, "monthly schedule not registered").toBeTruthy();
  });

  test("K2 — research → strategy → propose pipeline", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "先用 AgentTeam 全面分析 NVDA（paper），如果团队结论是 BUY，就帮我做一个对应的买入策略并准备 promote（先停在 backtest 阶段，不要上线）。",
      { timeoutMs: 600_000 },
    );
    expect(reply.toLowerCase()).toMatch(/buy|hold|sell|nvda|nvidia/i);
    const proposals = await api.get<{
      proposals: { id: string; kind: string }[];
    }>("/evolution/proposals?kind=strategy_package_proposal&limit=10");
    const hasNvda = (proposals.proposals ?? []).some((p) =>
      JSON.stringify(p).toLowerCase().includes("nvda"),
    );
    expect(hasNvda).toBeTruthy();
  });
});
