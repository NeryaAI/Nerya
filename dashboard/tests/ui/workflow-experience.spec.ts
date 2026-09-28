import { test, expect, type Page } from "@playwright/test";
import { parseScriptDocumentation } from "../../lib/scriptDocumentation";
import { strategyLanding, strategyMarketTarget } from "../../lib/strategyLanding";
import { invocationGraph, publicReplay, recordedTurnReplay, selectedInvocation } from "../../lib/workflowReplay";
import type { WorkflowGraph, WorkflowNode, WorkflowView } from "../../lib/workflowTypes";
import { compactWorkflow } from "../../lib/workflowProjection";
import { cardFacts, cardTitle } from "../../lib/workflowPresentation";
import { copy } from "../../lib/i18n";
import { chooseOption } from "./choice-control";

const source = `# @nerya.title Collect observations
# @nerya.description Read candles without placing orders.
# @nerya.input Configured market
# @nerya.output Observations
# @nerya.step read | Read market | Load candles
# @nerya.next publish | Data available
# @nerya.step publish | Publish observations | Send to the Agent
# @nerya.output observations

def run(ctx):
    return None
`;

test("lifecycle chooses run details, while drafts and proposals remain editable", () => {
  for (const status of ["live", "running", "paper", "canary"]) expect(strategyLanding(status)).toBe("performance");
  for (const status of ["draft", "disabled", "unknown", undefined]) expect(strategyLanding(status)).toBe("workflow");
  expect(strategyLanding("paused", true)).toBe("performance");
  expect(strategyLanding("live", true, "proposal")).toBe("workflow");
  expect(strategyMarketTarget("BINANCE:BTC/USDT:USDT")).toEqual({ venue: "binance", market: "BTC/USDT:USDT" });
  expect(strategyMarketTarget("BTC/USDT")).toBeNull();
  expect(strategyMarketTarget("binance:  ")).toBeNull();
});

test("script annotations become explanatory steps but never parse quoted examples", () => {
  const doc = parseScriptDocumentation(source);
  expect(doc.title).toBe("Collect observations");
  expect(doc.inputs).toEqual(["Configured market"]);
  expect(doc.steps.map((step) => step.id)).toEqual(["read", "publish"]);
  expect(doc.steps[0].next).toEqual([{ id: "publish", condition: "Data available" }]);
  expect(doc.warnings).toBe(0);
  const quoted = parseScriptDocumentation(`example = '''\n# @nerya.title Not a real annotation\n'''\n${source}`);
  expect(quoted.title).toBe("Collect observations");
  expect(parseScriptDocumentation("'''\n# @nerya.step hidden | Hidden\n'''\n").steps).toEqual([]);
  expect(parseScriptDocumentation("# @nerya.step first | A\n# @nerya.next missing\n# @nerya.step first | Duplicate").warnings).toBe(2);
});

test("replay preserves false and zero, exact IDs, and only public conversation", () => {
  const replay = publicReplay([{ kind: "reasoning", text: "private" }, { kind: "tool_use", call_id: "c", skill_id: "market", action: "read", payload: 0 }, { kind: "tool_result", call_id: "c", ok: true, result: false }, { kind: "text", text: "recorded reply" }]);
  expect(replay.calls).toEqual([{ id: "c", name: "market.read", input: 0, output: false, status: "returned" }]);
  expect(replay.messages).toHaveLength(3);
  expect(JSON.stringify(replay)).not.toContain("private");
  expect(selectedInvocation([{ id: "latest" }], "old", (row) => row.id)).toBeUndefined();
  const graph = invocationGraph({ id: "old", title: "Old", kind: "agent", status: "ok", calls: replay.calls }, { input: "Input", output: "Output", call: "Call" });
  expect(graph.nodes.every((node) => !node.editable)).toBeTruthy();
  expect(graph.nodes[1].config).toEqual({ input: 0, output: false });
  const persisted = recordedTurnReplay({ messages: [{ role: "user", content: "request", ts: "01" }, { role: "assistant", content: "reply", ts: "04" }], events: [{ phase: "tool_use", call_id: "c", tool: "market.read", ts: "02", payload: { payload: 0 } }, { phase: "tool_result", call_id: "c", tool: "market.read", ok: 1, ts: "03", payload: { result: false } }] });
  expect(persisted.messages.map((message) => message.content)).toEqual(["request", 0, false, "reply"]);
});

