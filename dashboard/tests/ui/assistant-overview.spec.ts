import {test, expect, type Page} from "@playwright/test";

const sid="assistant_0d6e4079e36703ebd37c0072";
async function home(page:Page) {
 await page.goto("/chat/"+sid);
 await expect(page.getByTestId("open-assistant-panel")).toBeVisible();
 await expect(page.getByTestId("conversation-tasks")).toHaveCount(0);
 await expect(page.getByTestId("assistant-task")).toHaveCount(0);
}
async function open(page:Page) {
 await page.getByTestId("open-assistant-panel").click();
 const panel=page.getByTestId("assistant-overview-panel");await expect(panel).toBeVisible();return panel;
}

test("recent activities are in a top-right popover in both assistant and task conversations",async({page})=>{
 await home(page);const panel=await open(page);
 await expect(panel.getByRole("heading",{name:"Nerya",exact:true})).toBeVisible();
 await expect(panel.getByRole("heading",{name:"Recent activity",exact:true})).toBeVisible();
 await expect(panel.getByRole("heading",{name:"Outputs",exact:true})).toBeVisible();
 const activity=panel.getByTestId("assistant-activity").first();await expect(activity).toBeVisible();
 const link=activity.locator("a");const path=await link.getAttribute("href");expect(path).toMatch(/taskchat_/);
 await link.click();await expect(page).toHaveURL(new RegExp(path!+"$"));
 await expect(page.getByTestId("task-topbar")).toBeVisible();
 await expect(page.getByTestId("conversation-tasks")).toHaveCount(0);await expect(page.getByTestId("assistant-task")).toHaveCount(0);
 const taskPanel=await open(page);await expect(taskPanel.getByTestId("assistant-activity").first()).toBeVisible();
 await page.getByTestId("open-assistant-panel").click();await expect(taskPanel).toHaveCount(0);
});

test("reading position, transcript geometry and draft survive opening and closing assistant details",async({page})=>{
 const writes:string[]=[];page.on("request",r=>{if(r.method()!=="GET"&&r.url().includes("/agent/commands"))writes.push(r.method());});
 await home(page);let panel=await open(page);await expect(panel.getByTestId("assistant-output").first()).toBeVisible();await page.keyboard.press("Escape");
 const input=page.getByTestId("chat-workbench").locator("textarea");await input.fill("右侧面板不能推走正在阅读的消息");
 const scroll=page.getByTestId("transcript-scroll");await scroll.evaluate(el=>{el.scrollTop=200;});
 await expect(page.getByTestId("jump-to-latest")).toBeVisible();
 const before=await scroll.evaluate(el=>({top:el.scrollTop,height:el.scrollHeight}));const geometry=await page.getByTestId("workspace-source").boundingBox();
 const url=page.url();const trigger=page.getByTestId("open-assistant-panel");await trigger.focus();await page.keyboard.press("Enter");
 panel=page.getByTestId("assistant-overview-panel");await expect(panel).toBeVisible();
 const box=await panel.boundingBox();expect(box!.x).toBeGreaterThan(900);expect(box!.width).toBeLessThanOrEqual(381);expect(box!.height).toBeLessThanOrEqual(701);
 expect(await page.getByTestId("workspace-source").boundingBox()).toEqual(geometry);
 const after=await scroll.evaluate(el=>({top:el.scrollTop,height:el.scrollHeight}));expect(Math.abs(after.top-before.top)).toBeLessThanOrEqual(1);expect(after.height).toBe(before.height);
 await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-overview-desktop.png",fullPage:true});
 await page.keyboard.press("Escape");await expect(panel).toHaveCount(0);await expect(trigger).toBeFocused();
 await expect(input).toHaveValue("右侧面板不能推走正在阅读的消息");await expect(page).toHaveURL(url);expect(writes).toEqual([]);
});

test("output selection opens the existing preview and its list stays open after returning",async({page})=>{
 await home(page);const panel=await open(page);const output=panel.getByTestId("assistant-output").first();await expect(output).toBeVisible();
 const title=await output.getAttribute("title");const url=page.url();await output.click();await expect(panel).toHaveCount(0);
 await expect(page.getByTestId("canvas-result-body")).toBeVisible();await expect(page.getByTestId("canvas-result-body")).not.toBeEmpty();await expect(page).toHaveURL(url);
 await page.getByRole("button",{name:"Show all results",exact:true}).click();
 await expect(page.getByTestId("assistant-result").first()).toBeVisible();
 const refreshed=page.waitForResponse(r=>r.url().includes("/api/proxy/assistants/results?")&&r.request().method()==="GET");await refreshed;
 await expect(page.getByTestId("assistant-result").first()).toBeVisible();await expect(page.getByTestId("canvas-result-body")).toHaveCount(0);
 expect(title).toBeTruthy();
});

