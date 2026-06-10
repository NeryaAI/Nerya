/**
 * Group C-EXT.A — Script-driven Agent strategies (execution_mode=agent).
 * Maps to C-AT1..C-AT14 in nerya-prompt-test-plan.md.
 *
 * What we verify (per case):
 *  1) Reply mentions the indicator/signal logic the user asked for.
 *  2) Latest strategy_package_proposal exists and has execution_mode=agent.
 *  3) Proposal's main.py contains the indicator math (regex on the file body).
 *  4) Proposal's main.py uses StrategyAgentTask.dispatch / .skip / .error.
 *  5) Where applicable, attached_skills include news_social/research/market_data_routing.
 */
import { test, expect } from "./fixtures";

interface ProposalRow {
  id: string;
  kind: string;
  metadata?: Record<string, unknown>;
}

interface ProposalDetail {
  proposal: {
    id: string;
    state?: string;
    metadata?: Record<string, unknown>;
    files?: Record<string, string>; // { "main.py": "...", "strategy.yml": "..." }
  };
}

async function getLatestStrategyProposal(
  api: { get: <T,>(p: string) => Promise<T> },
  needles: string[],
): Promise<ProposalDetail["proposal"] | null> {
  const list = await api.get<{ proposals: ProposalRow[] }>(
    "/evolution/proposals?kind=strategy_package_proposal&limit=20",
  );
  const lowerNeedles = needles.map((n) => n.toLowerCase());
  const match = (list.proposals ?? []).find((p) => {
    const blob = JSON.stringify(p).toLowerCase();
    return lowerNeedles.every((n) => blob.includes(n));
  });
  const pid = match?.id ?? list.proposals?.[0]?.id;
  if (!pid) return null;
  const d = await api.get<ProposalDetail>(`/evolution/proposals/${pid}`);
  return d.proposal ?? null;
}

function mainPyOf(p: ProposalDetail["proposal"] | null): string {
  if (!p?.files) return "";
  const k = Object.keys(p.files).find((x) => /(^|\/)main\.py$/.test(x));
  return k ? p.files[k] ?? "" : "";
}

function assertAgentExecutionMode(p: ProposalDetail["proposal"] | null) {
  expect(p, "no strategy proposal returned").toBeTruthy();
  const meta = JSON.stringify(p?.metadata ?? {}).toLowerCase();
  expect(meta).toMatch(/"execution_mode":\s*"(agent|agent_task)"/);
}

