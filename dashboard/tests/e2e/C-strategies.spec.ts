/**
 * Group C — Three strategy execution modes + backtest.
 * Maps to C1..C18 in nerya-prompt-test-plan.md.
 *
 *  - C1  script        BTC momentum
 *  - C5  agent_task    BSC smart-money meme
 *  - C7  agent_team    NVDA daily team
 *  - C10 skip backtest is blocked at promote
 *  - C15 backtest chart renders in dashboard
 */
import { test, expect } from "./fixtures";

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function findLatestProposalByKind(
  api: {
    get: <T,>(p: string) => Promise<T>;
  },
  kind: string,
  needleInBody?: string,
): Promise<string | null> {
  const res = await api.get<{
    proposals: { id: string; kind: string; metadata?: Record<string, unknown> }[];
  }>(`/evolution/proposals?kind=${kind}&limit=20`);
  const ps = res.proposals ?? [];
  if (!ps.length) return null;
  if (!needleInBody) return ps[0]?.id ?? null;
  const match = ps.find((p) =>
    JSON.stringify(p).toLowerCase().includes(needleInBody.toLowerCase()),
  );
  return match?.id ?? ps[0]?.id ?? null;
}

test.describe("C — Strategies (script / agent / agent_team)", () => {
  test("C1 — script BTC 1h momentum: proposal + backtest", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "帮我做一个 1 小时级别的 BTC 现货动量策略 (paper)，回测一下。",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/backtest|回测/);
    const pid = await findLatestProposalByKind(
      api,
      "strategy_package_proposal",
      "btc",
    );
    expect(pid, "no BTC proposal found").toBeTruthy();
    // Inspect proposal metadata for execution_mode=script
    const detail = await api.get<{
      proposal: { metadata?: Record<string, unknown> };
    }>(`/evolution/proposals/${pid}`);
    const meta = JSON.stringify(detail.proposal?.metadata ?? {});
    expect(meta).toMatch(/"execution_mode":\s*"script"/);
  });

  test("C5 — agent_task BSC meme strategy", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "做一个 BSC 链上跟单聪明钱钱包的 meme 币策略 (paper)。",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/agent_task|smart.?money|meme|链上/);
    const pid = await findLatestProposalByKind(
      api,
      "strategy_package_proposal",
      "meme",
    );
    expect(pid).toBeTruthy();
    const detail = await api.get<{
      proposal: { metadata?: Record<string, unknown> };
    }>(`/evolution/proposals/${pid}`);
    const meta = JSON.stringify(detail.proposal?.metadata ?? {}).toLowerCase();
    expect(meta).toMatch(
      /"execution_mode":\s*"(agent|agent_task)"/,
    );
  });

  test("C7 — agent_team NVDA daily strategy", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "用 AgentTeam 长期分析 NVDA，每天开盘前给我评分和仓位建议 (paper)。",
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/team|nvda|nvidia/);
    const pid = await findLatestProposalByKind(
      api,
      "strategy_package_proposal",
      "nvda",
    );
    expect(pid).toBeTruthy();
    const detail = await api.get<{
      proposal: { metadata?: Record<string, unknown> };
    }>(`/evolution/proposals/${pid}`);
    const meta = JSON.stringify(detail.proposal?.metadata ?? {}).toLowerCase();
    expect(meta).toMatch(/"execution_mode":\s*"(agent_team|team)"/);
  });

  test("C10 — promote without backtest is blocked", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    await chatSend(page, "做个简单的 BTC 网格 (paper)，不用回测了", {
      timeoutMs: 180_000,
    });
    const pid = await findLatestProposalByKind(
      api,
      "strategy_package_proposal",
      "grid",
    );
    expect(pid).toBeTruthy();
    // Strict promote via native tool surface (require_backtest defaults true)
    const r = await fetch(
      `${process.env.NERYA_API ?? "http://127.0.0.1:18318"}/strategies/runtime/promote`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          proposal_id: pid,
          require_backtest: true,
        }),
      },
    );
    const body = await r.json();
    // Either rejected with reason=backtest_required OR returns next_required_action
    const blocked =
      r.status === 409 ||
      body?.error?.reason === "backtest_required" ||
      body?.next_required_action?.tool === "strategy_backtest";
    expect(blocked, `promote should be blocked: ${JSON.stringify(body)}`).toBeTruthy();
  });

  test("C15 — backtest chart renders on /strategies/[id]/backtests/[ts]", async ({
    page,
    api,
  }) => {
    // Find any strategy with backtests
    const list = await api.get<{ strategies: { id: string }[] }>(
      "/strategies/list",
    );
    const cand = list.strategies?.[0];
    test.skip(!cand, "no promoted strategy available");
    const bts = await api.post<{
      backtests: { ts: string }[];
    }>("/strategy/backtests", { strategy_id: cand!.id });
    const latest = bts.backtests?.[0]?.ts;
    test.skip(!latest, "no backtest artifact available");
    await page.goto(`/strategies/${cand!.id}/backtests/${latest}`);
    // Either chart canvas OR metrics card should render
    await expect(
      page.locator("canvas, svg, [data-testid='backtest-chart']").first(),
    ).toBeVisible({ timeout: 60_000 });
  });
});