test("loading failure can retry to an empty activity/output state",async({page})=>{
 let recovered=false;
 await page.route("**/api/proxy/assistants/tasks?**",route=>route.fulfill({status:recovered?200:503,json:recovered?{ok:true,tasks:[]}:{ok:false,error:"unavailable"}}));
 await page.route("**/api/proxy/assistants/results?**",route=>route.fulfill({json:{ok:true,results:[]}}));
 await home(page);const panel=await open(page);const activity=panel.getByTestId("assistant-activities");
 await expect(activity).toContainText("Could not load task progress.");recovered=true;await activity.getByRole("button",{name:"Reload",exact:true}).click();
 await expect(activity).toContainText("No recent activity");await expect(panel.getByTestId("assistant-outputs")).toContainText("No outputs yet");
});

test("a legacy task can stop and its saved output remains in the side panel",async({page})=>{
 let stopped=false,requests=0;
 await page.route("**/api/proxy/assistants/tasks?**",route=>route.fulfill({json:{ok:true,tasks:[
  {task_id:"ui-active",title:"Sidebar active task",kind:"background",state:stopped?"cancelled":"running"},
  {task_id:"ui-output",title:"Saved legacy output",kind:"background",state:"succeeded",output:{output:"旧任务的结果仍可查看。"}}
 ]}}));
 await page.route("**/api/proxy/assistants/tasks/stop",async route=>{requests++;const body=route.request().postDataJSON();expect(body).toEqual({session_id:sid,task_id:"ui-active"});stopped=true;await route.fulfill({json:{ok:true,cancel_requested:true}});});
 await home(page);const panel=await open(page);await panel.getByRole("button",{name:"Stop task: Sidebar active task",exact:true}).click();
 await expect(panel.getByTestId("assistant-activity").filter({hasText:"Sidebar active task"})).toHaveAttribute("data-state","cancelled");expect(requests).toBe(1);
 await panel.getByRole("button",{name:/Saved legacy output/}).first().click();await expect(panel.getByTestId("assistant-activity-detail")).toContainText("旧任务的结果仍可查看。");
 await expect(page.getByTestId("conversation-tasks")).toHaveCount(0);
});

test("task completion refreshes the assistant conversation with the popover closed",async({page})=>{
 let done=false;
 await page.route("**/api/proxy/assistants/tasks?**",route=>route.fulfill({json:{ok:true,tasks:[{task_id:"ui-delivery",title:"Closed-panel delivery",kind:"background",state:done?"succeeded":"running",command_id:"ui-delivery-command"}]}}));
 await page.route("**/api/proxy/agent/session/transcript?**",async route=>{
  const response=await route.fetch();const data=await response.json();
  if(done)data.messages.push({message_id:"ui-sidebar-feedback",role:"assistant",content:"侧栏关闭时的任务结论也会同步回来。",turn_id:"ui-sidebar-feedback",ts:new Date().toISOString(),meta:{}});
  await route.fulfill({response,json:data});
 });
 await home(page);await expect(page.getByTestId("assistant-overview-panel")).toHaveCount(0);done=true;
 await expect(page.getByTestId("transcript-scroll")).toContainText("侧栏关闭时的任务结论也会同步回来。",{timeout:15000});
 await expect(page.getByTestId("conversation-tasks")).toHaveCount(0);
});

test.describe("Chinese mobile assistant details",()=>{
 test.use({locale:"zh-CN",viewport:{width:390,height:844}});
 test("panel scrolls independently and its settings preserve the conversation",async({page})=>{
  await home(page);const input=page.getByTestId("chat-workbench").locator("textarea");await input.fill("侧栏设置不能丢掉我的草稿");const url=page.url();
  const panel=await open(page);await expect(panel.getByRole("heading",{name:"近期活动",exact:true})).toBeVisible();
  await expect(panel.getByRole("heading",{name:"输出内容",exact:true})).toBeVisible();const box=await panel.boundingBox();expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(390);expect(box!.height).toBeLessThanOrEqual(591);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-overview-mobile.png",fullPage:true});
  await panel.getByRole("button",{name:"编辑助理",exact:true}).click();await expect(page.getByTestId("assistant-editor")).toBeVisible();
  await page.getByRole("button",{name:"取消",exact:true}).click();await expect(input).toHaveValue("侧栏设置不能丢掉我的草稿");await expect(page).toHaveURL(url);
 });
});
