import {test, expect} from "@playwright/test";
import {readFileSync} from "node:fs";

test.beforeEach(async ({page}) => {
  const auth = JSON.parse(readFileSync(process.env.NERYA_ASSISTANTS_QA_AUTH_FILE!, "utf8"));
  await page.addInitScript(({token, expires_at}) => {
    localStorage.setItem("nerya.admin_jwt.v1", token);
    localStorage.setItem("nerya.admin_jwt_expires_at.v1", String(expires_at));
  }, auth);
});

async function completedResult(page: import("@playwright/test").Page, title: string) {
  const trigger=page.getByTestId("open-assistant-results");
  if(await trigger.getAttribute("aria-expanded")!=="true")await trigger.click();
  const row=page.getByTestId("assistant-results").locator('[data-testid="assistant-result"][data-state="succeeded"]').filter({hasText:title}).first();
  await expect(row).toBeVisible({timeout:180000});await row.click();
  const link=page.getByRole("link",{name:"Open task conversation",exact:true});
  const path=await link.getAttribute("href");return {row,link,path};
}

test("fresh workspace has one default assistant and another requires manual creation", async ({page}) => {
  await page.goto("/chat?assistant=main");
  await expect(page.getByTestId("assistant-select")).toHaveCount(1);
  await expect(page.getByTestId("sidebar-assistants")).not.toContainText("研究助理");
  for (const surface of ["assistant-chat-header", "sidebar-assistants", "assistant-empty-conversation"]) {
    const avatar = page.getByTestId(surface).locator('img[src="/branding/Nerya.png"]');
    await expect(avatar).toBeVisible();
    await expect.poll(() => avatar.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  }
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/default-main-mascot-desktop.png",fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByTestId("assistant-select")).toHaveCount(1);
  await expect(page.getByTestId("composer-dock")).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/default-main-mascot-mobile.png",fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByTestId("assistant-chat-header").getByRole("button",{name:"New assistant",exact:true}).click();
  const editor=page.getByTestId("assistant-editor");
  await editor.getByLabel("Name",{exact:true}).fill("研究助理");
  await editor.getByText("Researcher",{exact:true}).click();
  await editor.getByRole("button",{name:"Save assistant",exact:true}).click();
  await expect(editor).not.toBeVisible();
  await expect(page.getByTestId("assistant-select")).toHaveCount(2);
  await expect(page.getByTestId("assistant-select").filter({hasText:"研究助理"}).locator('img[src="/branding/agents/researcher.png"]')).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("assistant-select").filter({hasText:"研究助理"}).locator('img[src="/branding/agents/researcher.png"]')).toBeVisible();
});

