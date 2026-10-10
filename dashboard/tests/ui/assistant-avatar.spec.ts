import {test,expect,type Page} from "@playwright/test";
let aid:string;
const hidden={role:"Preserve role",model_provider:"agnes-ai",model_id:"agnes-3.0-flash",research_allowed:false,memory_allowed:false,browser_allowed:false,files_allowed:false,shell_allowed:false,tool_policy:{allow_groups:[],deny:["run_shell"]}};
test.beforeAll(async({request})=>{
 const r=await request.post("/api/proxy/assistants/save",{data:{profile:{name:"Avatar UI verification",instructions:"Keep this preference",...hidden}}});expect(r.ok()).toBeTruthy();aid=(await r.json()).assistant.id;
});
test.afterAll(async({request})=>{
 if(!aid)return;const inventory=await (await request.get("/api/proxy/assistants")).json();const p=inventory.assistants.find((p:{id:string})=>p.id===aid);if(p?.enabled)await request.post("/api/proxy/assistants/archive",{data:{id:aid,expected_revision:p.revision}});
});
async function editor(page:Page){
 await page.goto("/chat?assistant="+aid);await expect(page).toHaveURL(/\/chat\/assistant_/);
 await page.getByTestId("assistant-chat-header").getByRole("button",{name:"Edit assistant",exact:true}).click();
 const dialog=page.getByTestId("assistant-editor");await expect(dialog).toBeVisible();return dialog;
}
async function image(page:Page,format="image/png"){
 const value=await page.evaluate(format=>{const c=document.createElement("canvas");c.width=80;c.height=50;const ctx=c.getContext("2d")!;ctx.fillStyle="#7058c5";ctx.fillRect(0,0,80,50);ctx.fillStyle="#ecdb7a";ctx.fillRect(15,5,50,40);return c.toDataURL(format);},format);
 return Buffer.from(value.split(",")[1],"base64");
}
async function profile(page:Page){const d=await (await page.request.get("/api/proxy/assistants")).json();return d.assistants.find((p:{id:string})=>p.id===aid);}

test("preset picker replaces avatar text input and preserves model/permission settings",async({page})=>{
 const dialog=await editor(page);
 const portraits=dialog.locator('img[src^="/branding/agents/"]');await expect(portraits).toHaveCount(20);
 await expect.poll(()=>portraits.evaluateAll(images=>images.every(img=>(img as HTMLImageElement).naturalWidth===256))).toBeTruthy();
 await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-avatar-settings-desktop.png",fullPage:true});
 await expect(dialog.getByLabel("Avatar",{exact:true})).toHaveCount(0);await expect(dialog.getByRole("checkbox")).toHaveCount(0);
 await expect(dialog.getByText("Tool permissions",{exact:true})).toHaveCount(0);await expect(dialog.getByText("Tool policy",{exact:true})).toHaveCount(0);await expect(dialog.getByText("Model",{exact:true})).toHaveCount(0);
 await dialog.getByText("Researcher",{exact:true}).click();await expect(dialog.getByRole("radio",{name:"Researcher",exact:true})).toBeChecked();
 const save=page.waitForResponse(r=>r.url().endsWith("/api/proxy/assistants/save")&&r.request().method()==="POST");await dialog.getByRole("button",{name:"Save assistant",exact:true}).click();
 const response=await save;expect(Object.keys(response.request().postDataJSON().profile).sort()).toEqual(["avatar","instructions","name"]);
 await expect(dialog).toHaveCount(0);const p=await profile(page);expect(p.avatar).toBe("role:researcher");for(const [key,value]of Object.entries(hidden))expect(p[key]).toEqual(value);
 await page.reload();
 for(const container of [page.getByTestId("assistant-chat-header"),page.getByTestId("assistant-select").filter({hasText:"Avatar UI verification"})]) {
  const avatar=container.locator('img[src="/branding/agents/researcher.png"]');await expect(avatar).toBeVisible();await expect.poll(()=>avatar.evaluate(img=>(img as HTMLImageElement).naturalWidth)).toBe(256);
 }
 await page.getByTestId("open-assistant-panel").click();await expect(page.getByTestId("assistant-overview-panel").locator('img[src="/branding/agents/researcher.png"]')).toBeVisible();
});

