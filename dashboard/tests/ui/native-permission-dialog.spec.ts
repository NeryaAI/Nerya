import { test, expect } from "@playwright/test";
import { parityFixture } from "./command-fixture";

const SID = "parity-session";
const ID = "tool_batch_fixture_permission";

const permission = (sessionId = SID) => ({
  record: {
    approval_id: ID, id: ID, kind: "tool_permission_batch", state: "pending",
    requester_session_id: sessionId, session_id: sessionId,
    turn_id: "turn_approval_fixture", requester_actor_id: "fixture-user",
    items: [{tool_use_id:"tool_1",reason:"Network execution needs review",
      tool: {name: "run_shell", skill_id:"native",call_id:"tool_1"},
      payload:{arguments:{command:"echo reviewed"}}}],
  },
  prompt: {
    approval_id: ID, text: "One native tool requires permission",
    buttons: [
      {label:"Allow once", callback_data:`approve:${ID}`,style:"primary"},
      {label:"Deny", callback_data:`reject:${ID}`,style:"danger"},
    ],
  },
});

test("pending native permission opens a modal and approves only the exact request", async ({page}) => {
  const fixture = await parityFixture(page);
  let pending = true;
  const callbacks: unknown[] = [];
  await page.route("**/api/proxy/approvals/pending", route => route.fulfill({json:{
    ok:true,approvals:pending?[permission()]:[],count:pending?1:0,
  }}));
  await page.route("**/api/proxy/approvals/callback", async route => {
    callbacks.push(route.request().postDataJSON());
    pending=false;
    await route.fulfill({json:{ok:true,approval_id:ID,approval_ids:[ID],state:"approved",resume:{ok:true}}});
  });
  await page.goto(`/chat/${SID}`);
  const dialog = page.getByTestId("native-permission-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Permission required");
  await expect(dialog).toContainText("run_shell");
  await expect(dialog).toContainText("echo reviewed");
  await dialog.getByRole("button",{name:"Allow once"}).click();
  await expect(dialog).toHaveCount(0);
  expect(callbacks).toEqual([{callback_data:`approve:${ID}`,actor_id:"dashboard"}]);
  expect(fixture.errors).toEqual([]);
});

test("dismissed permission modal stays available inline without reprompting", async ({page}) => {
  await parityFixture(page);
  await page.route("**/api/proxy/approvals/pending",route => route.fulfill({json:{ok:true,approvals:[permission()],count:1}}));
  await page.goto(`/chat/${SID}`);
  const dialog = page.getByTestId("native-permission-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button",{name:"Review later"}).click();
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(2700); // a second approval snapshot must not reopen a dismissed modal
  await expect(dialog).toHaveCount(0);
});

test("permission for a different session never interrupts the current chat", async ({page}) => {
  await parityFixture(page);
  await page.route("**/api/proxy/approvals/pending",route => route.fulfill({json:{ok:true,approvals:[permission("other-session")],count:1}}));
  await page.goto(`/chat/${SID}`);
  await expect(page.locator("[data-chat-composer] textarea")).toBeVisible();
  await expect(page.getByTestId("native-permission-dialog")).toHaveCount(0);
});