test("assistants sit above their conversations and the main surface is chat", async ({page}) => {
  const errors: string[]=[];page.on("pageerror", error=>errors.push(error.message));
  await page.goto("/assistants");
  await expect(page).toHaveURL(/\/chat\/assistant_[a-f0-9]+/);
  await expect(page.getByTestId("assistant-chat-header")).toBeVisible();
  await expect(page.getByTestId("assistant-select")).toHaveCount(2);
  await expect(page.getByTestId("assistant-empty-conversation")).toBeVisible();
  await expect(page.getByTestId("composer-dock")).toBeVisible();
  await expect(page.getByRole("heading",{name:"Gateway channels"})).not.toBeVisible();
  const assistants=await page.getByTestId("sidebar-assistants").boundingBox();
  const conversations=await page.getByTestId("workbench-tasks").boundingBox();
  expect(assistants!.y+assistants!.height).toBeLessThanOrEqual(conversations!.y);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/corrected-assistants-desktop.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("the assistant delegates a persistent task and receives its conclusion as a bubble",async({page})=>{
  test.setTimeout(180000);await page.goto("/chat?assistant=main");
  await expect(page.getByTestId("assistant-chat-header")).toContainText("Nerya");
  await page.getByTestId("chat-workbench").locator("textarea").fill("请做一份题为「整理验收资料」的投研备忘录，只读取 notes/assistant-proof.txt，根据原文整理三点结论及证据，不要访问外网或执行交易。");
  const assistantUrl=page.url();await page.getByTestId("native-command-send").click();
  const result=await completedResult(page,"整理验收资料");expect(result.path).not.toBe(new URL(assistantUrl).pathname);
  await expect(page.getByTestId("execution-process")).toHaveCount(0);
  await expect(page.getByTestId("assistant-task")).toHaveCount(0);
  await expect(page.getByTestId("canvas-result-body")).toContainText(/持久|persistent|task/i);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/corrected-task-in-conversation.png",fullPage:true});
  await page.reload();const persisted=await completedResult(page,"整理验收资料");expect(persisted.path).toBe(result.path);
  await expect(page.getByTestId("assistant-select")).toHaveCount(2);
  await expect(page.getByTestId("assistant-chat-header").locator('img[src="/branding/Nerya.png"]')).toBeVisible();
  await expect(page).toHaveURL(assistantUrl);await persisted.link.click();
  await expect(page).toHaveURL(new URL(result.path!,assistantUrl).href);
  await expect(page.getByTestId("transcript-scroll")).toContainText("assistant-proof.txt");
  await expect(page.getByTestId("execution-process").first()).toBeVisible();
  await page.getByTestId("assistant-select").filter({hasText:"Nerya"}).click();await expect(page).toHaveURL(assistantUrl);
});

test("switching assistants filters the conversation list and settings stay in a menu", async ({page}) => {
  await page.goto("/chat?assistant=main");
  const header=page.getByTestId("assistant-chat-header");
  await header.getByTestId("assistant-select").filter({hasText:"研究助理"}).click();
  await expect(page).toHaveURL(/\/chat\/assistant_[a-f0-9]+/);
  await expect(header.getByTestId("assistant-select").filter({hasText:"研究助理"})).toHaveAttribute("aria-pressed","true");
  await expect(page.getByTestId("workbench-tasks")).not.toContainText("请在这段对话");
  const draft="这个草稿在打开和取消助理设置后应该保留。";
  const composer=page.getByTestId("chat-workbench").locator("textarea");
  await composer.fill(draft);
  const conversationUrl=page.url();
  await header.getByRole("button",{name:"Edit assistant",exact:true}).click();
  await expect(page.getByTestId("assistant-editor")).toBeVisible();
  await page.getByTestId("assistant-editor").getByRole("button",{name:"Cancel",exact:true}).click();
  await expect(page.getByTestId("assistant-editor")).not.toBeVisible();
  await expect(page.getByTestId("composer-dock")).toBeVisible();
  await expect(page).toHaveURL(conversationUrl);
  await expect(composer).toHaveValue(draft);
  await page.reload();
  await expect(composer).toHaveValue(draft);
});

test("sidebar assistant settings preserve the current conversation and draft",async({page})=>{
  await page.goto("/chat?assistant=main");
  await expect(page).toHaveURL(/\/chat\/[A-Za-z0-9_-]+/);
  await expect(page.getByTestId("assistant-task")).toHaveCount(0);
  const composer=page.getByTestId("chat-workbench").locator("textarea");
  const draft="打开侧栏设置时保留这段对话和草稿。";
  await composer.fill(draft);
  const conversationUrl=page.url();
  const settings=page.getByTestId("sidebar-assistants").getByRole("button",{name:"Settings for Nerya",exact:true});
  await settings.focus();
  await settings.click();
  await expect(page.getByTestId("assistant-editor")).toBeVisible();
  await page.getByTestId("assistant-editor").getByRole("button",{name:"Cancel",exact:true}).click();
  await expect(page).toHaveURL(conversationUrl);
  await expect(composer).toHaveValue(draft);
  await expect(page.getByTestId("assistant-task")).toHaveCount(0);
  await page.reload();
  await expect(page).toHaveURL(conversationUrl);
  await expect(composer).toHaveValue(draft);
  await expect(page.getByTestId("assistant-task")).toHaveCount(0);
});

test.describe("Chinese mobile chat",()=>{
  test.use({locale:"zh-CN",viewport:{width:390,height:844}});
  test("assistant header, dialogue and composer fit without a management grid",async({page})=>{
    await page.goto("/chat?assistant=main");
    await expect(page.getByTestId("assistant-chat-header")).toBeVisible();
    await expect(page.getByTestId("assistant-chat-header")).toContainText("Nerya");
    await expect(page.getByTestId("assistant-task")).toHaveCount(0);
    await expect(page.getByTestId("assistant-select")).toHaveCount(2);
    await expect(page.getByTestId("composer-dock")).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
    await page.screenshot({path:"/tmp/nerya-assistants-opendots/corrected-assistants-mobile.png",fullPage:true});
  });
});

test("strategy creation opens a task conversation and publishes its proposal in Results",async({page})=>{
  test.setTimeout(240000);await page.goto("/chat?assistant=main");const assistantUrl=page.url();
  await page.getByTestId("chat-workbench").locator("textarea").fill("创建一个可审批的 Python 策略提案，名称为「均线策略草案」：使用快线 5、慢线 20 的均线交叉，只用于单市场 paper 模式。使用现有技能和可用模板，编写参数、风控规则和最小验证，产出真实的 PatchProposal，保留待审批状态。不要执行交易、发布、安装、激活或联网获取行情。");
  await page.getByTestId("native-command-send").click();const result=await completedResult(page,"均线策略草案");
  expect(result.path).not.toBe(new URL(assistantUrl).pathname);await expect(page).toHaveURL(assistantUrl);
  await expect(page.getByTestId("execution-process")).toHaveCount(0);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/dual-chain-assistant-results.png",fullPage:true});
  await result.link.click();await expect(page).toHaveURL(new URL(result.path!,assistantUrl).href);
  await expect(page.getByTestId("transcript-scroll")).toContainText(/提案|proposal/i);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/dual-chain-strategy-conversation.png",fullPage:true});
});

test("a failed strategy task continues in its existing conversation and reports the recovered result",async({page})=>{
  test.skip(!process.env.NERYA_ASSISTANTS_RECOVERY_SESSION,"Opt-in recovery of the real strategy acceptance task.");test.setTimeout(240000);
  const sid=process.env.NERYA_ASSISTANTS_RECOVERY_SESSION!;await page.goto("/chat/"+sid);
  await page.getByTestId("chat-workbench").locator("textarea").fill("请继续当前任务，先检查已存在的均线策略草案提案 prp_d132db22583f。上次模型连接中断；现有 validation_report.json 指出 strategy.yml 的 backtest 设置 fee_bps、slippage_bps 不受支持。请使用现有策略技能修正这个配置错误并重新运行结构校验，保留待审批的提案；如必须生成修订提案请关联原提案。不要创建新的任务，不要重复创建账户，不要运行回测、联网、交易、激活或批准提案。确认真实的校验结果，再简洁报告提案编号、文件与待审批状态。");
  await page.getByTestId("native-command-send").click();await page.getByTestId("assistant-select").filter({hasText:"Nerya"}).click();
  await expect(page).toHaveURL(/\/chat\/assistant_[a-f0-9]+/);const result=await completedResult(page,"均线策略草案");
  expect(result.path).toBe("/chat/"+sid);await result.link.click();await expect(page).toHaveURL(new RegExp("/chat/"+sid+"$"));
  await expect(page.getByTestId("transcript-scroll")).toContainText(/提案|proposal/i);
});

test("recovered strategy result survives service restart with the same task identity",async({page})=>{
  test.skip(!process.env.NERYA_ASSISTANTS_RECOVERY_SESSION,"Opt-in persisted real strategy result check.");await page.goto("/chat?assistant=main");
  const result=await completedResult(page,"均线策略草案");expect(result.path).toBe("/chat/"+process.env.NERYA_ASSISTANTS_RECOVERY_SESSION);
  await result.link.click();await expect(page.getByTestId("transcript-scroll")).toContainText("prp_d132db22583f");
  await expect(page.getByTestId("transcript-scroll")).toContainText(/ok=true|校验/);
  await page.getByTestId("assistant-select").filter({hasText:"Nerya"}).click();await expect(page).toHaveURL(/\/chat\/assistant_[a-f0-9]+/);
  await page.reload();const persisted=await completedResult(page,"均线策略草案");expect(persisted.path).toBe(result.path);
});
