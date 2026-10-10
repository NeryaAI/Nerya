import {test, expect, type Page} from "@playwright/test";
import {writeFileSync} from "node:fs";

const evidence: Record<string, unknown> = {};
async function home(page: Page) {
  await page.goto("/chat?assistant=main");
  await expect(page).toHaveURL(/\/chat\/assistant_/);
  await expect(page.getByTestId("chat-workbench")).toHaveAttribute("data-conversation-kind", "assistant");
  await expect(page.locator('[data-composer-mode="assistant"]')).toBeVisible();
  await expect(page.getByTestId("task-topbar")).toHaveCount(0);
  await expect(page.getByTestId("execution-process")).toHaveCount(0);
  await expect(page.getByTestId("assistant-task")).toHaveCount(0);
  await expect(page.locator('[data-composer-options]')).toHaveCount(0);
}
async function send(page: Page, text: string) {
  const input=page.getByTestId("chat-workbench").locator("textarea");await input.fill(text);
  const button=page.getByTestId("native-command-send");await expect(button).toBeEnabled();
  const admitted=page.waitForResponse(r=>new URL(r.url()).pathname==="/api/proxy/agent/commands"&&r.request().method()==="POST");
  await button.click();const response=await admitted;const body=await response.json();
  expect(response.ok(),JSON.stringify({ok:body.ok,error:body.error})).toBeTruthy();
  const command=body.command;expect(command?.command_id, Object.keys(body).join(",")).toBeTruthy();
  expect(response.request().postDataJSON().request.conversation_kind).toBe("assistant");
  await expect.poll(async()=>{
    await expect(page.getByTestId("execution-process")).toHaveCount(0);
    const res=await page.request.get("/api/proxy/agent/commands?session_id="+encodeURIComponent(command.session_id));
    const data=await res.json();return data.commands?.find((c:{command_id:string})=>c.command_id===command.command_id)?.state;
  },{timeout:120000,intervals:[1000,2000,5000]}).toBe("succeeded");
  await expect(page.locator('[data-testid="assistant-reply-bubble"][data-command-id="'+command.command_id+'"]')).toHaveAttribute("data-turn-loading","false");
  return command;
}

test("anonymous preview sends real replies and follow-ups as final-only assistant bubbles",async({page})=>{
  test.setTimeout(240000);await home(page);
  expect(await page.evaluate(()=>localStorage.getItem("nerya.admin_jwt.v1"))).toBeNull();
  const first=await send(page,"请只回复：预览消息已恢复。不要创建任务、调用工具或写入记忆。");
  await expect(page.getByTestId("transcript-scroll")).toContainText("预览消息已恢复");
  const second=await send(page,"上一条让你确认了什么？一句话回答，不要创建任务或调用工具。");
  await expect(page.locator('[data-testid="assistant-reply-bubble"][data-command-id="'+second.command_id+'"]')).toContainText(/恢复|消息/);
  await expect(page.locator('[data-testid="assistant-reply-bubble"][data-command-id="'+second.command_id+'"]')).toHaveAttribute("data-turn-loading","false");
  await expect(page.getByTestId("execution-process")).toHaveCount(0);
  await page.reload();await home(page);
  await expect(page.locator('[data-testid="assistant-reply-bubble"][data-command-id="'+second.command_id+'"]')).toContainText(/恢复|消息/);
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-bubbles-desktop.png",fullPage:true});
  evidence.messages=[first.command_id,second.command_id];
  writeFileSync("/tmp/nerya-assistants-opendots/assistant-chat-verification.json",JSON.stringify(evidence,null,2));
});

test("complex research runs in a task chat and delivers only its conclusion to assistant chat",async({page})=>{
  test.setTimeout(240000);await home(page);const url=page.url();const title="气泡链路验收 "+Date.now();
  const command=await send(page,`请创建一个独立投研任务，标题为「${title}」：只读取 notes/assistant-proof.txt，根据原文整理三点结论及证据，完成后把结论同步给我。不要访问外网、写入文件或执行交易。`);
  const sid=new URL(url).pathname.split("/").at(-1)!;
  let task:{task_id:string;task_session_id:string;state:string}|undefined;
  await expect.poll(async()=>{
    const data=await (await page.request.get("/api/proxy/assistants/tasks?session_id="+sid)).json();
    task=data.tasks.find((t:{title:string})=>t.title.includes(title));return task?.state;
  },{timeout:150000,intervals:[1000,2000,5000]}).toBe("succeeded");
  expect(task!.task_session_id).not.toBe(sid);
  await expect(page.getByTestId("transcript-scroll")).toContainText(title);
  await expect(page.locator('[data-testid="assistant-reply-bubble"] a[href="/chat/'+task!.task_session_id+'"]')).toBeVisible({timeout:30000});
  await expect(page.getByTestId("execution-process")).toHaveCount(0);
  await expect(page.getByTestId("assistant-task")).toHaveCount(0);
  await expect(page).toHaveURL(url);
  await page.getByTestId("open-assistant-results").click();
  const result=page.getByTestId("assistant-result").filter({hasText:title});await expect(result).toHaveAttribute("data-state","succeeded");await result.click();
  await expect(page.getByTestId("canvas-result-body")).toContainText(/assistant-proof|证据|结论/);
  await page.getByRole("link",{name:"Open task conversation",exact:true}).click();
  await expect(page.getByTestId("chat-workbench")).toHaveAttribute("data-conversation-kind","task");
  await expect(page.getByTestId("task-topbar")).toBeVisible();
  await expect(page.getByTestId("execution-process").first()).toBeVisible();
  await expect(page.locator('[data-composer-mode="agent"]')).toBeVisible();
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-task-execution.png",fullPage:true});
  await page.getByTestId("assistant-select").filter({hasText:"Nerya"}).click();await expect(page).toHaveURL(url);await home(page);
  evidence.research={command_id:command.command_id,task};
  writeFileSync("/tmp/nerya-assistants-opendots/assistant-chat-verification.json",JSON.stringify(evidence,null,2));
});

test.describe("Chinese mobile assistant",()=>{
 test.use({locale:"zh-CN",viewport:{width:390,height:844}});
 test("retains draft and results without showing task controls",async({page})=>{
  await home(page);await expect(page.locator('[data-testid="assistant-select"]:visible')).toHaveCount(1);const input=page.getByTestId("chat-workbench").locator("textarea");await expect(input).toHaveAttribute("placeholder","给 Nerya 发消息…");await input.fill("气泡界面不能丢掉这条草稿");
  const url=page.url();await page.getByTestId("open-assistant-results").click();await expect(page.locator("#task-dock-panel-reports")).toBeVisible();
  await page.getByRole("button",{name:"返回对话",exact:true}).click();await expect(input).toHaveValue("气泡界面不能丢掉这条草稿");await expect(page).toHaveURL(url);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-bubbles-mobile.png",fullPage:true});
 });
});
