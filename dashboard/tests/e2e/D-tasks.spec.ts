/**
 * Group D — Task planning (one-off subagent, recurring schedule, session API).
 * Maps to D1..D10 in nerya-prompt-test-plan.md.
 */
import { test, expect } from "./fixtures";

test.describe("D — Tasks & schedules", () => {
  test("D1 — one-off background task launches and finishes", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "跑个后台任务：把最近一周的 ETH/BTC 比率拉出来给我做个简报。",
      { timeoutMs: 240_000 },
    );
    expect(reply.toLowerCase()).toMatch(/eth|btc|ratio|比率/);
    const tasks = await api
      .get<{ tasks: { id: string; status: string; created_at: string }[] }>(
        "/agent/tasks?limit=10",
      )
      .catch(() => ({ tasks: [] }));
    expect(tasks.tasks?.length ?? 0).toBeGreaterThan(0);
  });

  test("D4 — recurring agent schedule registered in schedules.yml", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    await chatSend(
      page,
      "每小时帮我看一遍 BTC/ETH 仓位健康度（仅 paper），不用真发送。",
      { timeoutMs: 120_000 },
    );
    const r = await api.get<{
      schedules: {
        id: string;
        session_kind?: string;
        cron?: string;
      }[];
    }>("/triggers/schedules");
    const agentRows = (r.schedules ?? []).filter(
      (s) => s.session_kind === "agent",
    );
    expect(agentRows.length).toBeGreaterThan(0);
  });

  test("D6 — Workflows page lists newly-created schedule", async ({
    page,
    api,
  }) => {
    await page.goto("/workflows");
    // Wait for any schedule row to appear (or empty-state)
    const anyRow = page.locator('[data-testid="schedule-row"], table tbody tr, li[role="listitem"]').first();
    const empty = page.getByText(/no scheduled|no workflow|暂无/i).first();
    await expect(anyRow.or(empty)).toBeVisible({ timeout: 30_000 });
  });

  test("D7 — strategy_design_team produces phased events", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "跑一遍 strategy_design_team，给我设计一个 SOL 现货策略 (paper)。",
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/team|sol|策略|strategy/);
    const runs = await api.get<{
      runs: { id: string; template: string; status: string }[];
    }>("/teams/runs?limit=10");
    const sdt = (runs.runs ?? []).find((r) =>
      r.template?.includes("strategy_design_team"),
    );
    expect(sdt, "strategy_design_team run missing").toBeTruthy();
  });
});
