import { chooseOption } from "./choice-control";
import { test, expect, type Page } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import path from "node:path";
import type { WorkflowSummary, WorkflowView, WorkflowTemplate } from "../../lib/workflowTypes";

// Opt-in real integration. Never let this test write into a normal workspace.
const API = process.env.NERYA_WORKFLOW_TEST_API || "";
const enabled = /^http:\/\/(127\.0\.0\.1|localhost):18319$/.test(API);
const screenshots = path.resolve("test-results/workflow-demo/screenshots");
const examples: Partial<Record<WorkflowTemplate, { strategy_id: string; proposal_id: string }>> = {};
const templateIds: WorkflowTemplate[] = ["multi_script", "script_agent", "scheduler_agent"];
let browserAuth: { token: string; expires_at: number };

test.describe("resource-backed workflow integration", () => {
  test.skip(!enabled, "Requires the isolated workflow-demo runtime on port 18319");
  test.describe.configure({ mode: "serial" });
  test.setTimeout(90_000);

  test.beforeAll(async ({ request }) => {
    const workspace = await (await request.get(`${API}/workspace`)).json();
    expect(String(workspace.root)).toContain("/test-results/workflow-demo/workspace");
    expect(workspace.live_trading_enabled).toBe(false);
    // Next dev may mark loopback requests as forwarded. Authenticate normally
    // rather than weakening the production proxy's remote-request guard.
    const authStatus = await (await request.get(`${API}/auth/status`)).json();
    const passwordFile = path.resolve("test-results/workflow-demo/.test-password");
    mkdirSync(path.dirname(passwordFile), { recursive: true });
    if (!authStatus.password_configured) {
      const password = randomBytes(32).toString("base64url");
      writeFileSync(passwordFile, password, { mode: 0o600 });
      const configured = await (await request.post(`${API}/auth/admin/password`, { data: { new_password: password } })).json();
      expect(configured.ok).toBe(true);
    }
    expect(existsSync(passwordFile), "Only reuse this test's isolated password").toBe(true);
    const login = await (await request.post(`${API}/auth/login`, { data: { password: readFileSync(passwordFile, "utf8") } })).json();
    expect(login.ok).toBe(true);
    browserAuth = { token: login.token, expires_at: login.expires_at };
    const accounts = await (await request.get(`${API}/accounts/list`)).json();
    expect(accounts.accounts.some((row: { profile: { id: string; mode: string } }) => row.profile.id === "paper_main" && row.profile.mode === "paper")).toBe(true);
    const index = await (await request.get(`${API}/strategies/runtime/workflows`)).json();
    for (const template of templateIds) {
      const strategy_id = `workflow_demo_${template}`;
      const existing = (index.workflows as WorkflowSummary[]).find((row) => row.strategy_id === strategy_id && row.state === "draft");
      if (existing?.proposal_id) {
        examples[template] = { strategy_id, proposal_id: existing.proposal_id };
        continue;
      }
      const response = await request.post(`${API}/strategies/runtime/workflow/template`, { data: {
        template, strategy_id, accounts: ["paper_main"], markets: ["BINANCE:BTCUSDT"],
      } });
      expect(response.ok()).toBe(true);
      const out = await response.json();
      expect(out.ok, JSON.stringify(out)).toBe(true);
      expect(out.validation.ok).toBe(true);
      expect(out.workflow.manifest.mode).toBe("paper");
      expect(out.workflow.manifest.schedule.enabled).toBe(false);
      expect(out.workflow.manifest.tuning.schedule.enabled).toBe(false);
      examples[template] = { strategy_id: out.strategy_id, proposal_id: out.proposal_id };
    }
    mkdirSync(screenshots, { recursive: true });
    // Next dev compiles large routes lazily (the chat bundle can take >10s).
    // Compile destinations before measuring interactions; API calls below
    // still use the real isolated runtime and assertions keep their timeout.
    for (const route of ["/chat", "/self-evolution"]) {
      expect((await request.get(route)).ok()).toBe(true);
    }
  });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript((auth) => {
      localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: "zh", darkMode: "dark" }));
      localStorage.setItem("nerya.admin_jwt.v1", auth.token);
      localStorage.setItem("nerya.admin_jwt_expires_at.v1", String(auth.expires_at));
    }, browserAuth);
    await page.setViewportSize({ width: 1680, height: 1180 });
  });

  async function openExample(page: Page, template: WorkflowTemplate) {
    const item = examples[template]!;
    await page.goto(`/strategies?strategy_id=${item.strategy_id}&proposal_id=${item.proposal_id}`);
    await expect(page.getByRole("heading", { name: "策略工作流", exact: true })).toBeVisible();
    await expect(page.getByTestId("strategy-workflow-panel")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator("[data-workflow-node]").first()).toBeVisible();
    return item;
  }

  async function capture(page: Page, filename: string) {
    await page.evaluate(() => document.fonts.ready);
    // The app scrolls inside its shell; fullPage alone clips the lower canvas.
    // Capture the actual panel rather than just the browser viewport.
    const panel = page.getByTestId("strategy-workflow-panel");
    await panel.scrollIntoViewIfNeeded();
    if ((page.viewportSize()?.width || 0) >= 768) {
      await panel.screenshot({ path: path.join(screenshots, filename), animations: "disabled" });
    } else {
      await page.getByTestId("workflow-inspector").scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(screenshots, filename), fullPage: true, animations: "disabled" });
    }
  }

  for (const template of templateIds) {
    test(`${template}: real draft, connected cards and screenshot`, async ({ page, request }) => {
      const item = await openExample(page, template);
      const out = await (await request.get(`${API}/strategies/runtime/workflow`, { params: item })).json() as WorkflowView;
      await expect(page.locator("[data-workflow-node]")).toHaveCount(out.strategy.nodes.length);
      await expect(page.locator("[data-edge]")).toHaveCount(out.strategy.edges.length);
      // A long same-column dependency must not run through another card.
      const clearBranches = await page.locator('[data-routing="side"] > path:first-child').evaluateAll((paths) => {
        const cards = Array.from(document.querySelectorAll<HTMLElement>("[data-workflow-node]")).map((card) => ({
          x: parseFloat(card.style.left), y: parseFloat(card.style.top), width: parseFloat(card.style.width), height: parseFloat(card.style.height),
        }));
        return paths.every((element) => {
          const route = element as SVGPathElement;
          for (let i = 1; i < 40; i++) {
            const point = route.getPointAtLength(route.getTotalLength() * i / 40);
            if (cards.some((card) => point.x > card.x + 1 && point.x < card.x + card.width - 1 && point.y > card.y + 1 && point.y < card.y + card.height - 1)) return false;
          }
          return true;
        });
      });
      expect(clearBranches).toBe(true);
      const connected = new Set(out.strategy.edges.flatMap((edge) => [edge.source, edge.target]));
      expect(out.strategy.nodes.every((node) => connected.has(node.id))).toBe(true);
      expect(out.strategy.nodes.filter((node) => node.kind === "script").length).toBe(template === "multi_script" ? 4 : template === "script_agent" ? 3 : 1);
      expect(out.strategy.nodes.filter((node) => node.kind === "agent").length).toBe(template === "multi_script" ? 0 : 3);
      await expect(page.getByRole("button", { name: "保存提案", exact: true })).toBeDisabled();
      await capture(page, `${template}.png`);
    });
  }

  async function openNode(page: Page, id: string) {
    await page.locator(`[data-workflow-node="${id}"]`).getByRole("button", { name: /^编辑详情:/ }).click();
    await expect(page.getByTestId("workflow-inspector")).toBeVisible();
  }
  async function advancedJson(page: Page) {
    await page.getByRole("tab", { name: "高级设置", exact: true }).click();
    const editor = page.getByLabel("完整配置 JSON", { exact: true });
    if (!await editor.isVisible()) await page.locator("summary").filter({ hasText: /^完整配置 JSON$/ }).click();
    return editor;
  }
  async function saveProposal(page: Page): Promise<{ ok: boolean; state: string; workflow: WorkflowView }> {
    const response = page.waitForResponse((r) => r.url().includes("/workflow/propose") && r.request().method() === "POST");
    await page.getByRole("button", { name: "保存提案", exact: true }).click();
    const out = await (await response).json();
    expect(out.ok, JSON.stringify(out)).toBe(true);
    await expect(page.getByTestId("strategy-workflow-panel")).toHaveAttribute("aria-busy", "false");
    return out;
  }

  test("edit code, propose, reload and reject a stale revision", async ({ page, request }) => {
    const item = await openExample(page, "multi_script");
    await openNode(page, "script:signals.py");
    const editor = page.getByLabel("编辑文件内容", { exact: true });
    await expect(editor).not.toBeVisible();
    await page.getByText("查看或编辑代码", { exact: true }).click();
    const original = await editor.inputValue();
    const edited = `${original}\n# Workflow editor persistence regression\n`;
    await editor.fill(edited);
    await expect(page.getByText("有未保存修改", { exact: true })).toBeVisible();
    await capture(page, "node-editor.png");
    expect((await saveProposal(page)).state).toBe("pending_review");
    await page.reload();
    await expect(page.getByTestId("strategy-workflow-panel")).toHaveAttribute("aria-busy", "false");
    await openNode(page, "script:signals.py");
    await page.getByText("查看或编辑代码", { exact: true }).click();
    await expect(editor).toHaveValue(edited);
    const source = await (await request.get(`${API}/strategies/runtime/workflow`, { params: item })).json() as WorkflowView;
    expect(source.strategy.nodes.find((node) => node.id === "script:signals.py")?.content).toBe(original);
    const stale = await (await request.post(`${API}/strategies/runtime/workflow/propose`, { data: { ...item, base_revision: "stale", changes: [{ node_id: "scheduler:trading", config: { every_seconds: 1 } }] } })).json();
    expect(stale.ok).toBe(false);
    expect(stale.error).toContain("revision_conflict");
  });

  test("invalid JSON remains local and annotations do not execute", async ({ page }) => {
    await openExample(page, "script_agent");
    await openNode(page, "scheduler:trading");
    const editor = await advancedJson(page);
    await editor.fill("{broken");
    await page.getByRole("button", { name: "保存提案", exact: true }).click();
    await expect(page.getByRole("alert").first()).toBeVisible();
    await expect(editor).toHaveValue("{broken");
    await editor.fill('{"type":"interval","every_seconds":300,"enabled":false}');
    await page.getByRole("button", { name: "关闭详情", exact: true }).click();
    await page.getByRole("button", { name: "说明连线: signals.py", exact: true }).click();
    await page.getByRole("button", { name: "说明连线: risk_critic", exact: true }).click();
    await expect(page.getByText("这是说明关系，不会被执行器调度。实际调用关系请编辑脚本或配置。", { exact: true })).toBeVisible();
    await page.getByLabel("连线说明", { exact: true }).fill("核查信号边界");
    const out = await saveProposal(page);
    expect(out.workflow.strategy.edges.some((edge) => edge.label === "核查信号边界" && edge.origin === "annotation" && edge.relation === "annotation")).toBe(true);
  });

  test("review workflow and editable tuner with protected approvals", async ({ page }) => {
    await openExample(page, "scheduler_agent");
    await page.getByRole("tab", { name: "复盘进化", exact: true }).click();
    await expect(page.locator("[data-workflow-node]")).toHaveCount(8);
    await capture(page, "review-evolution.png");
    await openNode(page, "agent:tuner");
    await expect(page.getByLabel("编辑文件内容", { exact: true })).toBeEditable();
    await capture(page, "review-agent-editor.png");
    await openNode(page, "approval:operator");
    await expect(page.getByText("始终需要你的确认", { exact: true }).last()).toBeVisible();
    const editor = await advancedJson(page);
    await expect(editor).not.toBeEditable();
    await page.goto("/self-evolution");
    await expect(page.getByRole("tab", { name: "策略复盘工作流", exact: true })).toBeVisible();
    await expect(page.locator("[data-workflow-node]")).toHaveCount(8);
    await page.getByRole("tab", { name: "系统进化记录", exact: true }).click();
    await expect(page.getByText(/历史证据不可改写/)).toBeVisible();
  });

  test("review loading is distinct from empty and a failed read can be retried", async ({ page }) => {
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    let reads = 0;
    await page.route("**/strategies/runtime/workflows", async (route) => {
      if (++reads === 1) {
        await held;
        await route.fulfill({ status: 503, json: { ok: false, error: "simulated_read_failure" } });
      } else await route.continue();
    });
    try {
      await page.goto("/self-evolution");
      await expect(page.getByTestId("evolution-workflows-loading")).toBeVisible();
      await expect(page.getByText("还没有可展示的策略复盘配置。先创建一个策略工作流。", { exact: true })).not.toBeVisible();
      release();
      await page.getByRole("button", { name: "重新加载策略", exact: true }).click();
      await expect(page.getByTestId("strategy-workflow-panel")).toHaveAttribute("aria-busy", "false");
      await expect(page.locator("[data-workflow-node]")).toHaveCount(8);
    } finally { release(); }
  });

  test("a strategy deep link opens while the directory is still loading", async ({ page }) => {
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/strategies/runtime/workflows", async (route) => {
      await held;
      await route.continue();
    });
    try {
      await openExample(page, "script_agent");
      await openNode(page, "script:signals.py");
      await expect(page.getByTestId("workflow-inspector")).toBeVisible();
    } finally { release(); }
  });

  test("mobile gallery, editor and light theme do not overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openExample(page, "scheduler_agent");
    await page.getByRole("button", { name: "卡片一览", exact: true }).click();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    await openNode(page, "script:main.py");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    await capture(page, "mobile-editor.png");
    await page.addInitScript(() => localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: "zh", darkMode: "light" })));
    await page.setViewportSize({ width: 1680, height: 1180 });
    await page.reload();
    await expect(page.getByTestId("strategy-workflow-panel")).toHaveAttribute("aria-busy", "false");
    await capture(page, "light-workflow.png");
  });

  test("main Agent receives a draft, not an automatically sent request", async ({ page }) => {
    await openExample(page, "multi_script");
    const sent: string[] = [];
    page.on("request", (request) => { if (request.method() === "POST" && /run_turn|\/agent\/send|\/messages\/send/.test(request.url())) sent.push(request.url()); });
    await page.getByRole("button", { name: "让主 Agent 创建", exact: true }).click();
    await expect(page).toHaveURL(/\/chat/);
    await expect(page.locator("textarea:visible").first()).toHaveValue(/strategy_author/);
    expect(sent).toEqual([]);
  });

  test("schedule presets and daily rule persist without silently starting a job", async ({ page }) => {
    await openExample(page, "script_agent");
    await openNode(page, "scheduler:trading");
    await expect(page.getByLabel("完整配置 JSON", { exact: true })).not.toBeVisible();
    await page.getByRole("button", { name: "15 分钟", exact: true }).click();
    await expect(page.getByLabel("自定义间隔（秒）", { exact: true })).toHaveValue("900");
    await chooseOption(page.getByLabel("什么时候运行", { exact: true }), "daily");
    await page.getByLabel("每天几点", { exact: true }).fill("08:30");
    const saved = await saveProposal(page);
    expect(saved.workflow.manifest.schedule).toMatchObject({ type: "cron", cron: "30 8 * * *", enabled: false });
    await page.reload();
    await openNode(page, "scheduler:trading");
    await expect(page.getByLabel("每天几点", { exact: true })).toHaveValue("08:30");
  });

  test("review hourly schedule is understandable and approval stays protected in advanced editing", async ({ page }) => {
    await openExample(page, "scheduler_agent");
    await page.getByRole("tab", { name: "复盘进化", exact: true }).click();
    await openNode(page, "scheduler:tuning");
    await expect(page.getByRole("combobox", { name: "什么时候运行", exact: true })).toHaveAttribute("data-value", "hourly");
    await expect(page.getByRole("combobox", { name: "每隔多久复查一次", exact: true })).toHaveAttribute("data-value", "6");
    await expect(page.getByLabel("Cron 时间规则", { exact: true })).not.toBeVisible();
    await chooseOption(page.getByRole("combobox", { name: "每隔多久复查一次", exact: true }), "12");
    const saved = await saveProposal(page);
    expect((saved.workflow.manifest.tuning as { schedule: unknown }).schedule).toMatchObject({ type: "cron", cron: "0 */12 * * *", enabled: false });
    await openNode(page, "validation:tuning");
    await page.getByRole("tab", { name: "高级设置", exact: true }).click();
    await expect(page.getByRole("switch", { name: "require_operator_approval", exact: true })).toBeDisabled();
  });

  test("human-readable objectives preserve the primary-secondary configuration and custom instructions", async ({ page }) => {
    await openExample(page, "scheduler_agent");
    await page.getByRole("tab", { name: "复盘进化", exact: true }).click();
    await openNode(page, "proposal:tuning");
    const json = await advancedJson(page);
    const config = JSON.parse(await json.inputValue());
    await json.fill(JSON.stringify({ ...config, objectives: { primary: "risk_adjusted_return", secondary: ["drawdown"] } }));
    await page.getByRole("tab", { name: "常用设置", exact: true }).click();
    await expect(page.getByRole("checkbox", { name: "平衡收益与风险", exact: true })).toBeChecked();
    await page.getByText("更多优化指标", { exact: true }).click();
    await page.getByRole("checkbox", { name: "减少滑点", exact: true }).check();
    await page.getByLabel("补充要求", { exact: true }).fill("减少无效信号，并解释每项改进的依据。");
    const saved = await saveProposal(page);
    const proposal = saved.workflow.evolution.nodes.find((n) => n.id === "proposal:tuning")!;
    expect(proposal.config).toMatchObject({ objectives: { primary: "risk_adjusted_return", secondary: ["drawdown", "slippage"] }, tuning_prompt: "减少无效信号，并解释每项改进的依据。" });
  });

  test("custom fields survive basic edits, invalid numeric inputs never save", async ({ page, request }) => {
    const item = await openExample(page, "script_agent");
    const source = await (await request.get(`${API}/strategies/runtime/workflow`, { params: item })).json() as WorkflowView;
    const node = source.strategy.nodes.find((n) => n.kind === "source" && n.binding.path?.[0] === "data_sources")!;
    await openNode(page, node.id);
    const editor = await advancedJson(page);
    const original = JSON.parse(await editor.inputValue());
    await editor.fill(JSON.stringify({ ...original, custom_options: { retry: { attempts: 3 }, tags: ["research", "paper"] } }));
    await page.getByRole("tab", { name: "常用设置", exact: true }).click();
    await page.getByLabel("每次读取条数", { exact: true }).fill("");
    const posts: string[] = [];
    page.on("request", (r) => { if (r.method() === "POST" && r.url().endsWith("/workflow/propose")) posts.push(r.url()); });
    await page.getByRole("button", { name: "保存提案", exact: true }).click();
    await expect(page.getByRole("alert").filter({ hasText: "有效范围" }).first()).toBeVisible();
    expect(posts).toEqual([]);
    await page.getByLabel("每次读取条数", { exact: true }).fill("96");
    const saved = await saveProposal(page);
    expect(saved.workflow.strategy.nodes.find((n) => n.id === node.id)?.config).toMatchObject({ limit: 96, custom_options: { retry: { attempts: 3 }, tags: ["research", "paper"] } });
  });

  test("add custom parameter with a form, then duplicate the edited source", async ({ page, request }) => {
    const item = await openExample(page, "script_agent");
    const source = await (await request.get(`${API}/strategies/runtime/workflow`, { params: item })).json() as WorkflowView;
    const node = source.strategy.nodes.find((n) => n.kind === "source" && n.binding.path?.[0] === "data_sources")!;
    await openNode(page, node.id);
    await page.getByRole("tab", { name: "高级设置", exact: true }).click();
    await page.getByText("+ 添加自定义参数", { exact: true }).click();
    await page.getByLabel("参数名称", { exact: true }).fill("custom_window");
    await chooseOption(page.getByRole("combobox", { name: "参数类型", exact: true }), "number");
    await page.getByRole("button", { name: "添加参数", exact: true }).click();
    await page.getByLabel("custom_window", { exact: true }).fill("7");
    await expect(page.getByRole("textbox", { name: "provider", exact: true })).toBeEditable();
    const reviewDir = path.resolve("test-results/workflow-demo/card-review");
    mkdirSync(reviewDir, { recursive: true });
    await page.getByTestId("workflow-inspector").screenshot({ path: path.join(reviewDir, "custom-parameters-editor.png"), animations: "disabled" });
    await page.getByRole("tab", { name: "常用设置", exact: true }).click();
    await page.getByRole("button", { name: "复制为新卡片", exact: true }).click();
    const name = await page.getByLabel("资源标识", { exact: true }).inputValue();
    await page.getByLabel("每次读取条数", { exact: true }).fill("72");
    await page.getByRole("button", { name: "加入当前变更", exact: true }).click();
    const saved = await saveProposal(page);
    const copied = saved.workflow.strategy.nodes.find((n) => n.id === `source:data_sources/${name}`)!;
    expect(copied.config).toMatchObject({ custom_window: 7, limit: 72 });
    expect(saved.workflow.strategy.nodes.find((n) => n.id === node.id)?.config).toMatchObject({ custom_window: 7 });
  });

  test("unfinished new-card form is protected and account binding is a selector", async ({ page }) => {
    await openExample(page, "script_agent");
    await openNode(page, "account:paper_main");
    await expect(page.getByLabel("使用哪个账户", { exact: true })).toHaveAttribute("data-value", "paper_main");
    await expect(page.getByLabel("完整配置 JSON", { exact: true })).not.toBeVisible();
    await page.getByRole("button", { name: "+ 添加卡片", exact: true }).click();
    await page.getByRole("button", { name: /数据源.*提供行情/ }).click();
    await page.getByLabel("资源标识", { exact: true }).fill("unsaved_source");
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByLabel("资源标识", { exact: true })).toHaveValue("unsaved_source");
    await expect(page.getByRole("button", { name: "保存提案", exact: true })).toBeDisabled();
  });

  test("node-specific Agent handoff contains the exact resource and proposal", async ({ page }) => {
    const item = await openExample(page, "multi_script");
    await openNode(page, "script:signals.py");
    const sent: string[] = [];
    page.on("request", (r) => { if (r.method() === "POST" && /run_turn/.test(r.url())) sent.push(r.url()); });
    await page.getByRole("button", { name: "让主 Agent 帮我修改", exact: true }).click();
    await expect(page).toHaveURL(/\/chat/);
    const composer = page.locator("textarea:visible").first();
    await expect(composer).toHaveValue(new RegExp(item.proposal_id));
    await expect(composer).toHaveValue(/script:signals\.py/);
    expect(sent).toEqual([]);
  });

  test("every resource card and its essential editor are visible, readable and captured", async ({ page, request }) => {
    test.setTimeout(180_000);
    const item = await openExample(page, "script_agent");
    const base = await (await request.get(`${API}/strategies/runtime/workflow`, { params: item })).json() as WorkflowView;
    const root = base.strategy.nodes.find((n) => n.kind === "strategy")!;
    const runtime = base.strategy.nodes.find((n) => n.id === "agent:runtime")!;
    const cfg = runtime.config as Record<string, unknown>;
    // Localized demo content is persisted via the real proposal API, not injected into the DOM.
    const localized = await (await request.post(`${API}/strategies/runtime/workflow/propose`, { data: { ...item, base_revision: base.revision, changes: [
      { node_id: root.id, config: { title: "市场信号研究 · 卡片示例", description: "读取行情，计算信号，由 Agent 分析与复核。仅为架构示例，不启用交易。" } },
      { node_id: runtime.id, config: { ...cfg, agent_profile: { ...(cfg.agent_profile as Record<string, unknown>), role: "阅读脚本提供的市场信号，说明结论、依据和风险。证据不足时明确说明，不自动下单。" } } },
      { node_id: "proposal:tuning", config: { ...(base.evolution.nodes.find((n) => n.id === "proposal:tuning")!.config as Record<string, unknown>), objectives: ["risk_adjusted_return", "drawdown"], tuning_prompt: "先解释问题，再提出可验证的小步改进；不要更改账户和交易权限。" } },
      ...[...base.strategy.nodes, ...base.evolution.nodes].filter((n) => n.kind === "agent" && n.binding.file).map((n) => ({ node_id: n.id, content: n.id === "agent:tuner" ? "# 复盘任务\n查看近期运行记录，找出可改进之处。\n\n# 输出要求\n给出改进方案、验证方法和风险说明。\n所有改动先形成提案，等待人工确认。\n" : "# 分析任务\n阅读提供的数据，只基于已有证据给出判断。\n\n# 输出要求\n说明结论、依据、风险和缺失信息。\n不自动下单，不改变账户权限。\n" })),
    ] } })).json();
    expect(localized.ok, JSON.stringify(localized)).toBe(true);
    await page.goto(`/strategies?strategy_id=${item.strategy_id}&proposal_id=${localized.proposal_id}`);
    await expect(page.getByTestId("strategy-workflow-panel")).toHaveAttribute("aria-busy", "false");
    await page.setViewportSize({ width: 1680, height: 1500 });
    const dir = path.resolve("test-results/workflow-demo/card-review");
    mkdirSync(dir, { recursive: true });
    const browserErrors: string[] = [];
    page.on("pageerror", (err) => browserErrors.push(err.message));
    const manifests: Array<{ id: string; kind: string; card: string; editor: string }> = [];
    for (const view of ["strategy", "evolution"] as const) {
      if (view === "evolution") await page.getByRole("tab", { name: "复盘进化", exact: true }).click();
      await page.getByRole("button", { name: "卡片一览", exact: true }).click();
      const nodes = (localized.workflow as WorkflowView)[view].nodes;
      await expect(page.locator("[data-workflow-node]")).toHaveCount(nodes.length);
      await page.getByTestId("strategy-workflow-panel").screenshot({ path: path.join(dir, `${view}-gallery.png`), animations: "disabled" });
      for (const [index, node] of nodes.entries()) {
        const stem = `${view}-${String(index + 1).padStart(2, "0")}-${node.kind}`;
        const card = page.locator(`[data-workflow-node="${node.id}"]`);
        await card.screenshot({ path: path.join(dir, `${stem}-card.png`), animations: "disabled" });
        await openNode(page, node.id);
        await expect(page.getByRole("tab", { name: "常用设置", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(page.getByLabel("完整配置 JSON", { exact: true })).not.toBeVisible();
        const inspector = page.getByTestId("workflow-inspector");
        await inspector.screenshot({ path: path.join(dir, `${stem}-editor.png`), animations: "disabled" });
        manifests.push({ id: node.id, kind: node.kind, card: `${stem}-card.png`, editor: `${stem}-editor.png` });
        await page.getByRole("button", { name: "关闭详情", exact: true }).click();
      }
    }
    expect(new Set(manifests.map((m) => m.kind)).size).toBe(13);
    expect(browserErrors).toEqual([]);
    writeFileSync(path.join(dir, "manifest.json"), JSON.stringify(manifests, null, 2));
  });
});
