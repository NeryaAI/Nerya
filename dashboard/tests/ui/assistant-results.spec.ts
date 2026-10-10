import {test,expect} from "@playwright/test";
import {readFileSync} from "node:fs";

test.beforeEach(async({page})=>{
  if (!process.env.NERYA_ASSISTANTS_QA_AUTH_FILE) return;
  const auth=JSON.parse(readFileSync(process.env.NERYA_ASSISTANTS_QA_AUTH_FILE!,"utf8"));
  await page.addInitScript(({token,expires_at})=>{
    localStorage.setItem("nerya.admin_jwt.v1",token);
    localStorage.setItem("nerya.admin_jwt_expires_at.v1",String(expires_at));
  },auth);
});

test("results open from the top-right toolbar and preview without navigating chat",async({page})=>{
  const errors:string[]=[];page.on("pageerror",error=>errors.push(error.message));
  await page.goto("/chat?assistant=main");
  await expect(page).toHaveURL(/\/chat\/assistant_/);
  await expect(page.getByTestId("assistant-navigation").getByTestId("assistant-results")).toHaveCount(0);
  const trigger=page.getByTestId("open-assistant-results");
  await expect(trigger).toBeVisible();
  const box=await trigger.boundingBox();expect(box!.x).toBeGreaterThan(1000);expect(box!.y).toBeLessThan(120);
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded","true");
  const panel=page.locator("#task-dock-panel-reports");await expect(panel).toBeVisible();
  const source=await page.getByTestId("workspace-source").boundingBox();const pane=await panel.boundingBox();
  expect(pane!.x).toBeGreaterThanOrEqual(source!.x+source!.width);
  const url=page.url();
  await panel.locator('[data-testid="assistant-result"][data-state="succeeded"]').filter({hasText:"均线策略草案"}).click();
  await expect(panel.getByTestId("canvas-result-body")).toContainText("prp_d132db22583f");
  await expect(page).toHaveURL(url);
  await expect(panel.getByRole("link",{name:"Open task conversation",exact:true})).toHaveAttribute("href",/taskchat_/);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/results-top-right-desktop.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("opening, selecting and closing results preserves draft and never submits commands",async({page})=>{
  await page.goto("/chat?assistant=main");
  await expect(page.getByTestId("open-assistant-results")).toBeVisible();
  const url=page.url(),writes:string[]=[];
  page.on("request",request=>{if(request.method()!=="GET"&&request.url().includes("/agent/commands"))writes.push(request.url());});
  const input=page.getByTestId("chat-workbench").locator("textarea");
  await input.fill("结果面板开关不能丢掉这段草稿。");
  const trigger=page.getByTestId("open-assistant-results");await trigger.click();
  await page.getByTestId("assistant-results").locator('[data-testid="assistant-result"][data-state="succeeded"]').filter({hasText:"整理验收资料"}).click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("投研");
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded","false");
  await expect(input).toHaveValue("结果面板开关不能丢掉这段草稿。");
  await expect(page).toHaveURL(url);
  await page.reload();await expect(input).toHaveValue("结果面板开关不能丢掉这段草稿。");
  expect(writes).toEqual([]);
});

test("keyboard can open the result list and return to conversation",async({page})=>{
  await page.goto("/chat?assistant=main");
  const trigger=page.getByTestId("open-assistant-results");await trigger.focus();await page.keyboard.press("Enter");
  await expect(page.locator("#task-dock-panel-reports")).toBeVisible();
  await expect(page.getByRole("tab",{name:"Reports and results",exact:true})).toBeFocused();
  await trigger.focus();await page.keyboard.press("Enter");
  await expect(trigger).toHaveAttribute("aria-expanded","false");
  await expect(page.getByTestId("composer-dock")).toBeVisible();
});

test.describe("Chinese mobile result pane",()=>{
  test.use({locale:"zh-CN",viewport:{width:390,height:844}});
  test("top-right control opens results and returns to the unchanged conversation",async({page})=>{
    await page.goto("/chat?assistant=main");
    await expect(page).toHaveURL(/\/chat\/assistant_/);
    const url=page.url();const trigger=page.getByTestId("open-assistant-results");
    await expect(trigger).toHaveAttribute("aria-label","报告与结果");
    await trigger.click();
    await expect(page.locator("#task-dock-panel-reports")).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
    await page.getByTestId("assistant-results").locator('[data-testid="assistant-result"][data-state="succeeded"]').filter({hasText:"整理验收资料"}).click();
    await expect(page.getByTestId("canvas-result-body")).toContainText("投研");
    await page.screenshot({path:"/tmp/nerya-assistants-opendots/results-top-right-mobile.png",fullPage:true});
    await trigger.click();
    await expect(page.getByTestId("composer-dock")).toBeVisible();
    await expect(page).toHaveURL(url);
  });
});
