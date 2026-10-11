import { test, expect } from "@playwright/test";
import { parityFixture } from "./command-fixture";

const session = "parity-session";

/** The old recovery payload is intentionally part of the fixture: the UI
 * must not expose an MCP connection/auth form for historical tool failures. */
function rejectedConfigBlock(withLegacyReview: boolean) {
  return {
    kind: "tool_result",
    action: "evolve_core_config_patch",
    call_id: "config-tool-call",
    skill_id: "native",
    ok: false,
    error_kind: "permission_denied",
    error: "advisory reject: protected scope change refused. proposed patch touches protected scope nerya.yml:mcp",
    recovery: {
      reason: "protected_scope",
      ...(withLegacyReview ? {operator_review: {
        kind: "mcp_settings", target: "nerya.yml", scope: "nerya.yml:mcp",
        expected_revision: "a".repeat(64), requires_scope: "admin:ops",
        can_apply: true, changed_keys: ["mcp.enabled"], request: {enabled: true},
      }} : {}),
    },
  };
}

for (const withLegacyReview of [false, true]) {
  test(`ordinary Agent config reject never opens MCP auth (${withLegacyReview ? "legacy" : "current"} result)`, async ({page}) => {
    const fixture = await parityFixture(page);
    const mcpCalls: string[] = [];
    await page.route("**/api/proxy/mcp-settings**", async route => {
      mcpCalls.push(route.request().url());
      await route.fulfill({status: 403, json: {ok: false, error: "not_an_agent_chat_route"}});
    });
    await page.route("**/api/proxy/agent/session/transcript?**", route => route.fulfill({json: {
      ok: true, session_id: session, title: "Config rejection in Agent conversation",
      messages: [
        {message_id: "turn-review:user", role: "user", content: "Tune the provider", ts: 1720000000},
        {message_id: "turn-review:assistant", role: "assistant", content: "The protected config cannot be changed.",
          ts: 1720000020, turn_id: "turn-review", turn: {
            turn_id: "turn-review", final_text: "The requested configuration patch was rejected.",
            blocks: [{seq: 1, role: "tool", turn_id: "turn-review",
              message_id: "turn-review:assistant", block: rejectedConfigBlock(withLegacyReview)}],
          }},
      ], count: 2,
    }}));
    await page.goto(`/chat/${session}`);
    await expect(page.getByText(/protected scope change refused/).first()).toBeVisible();
    await expect(page.getByTestId("protected-mcp-review")).toHaveCount(0);
    await expect(page.getByTestId("native-permission-dialog")).toHaveCount(0);
    await expect(page.getByRole("button", {name: "Allow this MCP change"})).toHaveCount(0);
    await expect(page.locator("[data-chat-composer] textarea")).toBeEnabled();
    expect(mcpCalls).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}
