import { expect, test } from "@playwright/test";

const THREADS_KEY = "nerya.chat.threads.v1";

test("Canvas renders web search and JSON as structured views", async ({ page }) => {
  const now = Date.now();
  await page.addInitScript(
    ({ key, timestamp }) => {
      localStorage.setItem(
        key,
        JSON.stringify([
          {
            id: "canvas-fixture",
            title: "Canvas fixture",
            created_ts: timestamp,
            updated_ts: timestamp,
            messages: [
              {
                id: "assistant-canvas",
                role: "assistant",
                ts: timestamp,
                turn: {
                  reply_text: "Canvas fixture ready.",
                  blocks: [
                    {
                      block: {
                        kind: "tool_result",
                        action: "web_search",
                        payload: { query: "NVIDIA earnings" },
                        result: {
                          data: {
                            provider: "test-search",
                            results: [
                              {
                                title: "NVIDIA investor relations",
                                url: "https://investor.nvidia.com/results",
                                snippet: "Quarterly earnings and financial reports.",
                              },
                              {},
                            ],
                          },
                        },
                      },
                    },
                  ],
                  attachments: [
                    {
                      id: "json-report",
                      name: "report.json",
                      mime_type: "application/json",
                      size: 32,
                      text: '{"ticker":"NVDA","score":92}',
                    },
                  ],
                },
              },
            ],
          },
        ]),
      );
    },
    { key: THREADS_KEY, timestamp: now },
  );

  await page.goto("/chat/canvas-fixture");

  const canvas = page.getByRole("complementary", { name: /Canvas|画布/i });
  await expect(canvas).toBeVisible();
  await expect(canvas.getByText("NVIDIA earnings", { exact: true }).last()).toBeVisible();
  await expect(canvas.getByText("1 result", { exact: true })).toBeVisible();
  await expect(canvas.getByText("NVIDIA investor relations", { exact: true })).toBeVisible();
  await expect(canvas.getByText("investor.nvidia.com", { exact: true })).toBeVisible();

  await canvas.getByRole("button", { name: /report\.json/i }).click();
  await expect(canvas.getByRole("button", { name: "Structured" })).toBeVisible();
  await expect(canvas.getByText("ticker", { exact: true })).toBeVisible();
  await expect(canvas.getByText('"NVDA"', { exact: true })).toBeVisible();
});