const workflow: WorkflowView = {
  ok: true, strategy_id: "alpha", revision: "fixture", legacy: false, can_edit: true,
  manifest: { strategy_id: "alpha", title: "Alpha fixture", mode: "paper" },
  source: { proposal_id: null, state: "active", omitted_files: [] }, metadata: { version: 1, nodes: {}, edges: [] },
  strategy: { id: "strategy", nodes: [
    { id: "strategy:alpha", kind: "strategy", title: "Alpha fixture", subtitle: "alpha", resource: "alpha", config: { title: "Alpha fixture" }, binding: { file: null, path: [] }, editable: true, position: { x: 30, y: 30 } },
    { id: "script:main.py", kind: "script", title: "main.py", subtitle: "Python", resource: "main.py", content: source, config: {}, binding: { file: "main.py", path: null }, editable: true, position: { x: 360, y: 30 } },
  ], edges: [] }, evolution: { id: "evolution", nodes: [], edges: [] },
};

async function mock(page: Page, selectedWorkflow: WorkflowView = workflow, language = "en") {
  const errors: string[] = [], writes: string[] = [];
  let failOld = false;
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript((language) => localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language, darkMode: "dark" })), language);
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()), endpoint = url.pathname.replace(/^\/api\/proxy/, "");
    if (route.request().method() === "POST" && endpoint !== "/strategy/list_all") writes.push(endpoint);
    let result: unknown = { ok: true, items: [], count: 0, total: 0 };
    if (endpoint === "/strategies/runtime/workflow") result = selectedWorkflow;
    else if (endpoint === "/strategies/runtime/workflows") result = { ok: true, workflows: [{ key: "alpha", strategy_id: "alpha", title: "Alpha fixture", mode: "paper", status: "draft", state: "active", markets: [], counts: { script: 1 } }] };
    else if (endpoint === "/strategies/runtime/tuning/history") result = { ok: true, strategy_id: "alpha", runs: ["new", "old"].map((id, index) => ({ run_id: id, strategy_id: "alpha", status: "ok", started_at: `2026-09-0${2 - index}T00:00:00Z`, reason: `Review ${id}` })), has_more: false };
    else if (endpoint === "/strategies/runtime/tuning/record") {
      const id = url.searchParams.get("run_id");
      result = failOld && id === "old" ? { ok: false, error: "tuning_run_not_found" } : { ok: true, strategy_id: "alpha", run_id: id, partial: false,
        record: { status: "ok" }, audit: { payload: { request: `Input ${id}` }, prompt_records: [{ prompt: `Prompt ${id}` }], subagent_output: { result: `Output ${id}` }, conversation: [
          { kind: "tool_use", call_id: `call-${id}`, skill_id: "market", action: "read", payload: { limit: 0 } },
          { kind: "tool_result", call_id: `call-${id}`, ok: true, result: { allowed: false, tag: `Tool output ${id}` } },
          { kind: "text", text: `Conversation ${id}` },
        ] } };
    } else if (endpoint === "/auth/status") result = { ok: true, authenticated: true, local_access: true, password_set: true, enabled: true };
    else if (endpoint === "/workspace") result = { root: "fixture", live_trading_enabled: false };
    else if (endpoint === "/health") result = { status: "ok" };
    else if (endpoint === "/setup/readiness") result = { status: "ok", data: { checks: [], blocking: [] } };
    else if (endpoint === "/operator/overview") result = { status: "ok", data: { attention: [], counts: {}, accounts: [], strategies: [] } };
    else if (endpoint === "/operator/nav") result = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    else if (endpoint === "/accounts/list") result = { accounts: [], ts: 0 };
    else if (endpoint === "/agent/sessions") result = { sessions: [], has_more: false };
    else if (endpoint === "/llm/config") result = { ok: true, default_tier: "medium", tiers: [{ tier: "medium", provider: "openai", model: "fixture", key_ref: "fixture-only" }], provider_profiles: [] };
    else if (endpoint === "/portfolio/summary") result = { accounts: [], totals: {} };
    else if (endpoint === "/portfolio/pnl") result = { equity_usd: 0, realized_usd: 0, total_pnl_usd: 0 };
    else if (endpoint.includes("strategy/list")) result = { ok: true, strategies: [] };
    await route.fulfill({ status: 200, json: result });
  });
  return { errors, writes, failOld: () => { failOld = true; } };
}