test("uploaded image is normalized, persisted and rendered across assistant surfaces",async({page})=>{
 const dialog=await editor(page);await dialog.getByLabel("Upload image",{exact:true}).setInputFiles({name:"avatar.jpg",mimeType:"image/jpeg",buffer:await image(page,"image/jpeg")});
 await expect(dialog.locator('img[src^="data:image/png;base64,"]').first()).toBeVisible();await expect(dialog.getByRole("radio",{name:"Uploaded",exact:true})).toBeChecked();
 await dialog.getByRole("button",{name:"Save assistant",exact:true}).click();await expect(dialog).toHaveCount(0);
 const p=await profile(page);expect(p.avatar).toMatch(/^data:image\/png;base64,/);expect(p.avatar.length).toBeLessThan(131100);
 for(const [key,value]of Object.entries(hidden))expect(p[key]).toEqual(value);
 await page.reload();const header=page.getByTestId("assistant-chat-header").locator('img[src^="data:image/png;base64,"]');await expect(header).toBeVisible();
 await expect.poll(()=>header.evaluate(img=>(img as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
 await expect(page.getByTestId("sidebar-assistants").locator('img[src^="data:image/png;base64,"]')).toBeVisible();
 await page.getByTestId("open-assistant-panel").click();await expect(page.getByTestId("assistant-overview-panel").locator('img[src^="data:image/png;base64,"]')).toBeVisible();
});

test("cancel and invalid uploads retain saved avatar and conversation draft",async({page})=>{
 const dialog=await editor(page);const before=await profile(page);
 await dialog.getByLabel("Upload image",{exact:true}).setInputFiles({name:"bad.svg",mimeType:"image/svg+xml",buffer:Buffer.from("<svg/>")});await expect(dialog.getByRole("alert")).toContainText("Choose a PNG");
 await dialog.getByLabel("Upload image",{exact:true}).setInputFiles({name:"broken.png",mimeType:"image/png",buffer:Buffer.from("not an image")});await expect(dialog.getByRole("alert")).toContainText("could not be read");
 await dialog.getByLabel("Upload image",{exact:true}).setInputFiles({name:"large.png",mimeType:"image/png",buffer:Buffer.alloc(2*1024*1024+1)});await expect(dialog.getByRole("alert")).toContainText("under 2 MB");
 await dialog.getByRole("button",{name:"Cancel",exact:true}).click();const input=page.getByTestId("chat-workbench").locator("textarea");await input.fill("头像设置不能丢掉这段草稿");const url=page.url();
 await page.getByTestId("assistant-chat-header").getByRole("button",{name:"Edit assistant",exact:true}).click();const reopened=page.getByTestId("assistant-editor");await reopened.getByText("Coder",{exact:true}).click();await reopened.getByRole("button",{name:"Cancel",exact:true}).click();
 expect((await profile(page)).avatar).toBe(before.avatar);await expect(input).toHaveValue("头像设置不能丢掉这段草稿");await expect(page).toHaveURL(url);
});

test.describe("mobile avatar settings",()=>{
 test.use({locale:"zh-CN",viewport:{width:390,height:844}});
 test("presets support keyboard selection and the simple dialog fits on mobile",async({page})=>{
  await page.goto("/chat?assistant="+aid);await expect(page.getByTestId("assistant-select").filter({hasText:"Avatar UI verification"})).toBeVisible();
  await page.getByTestId("assistant-chat-header").getByRole("button",{name:"编辑助理",exact:true}).click();const dialog=page.getByTestId("assistant-editor");await expect(dialog).toBeVisible();
  await dialog.getByRole("radio",{name:"Nerya",exact:true}).focus();await page.keyboard.press("Space");await expect(dialog.getByRole("radio",{name:"Nerya",exact:true})).toBeChecked();
  await page.keyboard.press("ArrowRight");await expect(dialog.getByRole("radio",{name:"主控",exact:true})).toBeChecked();
  for(let i=0;i<19;i++) await page.keyboard.press("ArrowRight");
  await expect(dialog.getByRole("radio",{name:"进化",exact:true})).toBeChecked();
  await expect(dialog.getByText("进化",{exact:true})).toBeInViewport();
  await dialog.getByText("进化",{exact:true}).click();
  const grid=dialog.getByTestId("assistant-avatar-picker").locator("div").first();expect(await grid.evaluate(el=>el.scrollHeight>el.clientHeight&&el.clientHeight<=256)).toBeTruthy();
  await dialog.getByText("主控",{exact:true}).click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();await expect(dialog.getByRole("button",{name:"保存助理",exact:true})).toBeVisible();
  await page.screenshot({path:"/tmp/nerya-assistants-opendots/assistant-avatar-settings-mobile.png",fullPage:true});await dialog.getByRole("button",{name:"取消",exact:true}).click();
 });
});
