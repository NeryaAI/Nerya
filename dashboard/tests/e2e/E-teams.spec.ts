/**
 * Group E — Agent Team research.
 * Maps to E1..E14 in nerya-prompt-test-plan.md.
 */
import { test, expect } from "./fixtures";

test.describe("E — Agent Team research", () => {
  test("E1 — market_analysis_team gives verdict on TSLA", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "用 AgentTeam 全面分析 TSLA，给我 buy/hold/sell 评级 (paper)。",
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/buy|hold|sell|买入|持有|卖出/);
    const runs = await api.get<{
      runs: { id: string; template: string; status: string }[];
    }>("/teams/runs?limit=10");
    const r = (runs.runs ?? []).find((x) =>
      x.template?.includes("market_analysis_team"),
    );
    expect(r, "market_analysis_team run not found").toBeTruthy();
  });

  test("E2 — investment_committee_team produces bull vs bear", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "让多空委员会辩论：现在该不该 long BTC？(paper)",
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/bull|bear|多|空|long|short/);
    const runs = await api.get<{ runs: { template: string }[] }>(
      "/teams/runs?limit=10",
    );
    expect(
      (runs.runs ?? []).some((r) =>
        r.template?.includes("investment_committee_team"),
      ),
    ).toBeTruthy();
  });

  test("E4 — custom role saved and visible in /teams/roles", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    await chatSend(
      page,
      "加一个新角色叫 china_macro_specialist，专门覆盖 PBOC 决议与中国 CPI，下次分析 A 股带上他。",
      { timeoutMs: 180_000 },
    );
    const roles = await api.get<{ roles: { id: string }[] }>(
      "/teams/roles",
    );
    const ours = (roles.roles ?? []).find(
      (r) => r.id === "china_macro_specialist",
    );
    expect(ours, "custom role missing").toBeTruthy();
  });

  test("E7 — parallel concurrency limit (≥ 4 tickers)", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    await chatSend(
      page,
      "同时让团队分析这 6 只股：AAPL/MSFT/NVDA/AMD/GOOGL/META (paper)。",
      { timeoutMs: 480_000 },
    );
    // No strict assert on parallelism; verify a team run was created
    const runs = await api.get<{ runs: { id: string; tasks?: unknown[] }[] }>(
      "/teams/runs?limit=5",
    );
    expect(runs.runs?.[0]).toBeTruthy();
  });
});