function assertAgentTaskApiUsed(mainPy: string) {
  expect(mainPy).toMatch(/from\s+nerya\.strategies\s+import[^\n]*StrategyAgentTask/);
  expect(mainPy).toMatch(/StrategyAgentTask\.dispatch\(/);
}

test.describe("C-EXT.A — Script-driven Agent (execution_mode=agent)", () => {
  test("C-AT1 — MACD crossover triggers Agent", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "做一个 BTC 4h 周期的策略（paper）：",
        "脚本里算 MACD(12,26,9)，金叉时让 Agent 综合新闻和市场情绪决定是否买入；",
        "死叉时让 Agent 决定平仓或反手做空。execution_mode 用 agent。",
      ].join(" "),
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/macd|金叉|死叉/);
    const p = await getLatestStrategyProposal(api, ["btc", "macd"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // MACD computation must appear (ema/12/26/9 or pandas ewm)
    expect(py.toLowerCase()).toMatch(
      /macd|ema\(.*12|ewm\(span=12|ema_fast|ema_slow/,
    );
    // Crossover comparison pattern (sign change)
    expect(py).toMatch(/(<\s*0[\s\S]{0,80}?>\s*0)|cross|金叉|crossover/i);
    // Agent prompt content
    expect(py).toMatch(/dispatch\([\s\S]{0,400}?prompt=/);
  });

  test("C-AT2 — RSI extremes triggers Agent", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "ETH 1h，RSI(14) < 30 或 > 70 时触发 Agent，让它结合资金费率和大单流向判断反转可信度。execution_mode=agent。",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/rsi/);
    const p = await getLatestStrategyProposal(api, ["eth", "rsi"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    expect(py.toLowerCase()).toMatch(/rsi/);
    // 30 / 70 thresholds
    expect(py).toMatch(/\b30\b|\b70\b/);
  });

  test("C-AT3 — support/resistance touch triggers Agent", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "SOL 1h 策略：脚本算最近 20 天的前期高点/低点 + 0.618 Fibonacci，价格 0.3% 距离内触及时让 Agent 判断真假突破。execution_mode=agent。",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(
      /support|resistance|fib|支撑|阻力|fibonacci/,
    );
    const p = await getLatestStrategyProposal(api, ["sol", "support"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // levels math
    expect(py.toLowerCase()).toMatch(/high|low|fib|0\.618|level/);
  });

  test("C-AT5 — multi-factor confluence (most ticks should skip)", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "做个稳健 BTC 1h 策略：MACD 金叉 + RSI 40-60 + 当根量能 > MA20 量能 * 1.5",
        "三个条件全满足才让 Agent 决策，否则 skip。execution_mode=agent。",
      ].join(" "),
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/confluence|共振|all.*condition/i);
    const p = await getLatestStrategyProposal(api, ["btc", "confluence"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // Skip path must be present
    expect(py).toMatch(/StrategyAgentTask\.skip\(/);
    // All three signal hints present
    expect(py.toLowerCase()).toMatch(/macd/);
    expect(py.toLowerCase()).toMatch(/rsi/);
    expect(py.toLowerCase()).toMatch(/volume|vol/);
  });

  test("C-AT6 — technical + news layering (attached_skills includes news_social)", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "ETH 1h，MACD 金叉触发时让 Agent 先查最近 6 小时 ETH 新闻和监管事件再决策。execution_mode=agent。",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/news|新闻/);
    const p = await getLatestStrategyProposal(api, ["eth", "macd"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // attached_skills should include news_social or the prompt should reference it
    expect(py).toMatch(/news_social|recent_news|news skill|新闻/);
  });

  test("C-AT7 — funding-rate arbitrage signal", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "BTCUSDT 永续，funding 8h > 0.05% 时让 Agent 判断是否开空套利（用现货对冲）。execution_mode=agent。",
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/funding|资金费|套利/);
    const p = await getLatestStrategyProposal(api, ["btc", "funding"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    expect(py.toLowerCase()).toMatch(/funding|fund_rate|funding_rate/);
  });

  test("C-AT9 — wallet whale follow signal", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "BSC 链上 meme 币策略：当大鲸地址（>$1M 净值）5 分钟净流入 > $200k 时让 Agent 决定是否跟单（先做 security_token_scan）。execution_mode=agent。",
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/whale|鲸|smart.?money|聪明钱/);
    const p = await getLatestStrategyProposal(api, ["meme", "whale"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // wallet/onchain references
    expect(py.toLowerCase()).toMatch(
      /wallet|token_top_trader|token_trades|security_token_scan|onchain/,
    );
  });

  test("C-AT10 — multi-timeframe confluence", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "做个保守 BTC 长线策略：1d MACD 金叉 + 4h RSI > 50 + 1h close > EMA200。",
        "三个时间框架方向一致才让 Agent 决策。execution_mode=agent。",
      ].join(" "),
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/mtf|时间框架|multi.?timeframe/);
    const p = await getLatestStrategyProposal(api, ["btc", "mtf"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // Three timeframes must appear
    expect(py).toMatch(/1d/);
    expect(py).toMatch(/4h/);
    expect(py).toMatch(/1h/);
  });

  test("C-AT11 — script-only signal, Agent decides size", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "策略脚本只发 direction+confidence 信号给 Agent，",
        "让 Agent 自己根据 portfolio 风险预算决定 size_usd（单笔上限 NAV 5%）。execution_mode=agent。",
      ].join(" "),
      { timeoutMs: 300_000 },
    );
    expect(reply.toLowerCase()).toMatch(/size|仓位|risk.*budget|预算/);
    const p = await getLatestStrategyProposal(api, ["confidence"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    // Prompt should reference portfolio and explicit size cap
    expect(py).toMatch(/portfolio|nav|risk[_ ]?budget|5\s*%/i);
  });

  test("C-AT14 — Donchian + ATR + KDJ custom indicators", async ({
    page,
    openChat,
    chatSend,
    api,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      [
        "BTC 1h，用 Donchian Channel(20) 突破 + ATR(14) 计算止损 + KDJ 过滤超买。",
        "全部满足让 Agent 仲裁。execution_mode=agent。",
      ].join(" "),
      { timeoutMs: 360_000 },
    );
    expect(reply.toLowerCase()).toMatch(/donchian|atr|kdj/);
    const p = await getLatestStrategyProposal(api, ["btc", "donchian"]);
    assertAgentExecutionMode(p);
    const py = mainPyOf(p);
    assertAgentTaskApiUsed(py);
    expect(py.toLowerCase()).toMatch(/donchian|highest.*20|lowest.*20/);
    expect(py.toLowerCase()).toMatch(/atr|true.?range/);
    expect(py.toLowerCase()).toMatch(/kdj|stochastic/);
  });
});