test("historical review card restores its own canvas, I/O and conversation after reload", async ({ page }, info) => {
  const fixture = await mock(page);
  await page.goto("/strategies?strategy_id=alpha&workflow_log=evolution&workflow_run=old");
  const activity = page.getByTestId("workflow-review-activity");
  const canvas = page.getByTestId("workflow-invocation-canvas");
  await expect(activity).toBeVisible();
  await expect(page.getByTestId("review-explanation")).toBeVisible();
  await expect(canvas).not.toBeVisible();
  await page.getByTestId("review-technical-details").locator("summary").first().click();
  await expect(canvas).toHaveAttribute("data-invocation-id", "old");
  await expect(page.getByTestId("workflow-invocation-conversation")).toContainText("Conversation old");
  await page.getByTestId("workflow-invocation-conversation").getByRole("button", { name: /market.read/ }).first().click();
  await expect(page.getByTestId("workflow-invocation-io")).toContainText("Tool output old");
  await activity.getByRole("button", { name: /Review new/ }).click();
  await expect(canvas).toHaveAttribute("data-invocation-id", "new");
  await expect(canvas).not.toBeVisible();
  await page.getByTestId("review-technical-details").locator("summary").first().click();
  await expect(page.getByTestId("workflow-invocation-conversation")).not.toContainText("Conversation old");
  await activity.getByRole("button", { name: /Review old/ }).click();
  await page.reload();
  await expect(canvas).toHaveAttribute("data-invocation-id", "old");
  await expect(canvas).not.toBeVisible();
  await page.getByTestId("review-technical-details").locator("summary").first().click();
  await expect(page.getByTestId("workflow-invocation-conversation")).toContainText("Conversation old");
  await page.screenshot({ path: info.outputPath("review-replay.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(canvas).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2)).toBeTruthy();
  expect(fixture.errors).toEqual([]);
  expect(fixture.writes).toEqual([]);
});

test("missing historical review does not silently show the newest invocation", async ({ page }) => {
  const fixture = await mock(page); fixture.failOld();
  await page.goto("/strategies?strategy_id=alpha&workflow_log=evolution&workflow_run=old");
  await expect(page.getByTestId("workflow-review-activity").getByRole("alert")).toContainText("tuning_run_not_found");
  await expect(page.getByTestId("workflow-invocation-canvas")).toHaveCount(0);
  await expect(page.getByTestId("workflow-invocation-conversation")).toHaveCount(0);
  expect(fixture.errors).toEqual([]);
  expect(fixture.writes).toEqual([]);
});

test("a vanished review clears the prior conclusion on refresh", async ({ page }) => {
  const fixture = await mock(page);
  await page.goto("/strategies?strategy_id=alpha&workflow_log=evolution&workflow_run=old");
  await expect(page.getByTestId("review-explanation")).toBeVisible();
  fixture.failOld();
  await page.getByTestId("workflow-review-activity").getByRole("button", {name: "Refresh", exact: true}).click();
  await expect(page.getByTestId("workflow-review-activity").getByRole("alert")).toContainText("tuning_run_not_found");
  await expect(page.getByTestId("review-explanation")).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
});

// API-shape fixture only: no execution or performance is claimed by this graph.
function reviewFixture(): WorkflowView {
  const node = (id: string, kind: WorkflowNode["kind"], title: string, config: unknown, path: WorkflowNode["binding"]["path"]): WorkflowNode => ({
    id, kind, title, config, subtitle: "", resource: id.split(":")[1],
    position: { x: id === "agent:tuner" ? 390 : 30, y: 100 },
    binding: { file: null, path }, editable: path !== null,
  });
  const nodes: WorkflowNode[] = [
    node("evidence:review", "script", "Run evidence", { runs: 200, max_age_hours: 168, min_closed_trades: 0 }, ["tuning", "lookback"]),
    node("scheduler:tuning", "scheduler", "Review schedule", { type: "interval", every_seconds: 3600, enabled: false }, ["tuning", "schedule"]),
    { ...node("agent:tuner", "agent", "strategy_tuner", { name: "strategy_tuner", tier: "medium" }, null),
      editable: true, binding: { file: "subagents/strategy_tuner.agent.md", path: null }, content: "Review the frozen evidence and propose one focused change." },
    node("proposal:tuning", "proposal", "Change proposal", { objectives: ["drawdown"], tuning_prompt: "", proposal_policy: {} }, ["$tuning"]),
    node("validation:tuning", "validation", "Validation & replay", { require_operator_approval: true, require_backtest: true }, ["tuning", "guardrails"]),
    node("approval:operator", "approval", "Operator approval", { required: true }, null),
    node("apply:version", "apply", "Version & apply", {}, null),
    node("observation:feedback", "observation", "Observe & learn", {}, null),
  ];
  const chain = [["evidence:review", "agent:tuner"], ["scheduler:tuning", "agent:tuner"], ["agent:tuner", "proposal:tuning"], ["proposal:tuning", "validation:tuning"], ["validation:tuning", "approval:operator"], ["approval:operator", "apply:version"], ["apply:version", "observation:feedback"], ["observation:feedback", "evidence:review"]];
  const evolution: WorkflowGraph = { id: "evolution", enabled: false, nodes, edges: chain.map(([source, target], index) => ({ id: `review-edge-${index}`, source, target, relation: "review_stage", label: "review_stage", origin: "manifest" })) };
  return { ...structuredClone(workflow), evolution };
}

test("default review projects two steps without losing settings, bindings or custom flows", () => {
  const graph = reviewFixture().evolution;
  const before = structuredClone(graph);
  const t = (key: string, values?: Record<string, unknown>) => copy(false, key, values);
  const result = compactWorkflow(graph, t);
  expect(result.graph.nodes.map((node) => [node.id, node.kind])).toEqual([["evidence:review", "script"], ["agent:tuner", "agent"]]);
  expect(result.graph.edges).toEqual([graph.edges[0]]);
  expect(result.supporting.map((group) => group.members.map((node) => node.id))).toEqual([
    ["scheduler:tuning"], ["proposal:tuning", "validation:tuning", "approval:operator", "apply:version", "observation:feedback"],
  ]);
  expect(graph).toEqual(before);
  const legacy = structuredClone(graph);
  legacy.nodes[0].kind = "evidence";
  legacy.nodes[0].title = "Custom collector";
  legacy.nodes[0].position = { x: 71, y: 83 };
  const collector = compactWorkflow(legacy, t).graph.nodes[0];
  expect(collector.binding).toEqual(graph.nodes[0].binding);
  expect(collector.position).toEqual({ x: 71, y: 83 });
  expect(cardTitle(collector, t)).toBe("Custom collector");
  expect(cardFacts(collector, t)).toContain("200");
  const custom = structuredClone(graph);
  custom.nodes.push({ ...graph.nodes[0], id: "script:custom.py" });
  expect(compactWorkflow(custom, t).graph).toBe(custom);
  const unknown = { ...graph, nodes: graph.nodes.filter((node) => node.id !== "evidence:review") };
  expect(compactWorkflow(unknown, t).graph).toBe(unknown);
});

for (const language of ["en", "zh"]) {
  test(`simple review canvas and settings remain usable in ${language}`, async ({ page }, info) => {
    const fixture = await mock(page, reviewFixture(), language);
    const t = (key: string) => copy(language === "zh", key);
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.goto("/strategies?strategy_id=alpha&workflow_log=evolution");
    await expect(page.getByTestId("workflow-review-activity")).toBeVisible();
    await page.getByTestId("workflow-review-log-toggle").click();
    const panel = page.getByTestId("strategy-workflow-panel");
    await expect(panel.locator("[data-workflow-node]")).toHaveCount(2);
    await expect(panel.locator("[data-edge]")).toHaveCount(1);
    const collector = panel.locator('[data-workflow-node="evidence:review"]');
    await expect(collector).toHaveAttribute("data-kind", "script");
    await expect(collector).toContainText(t("copy.simpleReview.scriptTitle"));
    await expect(panel.locator('[data-workflow-node="agent:tuner"]')).toContainText(t("copy.simpleReview.agentTitle"));
    await expect(panel.getByRole("button", { name: t("copy.components_workflows_StrategyWorkflowPanel.042"), exact: true })).toHaveCount(0);

    await collector.locator("button[aria-pressed]").click();
    const inspector = page.getByTestId("workflow-inspector");
    await expect(inspector.getByLabel(t("copy.workflowSettings.fields.evidence.runs"), { exact: true })).toHaveValue("200");
    await expect(inspector.getByLabel(t("copy.workflowSettings.fields.evidence.min_closed_trades"), { exact: true })).toHaveValue("0");
    await expect(inspector).toContainText(t("copy.simpleReview.scriptHow"));
    await expect(inspector.getByRole("button", { name: t("copy.components_workflows_WorkflowInspector.024"), exact: true })).toHaveCount(0);
    await page.keyboard.press("Escape");

    await panel.locator('[data-support-member="scheduler:tuning"]').click();
    await expect(inspector).toBeVisible();
    await expect(inspector).toContainText(t("copy.simpleReview.schedule"));
    await page.keyboard.press("Escape");
    await panel.locator('[data-support-member="proposal:tuning"]').click();
    await chooseOption(page.locator("#workflow-group-resource"), "validation:tuning");
    await expect(inspector.getByRole("switch", { name: t("copy.workflowSettings.fields.validation.require_backtest"), exact: true })).toBeChecked();
    await chooseOption(page.locator("#workflow-group-resource"), "approval:operator");
    await expect(inspector.getByRole("switch")).toHaveCount(0);
    await page.keyboard.press("Escape");

    await chooseOption(panel.getByLabel(t("copy.components_workflows_StrategyWorkflowPanel.039"), { exact: true }), "cards");
    await expect(panel.locator("[data-workflow-node]")).toHaveCount(2);
    await chooseOption(panel.getByLabel(t("copy.components_workflows_StrategyWorkflowPanel.039"), { exact: true }), "canvas");
    await panel.screenshot({ path: info.outputPath(`review-template-${language}.png`), animations: "disabled" });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(collector).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2)).toBeTruthy();
    expect(fixture.errors).toEqual([]);
    expect(fixture.writes).toEqual([]);
  });
}
