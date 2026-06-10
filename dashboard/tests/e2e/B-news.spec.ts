/**
 * Group B — News / social fetch.
 * Maps to B1..B12 in nerya-prompt-test-plan.md.
 *
 * NOTE: B7 (CryptoPanic) and B8 (Wu Blockchain) are intentional NEGATIVE tests.
 */
import { test, expect } from "./fixtures";

test.describe("B — News & social", () => {
  test("B1 — crypto RSS returns CoinDesk/Cointelegraph items", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(page, "给我看看最近 3 小时的加密新闻", {
      timeoutMs: 120_000,
    });
    expect(reply.toLowerCase()).toMatch(
      /coindesk|cointelegraph|bitcoinmagazine/,
    );
  });

  test("B2 — Yahoo RSS for AAPL & NVDA tickers", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(page, "AAPL 和 NVDA 今天有什么消息？");
    expect(reply).toMatch(/AAPL/);
    expect(reply).toMatch(/NVDA|Nvidia/i);
    // Yahoo source attribution should appear somewhere
    expect(reply.toLowerCase()).toMatch(/yahoo|finance/);
  });

  test("B3 — Reddit hot threads", async ({ page, openChat, chatSend }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "Reddit 上 r/CryptoCurrency 最近热议什么？",
    );
    expect(reply.toLowerCase()).toMatch(/reddit|r\/cryptocurrency/);
  });

  test("B4 — X / Twitter site search (no native API)", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(page, "搜一下 X 上关于 $SOL 的热门讨论");
    // Must NOT claim Twitter API access. Should reference x.com URLs.
    expect(reply.toLowerCase()).toMatch(/x\.com|twitter\.com/);
  });

  test("B7 — CryptoPanic negative: graceful unsupported answer", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(page, "用 CryptoPanic 拉一下 BTC 利好新闻");
    // Reply should either route to RSS fallback OR honestly declare unsupported.
    // It MUST NOT invent CryptoPanic-specific structured fields.
    expect(reply).not.toMatch(/"votes":\s*\{/);
    expect(reply.toLowerCase()).toMatch(
      /cryptopanic|未集成|not available|fallback|rss|公开/i,
    );
  });

  test("B8 — Wu Blockchain negative: not integrated", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(page, "把吴说区块链最新内容给我看下");
    // Should honestly note it's not integrated; should not fabricate.
    expect(reply.toLowerCase()).toMatch(
      /未集成|未支持|not integrated|not available|无法获取|fallback/i,
    );
  });

  test("B12 — fetch_url summarizes a real article", async ({
    page,
    openChat,
    chatSend,
  }) => {
    await openChat();
    const reply = await chatSend(
      page,
      "读一下 https://www.coindesk.com/markets 这个页面有什么内容",
      { timeoutMs: 120_000 },
    );
    expect(reply.length).toBeGreaterThan(100);
    expect(reply.toLowerCase()).toMatch(/coindesk|market|价格|crypto/);
  });
});
