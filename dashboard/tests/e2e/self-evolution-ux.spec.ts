import { expect, test } from "./fixtures";

const TS = "2026-06-17T06:19:44.000Z";
const READY_PROPOSAL_ID = "prp_ux_ready";
const VALIDATION_ID = "vpl_ux_ready";
const EVIDENCE_REF = "strategy_tuning:tune_ux_smoke";

test.describe("Self-evolution UX smoke", () => {
  test("Inbox lineage, proposal detail, and evidence drawer stay readable", async ({ page }) => {
    await mockSelfEvolutionApi(page);

    await page.goto("/self-evolution");
    await expect(page.getByTestId("latest-evolution-replay")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Replays", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Browse records", exact: true }).click();
    const historyDrawer = page.getByTestId("timeline-history-drawer");
    await expect(historyDrawer).toBeVisible();
    await expect(historyDrawer.getByTestId("timeline-replay-strip").first()).toBeVisible();
    await expect(historyDrawer.getByTestId("timeline-replay-strip").first().getByText("Prompt")).toBeVisible();
    await expect(historyDrawer.getByTestId("timeline-replay-strip").first().getByText("Input")).toBeVisible();
    await expect(historyDrawer.getByTestId("timeline-replay-strip").first().getByText("Output")).toBeVisible();
    await historyDrawer.getByTestId("timeline-history-panel").getByRole("button", { name: "Close replay records", exact: true }).click();
    await expect(historyDrawer).toHaveCount(0);
    await expect(page.getByTestId("agent-run-replay")).toBeVisible();
    await expect(page.getByTestId("agent-run-replay").getByText("openai/gpt-4.1-mini", { exact: true }).first()).toBeVisible();
    await expect(page.getByTestId("agent-run-replay").getByText("Role prompt")).toBeVisible();
    await expect(page.getByTestId("agent-run-replay").getByText("Subagent payload", { exact: true })).toBeVisible();
    await expect(page.getByTestId("agent-run-replay").getByText("Subagent output", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await expect(page.getByTestId("evolution-inbox-panel")).toBeVisible();
    await expect(page.getByText("Evolution inbox")).toBeVisible();

    for (const group of [
      "Needs evidence",
      "Needs materialization",
      "Needs validation",
      "Needs approval",
      "Monitoring",
      "Reusable learning",
      "Negative learning",
    ]) {
      await expect(page.getByRole("heading", { name: group, exact: true })).toBeVisible();
    }

    await expect(page.getByTestId("inbox-run-trace-preview")).toHaveCount(1);
    await expect(page.getByText("Model I/O preview")).toHaveCount(1);
    await expect(page.getByTestId("replay-snippet-preview").getByText("Role prompt")).toBeVisible();
    await expect(page.getByTestId("replay-snippet-preview").getByText("Subagent payload", { exact: true })).toBeVisible();
    await expect(page.getByTestId("replay-snippet-preview").getByText("Subagent output", { exact: true })).toBeVisible();
    await expect(page.getByText("pending_review").first()).toBeVisible();
    await expect(page.getByText("rejected_or_rolled_back_outcome_should_downweight_future_reuse")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await expect(page.getByTestId("agent-run-replay")).toBeVisible();
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await expect(page.getByTestId("evolution-inbox-panel")).toBeVisible();
    await expect(page.getByTestId("inbox-run-trace-preview")).toHaveCount(1);
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 1280, height: 720 });
    const inboxReplay = page.getByTestId("inbox-replay-run");
    await expect(inboxReplay).toHaveCount(1);
    await expect(inboxReplay.getByText("Replay run")).toBeVisible();
    await expect(inboxReplay.getByText("openai/gpt-4.1-mini")).toBeVisible();
    await expect(inboxReplay.getByText("strategy_tuner")).toBeVisible();
    await inboxReplay.click();
    const replay = page.getByTestId("agent-run-replay");
    await expect(replay.getByText("Evolution run replay")).toBeVisible();
    await expect(page.getByTestId("run-result-summary").getByText("Run result")).toBeVisible();
    await expect(replay.getByText("strategy_tuner", { exact: true })).toBeVisible();
    await expect(replay.getByText("high", { exact: true }).first()).toBeVisible();
    await expect(replay.getByText("openai/gpt-4.1-mini", { exact: true }).first()).toBeVisible();
    await expect(replay.getByText("Role prompt")).toBeVisible();
    await expect(replay.getByText("Rendered prompt #0")).toBeVisible();
    await expect(replay.getByText("You are the strategy_tuner lane.")).toBeVisible();
    await expect(replay.getByText("Tune alpha for a high-volatility regime.").first()).toBeVisible();
    await expect(replay.getByText("Subagent payload", { exact: true })).toBeVisible();
    await expect(replay.getByText("Structured input summary")).toBeVisible();
    await expect(replay.getByText("Input fields")).toBeVisible();
    await expect(replay.getByText("market_regime high_volatility", { exact: true })).toBeVisible();
    await expect(replay.getByText("Decision context used")).toBeVisible();
    await expect(replay.getByText("mock:BTC/USDT").first()).toBeVisible();
    await expect(replay.getByText("Trade history")).toBeVisible();
    await expect(replay.getByText("pnl_total_usd 12.50")).toBeVisible();
    await expect(replay.getByText("Risk context", { exact: true })).toBeVisible();
    await expect(replay.getByText("risk_rejects 1.0000")).toBeVisible();
    await expect(replay.getByText("News context")).toBeVisible();
    await expect(replay.getByText("latest ETF flows increase volatility")).toBeVisible();
    await expect(replay.getByText("Runtime feedback used")).toBeVisible();
    await expect(replay.getByText("2 total observation(s)")).toBeVisible();
    await expect(replay.getByText("1 negative")).toBeVisible();
    await expect(replay.getByText("negative 1.00")).toBeVisible();
    await expect(replay.getByText("Recent observations")).toBeVisible();
    await expect(replay.getByText("paper run widened drawdown after apply", { exact: true }).first()).toBeVisible();
    await expect(replay.getByText("strategy_run_paper", { exact: true }).first()).toBeVisible();
    await expect(replay.getByText("Runtime feedback debug")).toBeVisible();
    await expect(replay.getByText("Subagent output", { exact: true })).toBeVisible();
    await expect(replay.getByText("Model output summary")).toBeVisible();
    await expect(replay.getByText("tighten alpha filter", { exact: true })).toBeVisible();
    await expect(replay.getByText("Proposed write to strategies/alpha/main.py")).toBeVisible();
    await expect(replay.getByText("Proposed file summary")).toBeVisible();
    await expect(replay.getByText("Raw proposed file")).toBeVisible();
    await expect(replay.getByText("volatility filter tightened")).not.toBeVisible();
    await expect(replay.getByText("tuning_audit.json")).toHaveCount(0);
    await expect(replay.getByText("tuning_run.json")).toHaveCount(0);
    await expect(replay.getByText("tuning_review.md")).toHaveCount(0);
    const timelineDetail = page.locator("section").filter({ hasText: "Evolution replay" });
    await expect(timelineDetail.getByTestId("debug-details").getByText("Debug details")).toBeVisible();
    await expect(timelineDetail.getByText("Raw replay payload")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "Assets", exact: true }).click();
    const optimizerFeedback = page.getByTestId("optimizer-feedback-summary");
    await expect(optimizerFeedback.getByText("Optimizer feedback")).toBeVisible();
    await expect(optimizerFeedback.getByText("2 feedback sample(s)")).toBeVisible();
    await expect(optimizerFeedback.getByText("Optimizer learning details")).toBeVisible();
    await expect(optimizerFeedback.getByText("validation:backtest")).not.toBeVisible();
    await expect(optimizerFeedback.getByText("risk:leverage")).not.toBeVisible();
    await expect(optimizerFeedback.getByText("Candidate decisions")).not.toBeVisible();
    await optimizerFeedback.getByText("Optimizer learning details").click();
    const optimizerCalibration = optimizerFeedback.getByTestId("optimizer-calibration");
    await expect(optimizerCalibration.getByText("Calibration")).toBeVisible();
    await expect(optimizerCalibration.getByText("needs_more_evidence")).toBeVisible();
    await expect(optimizerCalibration.getByText("confidence low")).toBeVisible();
    await expect(optimizerCalibration.getByText("proposal 50%")).toBeVisible();
    await expect(optimizerCalibration.getByText("decision 50%")).toBeVisible();
    await expect(optimizerCalibration.getByText("low_sample_count")).toBeVisible();
    await expect(optimizerFeedback.getByText("validation:backtest")).toBeVisible();
    await expect(optimizerFeedback.getByText("risk:leverage")).toBeVisible();
    await expect(optimizerFeedback.getByText("Candidate decisions")).toBeVisible();
    await expect(optimizerFeedback.getByText("1 promoted")).toBeVisible();
    await expect(page.getByText("eac_preview_safe")).toBeVisible();
    await expect(page.getByText("eac_preview_fail")).toBeVisible();
    await expect(page.getByRole("button", { name: "All 2" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Positive 1" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Negative 1" })).toBeVisible();
    const safeCandidate = page.getByTestId("asset-candidate-card").filter({ hasText: "eac_preview_safe" });
    await expect(safeCandidate.getByText("Backtest preview passed for strategy alpha optimizer candidate safe_backtest.")).toBeVisible();
    await expect(safeCandidate.getByText("review only")).toHaveCount(0);
    await expect(safeCandidate.getByText("not in GDI")).toHaveCount(0);
    await expect(safeCandidate.getByText("Promotion gates")).toHaveCount(0);
    await safeCandidate.getByRole("button", { name: "Inspect" }).click();
    const candidateDrawer = page.getByTestId("candidate-detail-drawer");
    await expect(candidateDrawer).toBeVisible();
    await expect(candidateDrawer.getByRole("heading", { name: "eac_preview_safe" })).toBeVisible();
    await expect(candidateDrawer.getByText("review only")).toBeVisible();
    await expect(candidateDrawer.getByText("not in GDI")).toBeVisible();
    await expect(candidateDrawer.getByText("Preview provenance")).toBeVisible();
    await expect(candidateDrawer.getByText("safe_backtest", { exact: true })).toBeVisible();
    await expect(candidateDrawer.getByText("Promotion gates")).toBeVisible();
    await expect(candidateDrawer.getByText("Candidate evidence")).toBeVisible();
    await expect(candidateDrawer.getByTestId("candidate-debug-payload").getByText("Candidate debug payload")).toBeVisible();
    await candidateDrawer.getByTestId("candidate-detail-close").click();
    await page.getByRole("button", { name: "Positive 1" }).click();
    await expect(page.getByText("eac_preview_safe")).toBeVisible();
    await expect(page.getByText("eac_preview_fail")).toHaveCount(0);
    await page.getByRole("button", { name: "Negative 1" }).click();
    await expect(page.getByText("eac_preview_fail")).toBeVisible();
    await expect(page.getByText("eac_preview_safe")).toHaveCount(0);
    await expect(page.getByText("strategy_tuning:tune_ux_smoke").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "Proposals", exact: true }).click();
    const row = page.locator("div.py-3.text-sm").filter({
      hasText: "High-volatility alpha filter update ready for approval",
    });
    await expect(row).toHaveCount(1);
    await row.getByRole("button", { name: "Inspect" }).click();

    const detail = page.getByTestId("proposal-detail-drawer");
    await expect(detail.getByText(READY_PROPOSAL_ID, { exact: true }).first()).toBeVisible();
    await expect(detail.getByTestId("run-result-summary").getByText("Run result")).toBeVisible();
    await expect(detail.getByText("Why and trust")).toBeVisible();
    await detail.getByText("Why and trust").click();
    await expect(detail.getByText("Why did this happen?")).toBeVisible();
    await expect(detail.getByText("How was this chosen?")).toBeVisible();
    await expect(detail.getByText("Can I trust it?")).toBeVisible();
    await expect(detail.getByText("Action gates")).toBeVisible();
    await expect(detail.getByText("Fitness vector")).toBeVisible();
    await expect(detail.getByText("Lineage graph")).toBeVisible();
    await expect(detail.getByText("Signal, reuse, proposal, validation, and outcome nodes")).toBeVisible();
    await expect(detail.getByText("Lineage details")).toBeVisible();
    const lineageDetails = detail.getByTestId("lineage-graph-details");
    await expect(lineageDetails.getByText("Connections", { exact: true })).not.toBeVisible();
    await detail.getByText("Lineage details").click();
    await expect(lineageDetails.getByText("Connections", { exact: true })).toBeVisible();
    const optimizer = detail.getByTestId("candidate-optimizer-panel");
    await expect(optimizer.getByText("Candidate optimizer")).toBeVisible();
    await expect(optimizer.getByText("safe_backtest", { exact: true }).first()).toBeVisible();
    await expect(optimizer.getByText("2 feedback sample(s)")).toBeVisible();
    await expect(optimizer.getByText("Candidate selection details")).toBeVisible();
    await expect(optimizer.getByText("validation_plan_required").first()).not.toBeVisible();
    await optimizer.getByText("Candidate selection details").click();
    await expect(optimizer.getByText("validation_plan_required")).toBeVisible();
    await expect(optimizer.getByText("validation:backtest")).toBeVisible();
    await expect(optimizer.getByText("Decision sample")).toBeVisible();
    await expect(optimizer.getByText("promoted_positive_preview_reward")).toBeVisible();
    await expect(optimizer.getByText("decision 1")).toBeVisible();
    await expect(optimizer.getByText("decay 1")).toBeVisible();
    await expect(optimizer.getByText(/half-life 45/)).toBeVisible();
    await expect(optimizer.getByText(/cap 1\.2/)).toBeVisible();
    await expect(optimizer.getByText("Validation preview").first()).toBeVisible();
    await expect(optimizer.getByText("1 previewed")).toHaveCount(2);
    await expect(optimizer.getByText("static_check").first()).toBeVisible();
    await expect(optimizer.getByText("Backtest preview").first()).toBeVisible();
    await expect(optimizer.getByText("real data only")).toBeVisible();
    await expect(optimizer.getByText("return 2.6")).toBeVisible();
    await expect(optimizer.getByText("Baseline comparison")).toBeVisible();
    await expect(optimizer.getByText("improved", { exact: true })).toBeVisible();
    await expect(optimizer.getByText("total_return_pct 1.4")).toBeVisible();
    await expect(optimizer.getByText("Learning candidate")).toHaveCount(2);
    await expect(optimizer.getByText("eac_preview_safe").first()).toBeVisible();
    await expect(optimizer.getByText("promoted").first()).toBeVisible();
    await expect(optimizer.getByText("cap_preview_safe")).toBeVisible();
    await expect(detail.getByText("Why reused")).toBeVisible();
    await expect(detail.getByText("strategies/alpha/main.py").first()).toBeVisible();
    const reviewDetails = detail.getByTestId("proposal-review-details");
    await expect(reviewDetails.getByText("Review details")).toBeVisible();
    await reviewDetails.getByText("Review details").click();
    await expect(reviewDetails.getByText("Backtest before / after")).toBeVisible();
    await expect(reviewDetails.getByText("Changed files")).toBeVisible();
    await expect(reviewDetails.getByText("Rationale", { exact: true })).toBeVisible();

    await detail.getByRole("button", { name: EVIDENCE_REF }).first().click();
    const drawer = page.locator(".fixed").filter({ hasText: "strategy_tuning_run_not_found" });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByRole("heading", { name: EVIDENCE_REF, exact: true })).toBeVisible();
    await expect(drawer.getByText("No linked artifacts.")).toBeVisible();
    const evidenceDebug = drawer.getByTestId("evidence-debug-record");
    await expect(evidenceDebug.getByText("Debug record")).toBeVisible();
    await expect(evidenceDebug.getByText("Raw resolver record")).toBeVisible();
  });
});

async function mockSelfEvolutionApi(page: import("@playwright/test").Page) {
  const timelineEnvelope = buildTimelineEnvelope();
  const proposalDetail = buildProposalDetail();

  await page.route("**/api/proxy/evolution/timeline", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(timelineEnvelope),
    });
  });
  await page.route(`**/api/proxy/evolution/proposals/${READY_PROPOSAL_ID}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(proposalDetail),
    });
  });
  await page.route("**/api/proxy/evolution/evidence/resolve", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        count: 1,
        items: [
          {
            ref: EVIDENCE_REF,
            type: "unknown",
            resolved: false,
            title: EVIDENCE_REF,
            summary: "strategy_tuning_run_not_found",
            reason: "strategy_tuning_run_not_found",
            record: {
              ref: EVIDENCE_REF,
              resolver: "mock",
              reason: "strategy_tuning_run_not_found",
            },
            artifacts: [],
          },
        ],
      }),
    });
  });
  await page.route("**/api/proxy/operator/nav", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, nav: {}, unread: 0 }),
    });
  });
  await page.route("**/api/proxy/inbox/items?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, items: [], count: 0 }),
    });
  });
}

function buildTimelineEnvelope() {
  const items = [
    timelineItem({
      id: "signal:missing",
      type: "signal",
      stage: "signal",
      status: "warn",
      title: "Tool Failure Cluster Signal",
      summary: "Three recent tuning attempts referenced volatility but no durable evidence was attached.",
      evidence_refs: [],
      signal_ids: ["sig_missing"],
    }),
    timelineItem({
      id: "proposal:advisory",
      proposal_id: "prp_ux_advisory",
      status: "pending_review",
      title: "Strategy Tuning Proposal",
      summary: "Advisory-only suggestion: consider wider stop when funding spikes",
      action_gates: actionGates({
        state: "pending_review",
        after_file_count: 0,
        validation_status: "missing",
      }),
    }),
    timelineItem({
      id: "proposal:needs_validation",
      proposal_id: "prp_ux_needs_validation",
      validation_plan_id: "vpl_ux_needs_validation",
      status: "pending_review",
      title: "Strategy Tuning Proposal",
      summary: "Tighten alpha entries, waiting for executable validation",
      why_reused: whyReused({ changeCount: 1, validationStatus: "not_run" }),
      action_gates: actionGates({
        state: "pending_review",
        after_file_count: 1,
        validation_status: "not_run",
      }),
    }),
    timelineItem({
      id: `proposal:${READY_PROPOSAL_ID}`,
      proposal_id: READY_PROPOSAL_ID,
      validation_plan_id: VALIDATION_ID,
      status: "pending_review",
      title: "Strategy Tuning Proposal",
      summary: "High-volatility alpha filter update ready for approval",
      evidence_refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
      why_reused: whyReused({ changeCount: 3, validationStatus: "passed" }),
      action_gates: actionGates({
        state: "pending_review",
        after_file_count: 3,
        validation_status: "passed",
      }),
      lineage_graph: lineageGraph(),
      optimizer_report: optimizerReport(),
      fitness_vector: fitnessVector(),
      process: agentRunProcess(),
    }),
    timelineItem({
      id: "proposal:monitoring",
      proposal_id: "prp_ux_monitoring",
      status: "applied",
      title: "Strategy Tuning Proposal",
      summary: "Applied alpha latency guard awaiting post-apply evidence",
      post_apply_monitor: {
        status: "pending",
        summary: "Attach post-apply paper/live/backtest evidence before calling this successful.",
      },
      action_gates: actionGates({
        state: "applied",
        after_file_count: 1,
        validation_status: "missing",
      }),
    }),
    timelineItem({
      id: "asset:positive",
      type: "asset",
      stage: "asset",
      status: "promoted",
      title: "Promoted capsule",
      summary: "Prior high-volatility filter tightening reduced false entries after news spikes.",
      outcome: "promoted",
    }),
    timelineItem({
      id: "proposal:negative",
      proposal_id: "prp_ux_negative",
      status: "rejected",
      title: "Strategy Tuning Proposal",
      summary: "Rejected alpha leverage suggestion after review",
      outcome: "rejected",
      action_gates: actionGates({
        state: "rejected",
        after_file_count: 0,
        validation_status: "missing",
      }),
    }),
  ];

  return {
    ok: true,
    timeline: items,
    inbox: {
      total: 7,
      groups: [
        inboxGroup("needs_evidence", "danger", "signal", [inboxEntry(items[0], ["no_resolvable_evidence_refs_recorded"])]),
        inboxGroup("needs_materialization", "danger", "proposal", [inboxEntry(items[1], ["advisory_only_no_applyable_after_files"])]),
        inboxGroup("needs_validation", "warn", "validation", [inboxEntry(items[2], ["validation_status:not_run"])]),
        inboxGroup("needs_approval", "brand", "proposal", [inboxEntry(items[3], ["validated_proposal_waiting_for_operator_review"])]),
        inboxGroup("monitoring", "ok", "outcome", [inboxEntry(items[4], ["post_apply_observation_pending"])]),
        inboxGroup("reusable_learning", "ok", "asset", [inboxEntry(items[5], ["promoted_learning_available_for_reuse"])]),
        inboxGroup("negative_learning", "danger", "asset", [
          inboxEntry(items[6], ["rejected_or_rolled_back_outcome_should_downweight_future_reuse"]),
        ]),
      ],
    },
    summary: {
      signals: 1,
      events: 1,
      assets: 2,
      capsules: 2,
      candidates: 1,
      blocked_candidates: 0,
      proposals: 5,
      open_proposals: 3,
      validation_plans: 2,
      blocked_validation_plans: 0,
      terminal_outcomes: 1,
      timeline_items: items.length,
      last_activity_ts: TS,
    },
    config: {
      hooks: { enabled: true, sources: ["tool", "strategy"] },
      signal_collection: {
        manual_refresh_endpoint: "/evolution/signals",
        reflection_endpoint: "/evolution/reflect",
        dedupe_window: 30,
      },
      memory_quality_gate: {
        enabled: true,
        minimum_score: 0.55,
        requires_evidence_refs: true,
        blocks_possible_secrets: true,
      },
      validation: {
        dry_run_only: false,
        execution_enabled: true,
        allowed_step_types: ["unit_test", "static_check", "backtest", "manual_review"],
        executable_step_types: ["unit_test", "static_check", "backtest"],
      },
      strategy_tuning: {
        total_strategies: 1,
        enabled_strategies: 1,
        strategies: [{ strategy_id: "alpha", enabled: true }],
      },
      periodic_reflection: {
        id: "evolution.reflect",
        kind: "daily",
        target: "skill:evolution.reflect",
        enabled: false,
        configured: false,
        time: "03:00",
        timezone: "Asia/Shanghai",
      },
    },
    raw: {
      signals: [],
      events: [],
      proposals: [
        proposalSummary("prp_ux_advisory", "Advisory-only suggestion: consider wider stop when funding spikes", "pending_review", null),
        proposalSummary(READY_PROPOSAL_ID, "High-volatility alpha filter update ready for approval", "pending_review", VALIDATION_ID),
        proposalSummary("prp_ux_needs_validation", "Tighten alpha entries, waiting for executable validation", "pending_review", "vpl_ux_needs_validation"),
      ],
      assets: [],
      candidates: [previewAssetCandidate(), failedPreviewAssetCandidate()],
      validation_plans: [],
      strategy_audits: [],
      optimizer_feedback: optimizerFeedbackSummary(),
    },
  };
}

function buildProposalDetail() {
  return {
    ...proposalSummary(READY_PROPOSAL_ID, "High-volatility alpha filter update ready for approval", "pending_review", VALIDATION_ID),
    rationale_md: "# Rationale\n\nReuse prior high-volatility lessons and avoid overtrade regression.\n",
    file_changes: [
      {
        path: "strategies/alpha/main.py",
        before_exists: true,
        before: "def run(ctx):\n    return ctx.result.hold(reason='baseline')\n",
        after: "def run(ctx):\n    return ctx.result.hold(reason='volatility filter tightened')\n",
        diff: "--- before/strategies/alpha/main.py\n+++ after/strategies/alpha/main.py\n@@\n-baseline\n+volatility filter tightened\n",
      },
    ],
    backtest_comparison: {
      strategy_id: "alpha",
      status: "complete",
      summary: "Backtest comparison available: verdict WARN -> PASS.",
      before: { backtest_id: "20260610_090000", metrics: { total_return_pct: 0.8, sharpe_ratio: 0.38 } },
      after: { backtest_id: "20260612_103000", metrics: { total_return_pct: 2.6, sharpe_ratio: 0.91 } },
      metrics_delta: [
        { key: "total_return_pct", before: 0.8, after: 2.6, delta: 1.8, direction: "improved" },
        { key: "sharpe_ratio", before: 0.38, after: 0.91, delta: 0.53, direction: "improved" },
      ],
      evidence_refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
    },
    fitness_vector: fitnessVector(),
    why_reused: whyReused({ changeCount: 3, validationStatus: "passed" }),
    action_gates: actionGates({
      state: "pending_review",
      after_file_count: 3,
      validation_status: "passed",
    }),
    lineage_graph: lineageGraph(),
    optimizer_report: optimizerReport(),
    process: agentRunProcess(),
  };
}

function agentRunProcess() {
  const rolePrompt = "You are the strategy_tuner lane.\nReturn structured JSON with proposed_changes and validation_plan.";
  const renderedPrompt = [
    "Role: strategy_tuner",
    "Mission: Tune alpha for a high-volatility regime.",
    "Allowed targets: strategies/alpha/main.py",
    "Return JSON only.",
  ].join("\n");
  const payload = {
    strategy_id: "alpha",
    market_regime: "high_volatility",
    prompt: "Tune alpha for a high-volatility regime.",
    allowed_targets: ["strategies/alpha/main.py"],
  };
  const decisionContext = {
    strategy_id: "alpha",
    market_context: {
      timeframe: "1h",
      markets: ["mock:BTC/USDT"],
      items: [
        {
          market: "mock:BTC/USDT",
          timeframe: "1h",
          candles_count: 120,
          features: {
            atr_pct: 0.042,
            trend_strength: 0.71,
            adx: 31,
          },
        },
      ],
    },
    trade_metrics: {
      pnl_total_usd: 12.5,
      max_drawdown_usd: -3.2,
      win_rate: 0.62,
      closed: 8,
      avg_slippage: 1.5,
    },
    risk_metrics: {
      risk_rejects: 1,
      risk_blocks: 0,
      decision_holds: 2,
    },
    news_context: {
      count: 2,
      symbols: ["BTC"],
      items: [
        {
          source: "newswire",
          title: "ETF flows increase volatility",
        },
      ],
    },
  };
  const runtimeFeedback = {
    post_apply_observation_count: 2,
    recent_count: 2,
    by_status: { regressed: 1, healthy: 1 },
    by_source: { strategy_run_paper: 1, validation_backtest: 1 },
    negative_count: 1,
    healthy_count: 1,
    observing_count: 0,
    weighted_by_status: { healthy: 1, regressed: 1 },
    weighted_by_source: { strategy_run_paper: 1, validation_backtest: 1 },
    weighted_negative_count: 1,
    weighted_healthy_count: 1,
    weighted_observing_count: 0,
    dominant_sources: [{ source: "strategy_run_paper", raw_count: 1, weight: 1 }],
    last_observed_at: "2026-06-17T06:18:00.000Z",
    decay: { half_life_days: 7, source_weight_cap: 3, anchor_observed_at: "2026-06-17T06:18:00.000Z" },
    evidence_refs: ["file:strategies/alpha/runs/run_regressed.json"],
    recent_observations: [
      {
        id: "obs_run_regressed",
        proposal_id: READY_PROPOSAL_ID,
        status: "regressed",
        source: "strategy_run_paper",
        observed_at: "2026-06-17T06:18:00.000Z",
        run_id: "run_regressed",
        summary: "paper run widened drawdown after apply",
        metrics: { mode: "paper", run_status: "error", max_drawdown_pct: -4.2 },
        evidence_refs: ["file:strategies/alpha/runs/run_regressed.json"],
      },
      {
        id: "obs_backtest_healthy",
        proposal_id: READY_PROPOSAL_ID,
        status: "healthy",
        source: "validation_backtest",
        observed_at: "2026-06-17T06:12:00.000Z",
        run_id: "backtest_after",
        summary: "backtest stayed inside drawdown limits",
        metrics: { mode: "backtest", verdict: "PASS", total_return_pct: 2.6 },
        evidence_refs: ["validation:vrn_ux_pass:step:0"],
      },
    ],
  };
  const output = {
    summary: "tighten alpha filter",
    candidate_id: "safe_backtest",
    proposed_changes: [{ file: "strategies/alpha/main.py", kind: "full_file" }],
    validation_plan: ["unit_test", "backtest"],
  };
  return {
    run: {
      subagent: "strategy_tuner",
      tier: "high",
      provider: "openai",
      model: "gpt-4.1-mini",
      ok: true,
      tokens: 1234,
      usd: 0.0123,
      wall_ms: 2450,
      model_calls: [{ iteration: 0, provider: "openai", model: "gpt-4.1-mini", tier: "high", tokens: 1234, usd: 0.0123 }],
      redacted: true,
    },
    has_prompt: true,
    has_inputs: true,
    has_outputs: true,
    has_generated_docs: true,
    has_file_changes: true,
    has_validation: true,
    sections: [
      {
        id: "prompt_inputs",
        title: "Prompt / inputs",
        summary: "Role prompt and rendered prompt sent to the model.",
        artifacts: [
          { id: "role_prompt", title: "Role prompt", kind: "prompt", language: "markdown", preview: rolePrompt },
          { id: "prompt_iteration_0", title: "Rendered prompt #0", kind: "prompt", language: "text", preview: renderedPrompt },
        ],
      },
      {
        id: "inputs",
        title: "Structured inputs",
        summary: "Runtime payload sent into the subagent.",
        artifacts: [
          { id: "subagent_payload", title: "Subagent payload", kind: "input", language: "json", preview: JSON.stringify(payload, null, 2) },
        ],
      },
      {
        id: "strategy_decision_context",
        title: "Market / risk context",
        summary: "Recent market, trade, risk, and news context used for tuning.",
        artifacts: [
          { id: "strategy_decision_context", title: "Market & risk context", kind: "input", language: "json", preview: JSON.stringify(decisionContext, null, 2) },
        ],
      },
      {
        id: "runtime_feedback",
        title: "Runtime feedback",
        summary: "Weighted post-apply observations and paper/live/shadow runtime evidence sent into the tuning run.",
        artifacts: [
          { id: "runtime_feedback", title: "Runtime feedback", kind: "input", language: "json", preview: JSON.stringify(runtimeFeedback, null, 2) },
        ],
      },
      {
        id: "subagent_output",
        title: "Subagent output",
        summary: "Final structured answer returned by the tuning subagent.",
        artifacts: [
          { id: "subagent_output", title: "Subagent output", kind: "output", language: "json", preview: JSON.stringify(output, null, 2) },
        ],
      },
      {
        id: "proposal_files",
        title: "Proposal files",
        summary: "Files generated under the proposal directory for operator review.",
        artifacts: [
          {
            id: "tuning_audit_json",
            title: "tuning_audit.json",
            kind: "document",
            language: "json",
            preview: JSON.stringify({ prompt_records: ["hidden from main replay"], payload }, null, 2),
          },
          {
            id: "tuning_run_json",
            title: "tuning_run.json",
            kind: "document",
            language: "json",
            preview: JSON.stringify({ optimizer_report: "hidden from main replay" }, null, 2),
          },
          {
            id: "after_main_py",
            title: "main.py",
            kind: "change",
            path: "/tmp/evolution/proposals/prp_ux_ready/after/strategies/alpha/main.py",
            language: "python",
            metadata: {
              scope: "after",
              operation: "proposed_write",
              workspace_path: "strategies/alpha/main.py",
            },
            preview: "def run(ctx):\n    return ctx.result.hold(reason='volatility filter tightened')\n",
          },
        ],
      },
      {
        id: "generated_docs",
        title: "Generated docs",
        summary: "Review and audit documents written by the self-evolution run.",
        artifacts: [
          {
            id: "tuning_review_md",
            title: "tuning_review.md",
            kind: "document",
            language: "markdown",
            preview: "# Review\n\nHidden from the main replay.",
          },
        ],
      },
    ],
  };
}

function optimizerReport() {
  return {
    version: "strategy_tuning_optimizer_v1",
    candidate_count: 3,
    evaluated_count: 3,
    truncated: false,
    selected_candidate_id: "safe_backtest",
    selected_index: 2,
    selected_score: 137,
    outcome_feedback: {
      version: "optimizer_outcome_feedback_v1",
      sample_count: 2,
      positive_samples: 1,
      negative_samples: 1,
      neutral_samples: 0,
      top_features: [
        { feature: "validation:backtest", positive: 1.5, negative: 0, net: 1.5, samples: 1 },
        { feature: "risk:leverage", positive: 0, negative: 2, net: -2, samples: 1 },
      ],
    },
    validation_preview: {
      version: "candidate_validation_preview_summary_v1",
      top_k: 2,
      previewed_count: 1,
      passed_count: 1,
      failed_count: 0,
      skipped_count: 2,
      executed_step_types: ["static_check"],
    },
    backtest_preview: {
      version: "candidate_backtest_preview_summary_v1",
      top_k: 1,
      previewed_count: 1,
      passed_count: 1,
      failed_count: 0,
      no_data_count: 0,
      skipped_count: 0,
    },
    selection_reason: "selected highest deterministic local score from materialization, validation strength, risk, evidence, and expected-effect signals",
    candidates: [
      {
        candidate_id: "blocked_no_validation",
        index: 0,
        score: 24,
        status: "blocked",
        summary: "Materialized change but no executable validation plan was supplied.",
        accepted_count: 0,
        dropped_count: 1,
        materialized_count: 0,
        unmaterialized_count: 0,
        validation_status: "blocked",
        validation_types: [],
        blocked_reasons: ["validation_plan_required"],
        risk_flags: [],
        reasons: ["accepted_changes:1", "validation_blocked"],
        outcome_feedback: {
          score_delta: 0,
          sample_count: 2,
          matched_features: [],
        },
        validation_preview: {
          status: "skipped",
          reason: "candidate_not_materialized",
          requested_step_types: [],
          executed_step_types: [],
          deferred_step_types: [],
        },
        backtest_preview: {},
      },
      {
        candidate_id: "advisory_patch",
        index: 1,
        score: 46,
        status: "advisory",
        summary: "Text patch was useful but not materialized into an after file.",
        accepted_count: 1,
        dropped_count: 0,
        materialized_count: 0,
        unmaterialized_count: 1,
        validation_status: "not_run",
        validation_types: ["manual_review"],
        blocked_reasons: [],
        risk_flags: [],
        reasons: ["accepted_but_not_materialized", "validation_plan_present"],
        outcome_feedback: {
          score_delta: 0,
          sample_count: 2,
          matched_features: [],
        },
        validation_preview: {
          status: "skipped",
          reason: "candidate_not_materialized",
          requested_step_types: ["manual_review"],
          executed_step_types: [],
          deferred_step_types: ["manual_review"],
        },
        backtest_preview: {},
      },
      {
        candidate_id: "safe_backtest",
        index: 2,
        score: 137,
        status: "materialized",
        summary: "Materialized alpha filter change with unit and backtest validation.",
        accepted_count: 1,
        dropped_count: 0,
        materialized_count: 1,
        unmaterialized_count: 0,
        materialized_files: ["strategies/alpha/main.py"],
        validation_status: "not_run",
        validation_types: ["unit_test", "backtest"],
        blocked_reasons: [],
        risk_flags: [],
        reasons: ["materialized_files:1", "validation_step:backtest", "expected_effect_present"],
        outcome_feedback: {
          score_delta: 4.5,
          sample_count: 2,
          matched_features: [
            {
              feature: "validation:backtest",
              positive: 1.5,
              negative: 0,
              net: 1.5,
              samples: 1,
              sources: { asset_candidate_decision: 1 },
              examples: [
                {
                  source: "asset_candidate_decision",
                  asset_candidate_id: "eac_preview_safe",
                  candidate_id: "safe_backtest",
                  run_id: "tune_ux_smoke",
                  state: "promoted",
                  decision: "promoted",
                  preview_type: "backtest",
                  preview_status: "passed",
                  feedback_score: 0.42,
                  feedback_policy: "promoted_positive_preview_reward",
                  feedback_weighting: {
                    version: "optimizer_candidate_decision_weighting_v1",
                    base_score: 0.42,
                    decay_weight: 1,
                    half_life_days: 45,
                    feature_source_cap: 1.2,
                    decided_at: TS,
                  },
                  evidence_refs: [
                    EVIDENCE_REF,
                    "file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/backtest_preview.json",
                  ],
                },
              ],
            },
          ],
        },
        validation_preview: {
          status: "passed",
          score_delta: 12,
          requested_step_types: ["unit_test", "backtest"],
          executed_step_types: ["static_check"],
          deferred_step_types: ["unit_test", "backtest"],
          evidence_refs: ["file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/validation_preview.json"],
          validation: {
            ok: true,
            blockers: [],
            warnings: [],
          },
        },
        backtest_preview: {
          status: "passed",
          score_delta: 24,
          preset: "default",
          allow_mock: false,
          blocked_reasons: [],
          evidence_refs: [
            "file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/backtest_preview.json",
            "file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/package/backtests/preview_ok/metrics.json",
          ],
          backtest_result: {
            ok: true,
            verdict: "PASS",
            coverage_ok: true,
            total_return_pct: 2.6,
            max_drawdown_pct: -0.4,
            sharpe_ratio: 0.91,
            total_trades: 6,
          },
          baseline_comparison: {
            version: "candidate_backtest_baseline_comparison_v1",
            status: "complete",
            overall_direction: "improved",
            summary: "Candidate backtest vs latest workspace baseline: 2 metric(s) improved, 0 regressed.",
            score_delta: 12,
            metrics_delta: [
              {
                key: "total_return_pct",
                before: 1.2,
                after: 2.6,
                delta: 1.4,
                direction: "improved",
              },
              {
                key: "sharpe_ratio",
                before: 0.55,
                after: 0.91,
                delta: 0.36,
                direction: "improved",
              },
            ],
            evidence_refs: ["file:strategies/alpha/backtests/baseline_ok/metrics.json"],
          },
        },
        asset_candidate: {
          id: "eac_preview_safe",
          kind: "capsule",
          state: "promoted",
          decision: "promoted",
          decided_at: TS,
          promoted_ref: "cap_preview_safe",
          safe_to_promote: true,
          blocked_reasons: [],
          evidence_refs: [
            EVIDENCE_REF,
            "file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/backtest_preview.json",
          ],
          preview_type: "backtest",
          preview_status: "passed",
          selected_by_optimizer: true,
          outcome_score: 0.7,
          promotion_gates: previewPromotionGates(),
        },
      },
    ],
  };
}

function previewAssetCandidate() {
  return {
    id: "eac_preview_safe",
    kind: "capsule",
    summary: "Backtest preview passed for strategy alpha optimizer candidate safe_backtest.",
    payload: {
      outcome_score: 0.7,
      metadata: {
        origin: "strategy_optimizer_preview",
        optimizer_run_id: "tune_ux_smoke",
        optimizer_candidate_id: "safe_backtest",
        preview_type: "backtest",
        preview_status: "passed",
        selected_by_optimizer: true,
      },
    },
    evidence_refs: [
      EVIDENCE_REF,
      "file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/backtest_preview.json",
    ],
    source_event_id: null,
    strategy_id: "alpha",
    state: "candidate",
    safe_to_promote: true,
    blocked_reasons: [],
    promotion_gates: previewPromotionGates(),
    ts: TS,
  };
}

function failedPreviewAssetCandidate() {
  return {
    id: "eac_preview_fail",
    kind: "capsule",
    summary: "Backtest preview failed for strategy alpha optimizer candidate bad_backtest.",
    payload: {
      outcome_score: -0.7,
      metadata: {
        origin: "strategy_optimizer_preview",
        optimizer_run_id: "tune_ux_smoke",
        optimizer_candidate_id: "bad_backtest",
        preview_type: "backtest",
        preview_status: "failed",
        selected_by_optimizer: false,
      },
    },
    evidence_refs: [
      EVIDENCE_REF,
      "file:evolution/optimizer_runs/tune_ux_smoke/candidates/bad_backtest/backtest_preview.json",
    ],
    source_event_id: null,
    strategy_id: "alpha",
    state: "candidate",
    safe_to_promote: true,
    blocked_reasons: [],
    promotion_gates: {
      ...previewPromotionGates(),
      warnings: ["review_only_until_promoted", "promotes_as_negative_cautionary_capsule"],
    },
    ts: TS,
  };
}

function previewPromotionGates() {
  return {
    version: "asset_candidate_promotion_gates_v1",
    can_promote: true,
    review_only_until_promoted: true,
    selector_eligible: false,
    checks: [
      {
        id: "evidence_refs",
        status: "passed",
        summary: "2 evidence ref(s) attached.",
      },
      {
        id: "runtime_selector",
        status: "review_only",
        summary: "Pending candidates are not used by Selector/GDI until explicit promotion.",
      },
    ],
    blockers: [],
    warnings: ["review_only_until_promoted"],
  };
}

function optimizerFeedbackSummary() {
  return {
    version: "optimizer_feedback_summary_v1",
    strategy_id: "alpha",
    run_count: 1,
    sample_count: 2,
    positive_samples: 1,
    negative_samples: 1,
    neutral_samples: 0,
    top_positive_features: [
      { feature: "validation:backtest", positive: 1.5, negative: 0, net: 1.5, samples: 1 },
    ],
    top_negative_features: [
      { feature: "risk:leverage", positive: 0, negative: 2, net: -2, samples: 1 },
    ],
    recent_examples: [
      {
        proposal_id: READY_PROPOSAL_ID,
        run_id: "tune_ux_smoke",
        strategy_id: "alpha",
        state: "pending_review",
        selected_candidate_id: "safe_backtest",
        selected_score: 137,
        candidate_status: "materialized",
        feedback_sample_count: 2,
      },
    ],
    calibration: {
      version: "optimizer_feedback_calibration_v1",
      status: "needs_more_evidence",
      confidence: "low",
      warnings: ["single_run_feedback", "low_sample_count"],
      run_count: 1,
      sample_count: 2,
      source_mix: {
        proposal_samples: 1,
        candidate_decision_samples: 1,
        proposal_ratio: 0.5,
        candidate_decision_ratio: 0.5,
      },
      polarity_mix: {
        positive_samples: 1,
        negative_samples: 1,
        neutral_samples: 0,
        positive_ratio: 0.5,
        negative_ratio: 0.5,
        neutral_ratio: 0,
      },
      candidate_decision_mix: {
        total: 1,
        promoted: 1,
        rejected: 0,
        promoted_ratio: 1,
        rejected_ratio: 0,
      },
      feature_concentration: {
        top_abs_net: 2,
        total_abs_net: 3.5,
        top_feature_ratio: 0.5714,
        feature_count: 2,
      },
    },
    candidate_decisions: {
      version: "optimizer_candidate_decisions_v1",
      total: 1,
      promoted: 1,
      rejected: 0,
      recent: [
        {
          candidate_id: "eac_preview_safe",
          asset_kind: "capsule",
          state: "promoted",
          decision: "promoted",
          decided_at: TS,
          strategy_id: "alpha",
          summary: "Backtest preview passed for strategy alpha optimizer candidate safe_backtest.",
          promoted_ref: "cap_preview_safe",
          optimizer_run_id: "tune_ux_smoke",
          optimizer_candidate_id: "safe_backtest",
          preview_type: "backtest",
          preview_status: "passed",
          selected_by_optimizer: true,
          outcome_score: 0.7,
          evidence_refs: [
            EVIDENCE_REF,
            "file:evolution/optimizer_runs/tune_ux_smoke/candidates/safe_backtest/backtest_preview.json",
          ],
        },
      ],
      evidence_refs: [EVIDENCE_REF],
    },
    evidence_refs: [`proposal:${READY_PROPOSAL_ID}`, EVIDENCE_REF],
  };
}

function lineageGraph() {
  return {
    version: "lineage_graph_v1",
    root_id: `proposal:${READY_PROPOSAL_ID}`,
    nodes: [
      {
        id: "signal:sig_high_vol",
        type: "signal",
        label: "market regime high volatility",
        status: "warn",
        summary: "Volatility increased and selected high-volatility tuning assets.",
        evidence_refs: [EVIDENCE_REF],
      },
      {
        id: "gene:gene_regime",
        type: "gene",
        label: "gene_regime",
        status: "selected",
        summary: "Use stricter filters in high volatility.",
        evidence_refs: ["gene:gene_regime"],
      },
      {
        id: `proposal:${READY_PROPOSAL_ID}`,
        type: "proposal",
        label: "High-volatility alpha filter update ready for approval",
        status: "pending_review",
        summary: "Proposal materialized file changes and passed validation.",
        evidence_refs: [EVIDENCE_REF],
      },
      {
        id: "file_change:alpha_main",
        type: "file_change",
        label: "strategies/alpha/main.py",
        status: "proposed",
        summary: "Proposed alpha filter change.",
        evidence_refs: [],
      },
      {
        id: "validation_plan:vpl_ux_ready",
        type: "validation_plan",
        label: "Validation plan",
        status: "passed",
        summary: "1 validation step, 1 required.",
        evidence_refs: ["validation:vrn_ux_pass:step:0"],
      },
      {
        id: "backtest_comparison:alpha",
        type: "backtest_comparison",
        label: "Backtest before/after",
        status: "complete",
        summary: "Backtest comparison available.",
        evidence_refs: ["validation:vrn_ux_pass:step:0"],
      },
    ],
    edges: [
      {
        id: "signal:sig_high_vol->gene:gene_regime:matched",
        source: "signal:sig_high_vol",
        target: "gene:gene_regime",
        type: "matched",
        label: "matched trigger",
        evidence_refs: [EVIDENCE_REF],
      },
      {
        id: `gene:gene_regime->proposal:${READY_PROPOSAL_ID}:selected`,
        source: "gene:gene_regime",
        target: `proposal:${READY_PROPOSAL_ID}`,
        type: "selected",
        label: "selected for proposal",
        evidence_refs: ["gene:gene_regime"],
      },
      {
        id: `proposal:${READY_PROPOSAL_ID}->file_change:alpha_main:proposed_change`,
        source: `proposal:${READY_PROPOSAL_ID}`,
        target: "file_change:alpha_main",
        type: "proposed_change",
        label: "changes file",
        evidence_refs: [],
      },
      {
        id: `proposal:${READY_PROPOSAL_ID}->validation_plan:vpl_ux_ready:requires_validation`,
        source: `proposal:${READY_PROPOSAL_ID}`,
        target: "validation_plan:vpl_ux_ready",
        type: "requires_validation",
        label: "requires validation",
        evidence_refs: [],
      },
      {
        id: `proposal:${READY_PROPOSAL_ID}->backtest_comparison:alpha:validated_by`,
        source: `proposal:${READY_PROPOSAL_ID}`,
        target: "backtest_comparison:alpha",
        type: "validated_by",
        label: "validated by backtest",
        evidence_refs: ["validation:vrn_ux_pass:step:0"],
      },
    ],
    evidence_refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
    warnings: [],
    truncated: false,
  };
}

function timelineItem(overrides: Record<string, unknown>) {
  return {
    id: "proposal:base",
    record_id: "base",
    type: "proposal",
    stage: "proposal",
    ts: TS,
    title: "Strategy Tuning Proposal",
    summary: "",
    status: "pending_review",
    strategy_id: "alpha",
    proposal_id: "prp_base",
    validation_plan_id: null,
    validation_status: null,
    signal_ids: [],
    evidence_refs: [EVIDENCE_REF],
    why: "Runtime emitted an evolution signal.",
    next_step: "Review in the Action Inbox.",
    outcome: null,
    raw: {},
    ...overrides,
  };
}

function inboxGroup(id: string, tone: string, stage: string, items: Record<string, unknown>[]) {
  return { id, tone, stage, action: "Review next action", count: items.length, items };
}

function inboxEntry(item: Record<string, unknown>, reasons: string[]) {
  return {
    id: `inbox:${item.id}`,
    item_id: item.id,
    record_id: item.record_id,
    type: item.type,
    stage: item.stage,
    status: item.status,
    title: item.title,
    summary: item.summary,
    ts: item.ts,
    strategy_id: item.strategy_id,
    proposal_id: item.proposal_id,
    validation_plan_id: item.validation_plan_id,
    evidence_refs: item.evidence_refs,
    reasons,
    next_step: item.next_step,
  };
}

function proposalSummary(id: string, summary: string, state: string, validationPlanId: string | null) {
  return {
    id,
    kind: "strategy_tuning_proposal",
    state,
    path: `/tmp/evolution/proposals/${id}`,
    summary,
    ts: TS,
    target: "strategies/alpha",
    evidence_refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
    validation_plan_id: validationPlanId,
    metadata: { strategy_id: "alpha", materialized: true },
  };
}

function actionGates({
  state,
  after_file_count,
  validation_status,
}: {
  state: string;
  after_file_count: number;
  validation_status: string;
}) {
  return {
    version: "action_gates_v1",
    can_apply: state === "approved" && validation_status === "passed" && after_file_count > 0,
    blockers: state === "pending_review" ? ["state_pending_review"] : state === "rejected" ? ["state_rejected"] : [],
    state,
    kind: "strategy_tuning_proposal",
    materialization: {
      required: true,
      after_file_count,
      paths: after_file_count ? ["strategies/alpha/main.py"] : [],
      advisory_only: after_file_count === 0,
    },
    evidence: {
      required: true,
      count: 2,
      refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
    },
    validation: {
      ok: validation_status === "passed",
      required: true,
      plan_id: VALIDATION_ID,
      status: validation_status,
      reason: validation_status === "missing" ? "missing_validation_plan" : null,
      evidence_refs: validation_status === "passed" ? ["validation:vrn_ux_pass:step:0"] : [],
    },
  };
}

function whyReused({
  changeCount,
  validationStatus,
}: {
  changeCount: number;
  validationStatus: string;
}) {
  return {
    version: "why_reused_v1",
    summary: "Reused 1 Genes, 2 Capsules, 1 cautionary Capsules for 2 matching trigger signals.",
    counts: { genes: 1, capsules: 2, negative_capsules: 1, selection_signals: 2 },
    trigger_context: {
      signal_kinds: ["market_regime_high_volatility", "post_apply_regression"],
      market_regimes: ["high_volatility"],
      markets: ["mock:BTC/USDT"],
      timeframes: ["1h"],
      selected_gene_ids: ["gene_nerya_market_regime_tuning_review"],
      selected_capsule_ids: ["cap_prior_filter_tightening", "cap_prior_overtrade_regression"],
    },
    selection_signals: [
      {
        id: "sig_vol",
        kind: "market_regime_high_volatility",
        severity: "warn",
        confidence: 0.92,
        summary: "High-volatility regime matched prior tuning lessons.",
        evidence_refs: [EVIDENCE_REF],
      },
      {
        id: "sig_regression",
        kind: "post_apply_regression",
        severity: "warn",
        confidence: 0.84,
        summary: "A prior matching capsule had negative post-apply observations.",
        evidence_refs: ["proposal:bad_prior"],
      },
    ],
    genes: [
      {
        kind: "gene",
        id: "gene_nerya_market_regime_tuning_review",
        summary: "Tune strategies using current market regime, news, and data-quality context.",
        evidence_refs: [EVIDENCE_REF],
        gdi_score: 0.82,
        relevance_score: 0.9,
        matched_signals: ["market_regime_high_volatility"],
        matched_context: { market_regimes: ["high_volatility"], markets: ["mock:BTC/USDT"] },
      },
    ],
    capsules: [
      {
        kind: "capsule",
        id: "cap_prior_filter_tightening",
        summary: "Prior high-volatility filter tightening reduced false entries.",
        evidence_refs: ["proposal:good_prior"],
        gdi_score: 0.76,
        relevance_score: 0.88,
        matched_signals: ["market_regime_high_volatility"],
      },
      {
        kind: "capsule",
        id: "cap_prior_overtrade_regression",
        summary: "Prior volatility patch overtraded during thin liquidity and regressed.",
        evidence_refs: ["proposal:bad_prior"],
        outcome_score: -0.45,
        gdi_score: 0.7,
        polarity: "negative",
        relevance_score: 0.86,
        matched_signals: ["market_regime_high_volatility"],
      },
    ],
    negative_capsules: [
      {
        kind: "capsule",
        id: "cap_prior_overtrade_regression",
        summary: "Prior volatility patch overtraded during thin liquidity and regressed.",
        evidence_refs: ["proposal:bad_prior"],
        outcome_score: -0.45,
        gdi_score: 0.7,
        polarity: "negative",
        relevance_score: 0.86,
      },
    ],
    proposal_diff: {
      change_count: changeCount,
      paths: changeCount ? ["strategies/alpha/main.py"] : [],
      materialized: changeCount > 0,
      advisory_only: changeCount === 0,
    },
    validation: {
      plan_id: VALIDATION_ID,
      status: validationStatus,
      summary: "1 validation step(s), 1 required.",
      evidence_refs: validationStatus === "passed" ? ["validation:vrn_ux_pass:step:0"] : [],
    },
    post_apply: null,
    evidence_refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
  };
}

function fitnessVector() {
  return {
    version: "fitness_vector_v0",
    status: "warning",
    summary: "Fitness needs more evidence; 1 warning(s) remain.",
    ready_for_approval: true,
    warnings: ["proposal_state:pending_review"],
    blockers: [],
    evidence_refs: [EVIDENCE_REF, "validation:vrn_ux_pass:step:0"],
    dimensions: [
      {
        id: "validation",
        label: "Validation",
        status: "passed",
        score: 1,
        summary: "Required validation has passed.",
        evidence_refs: ["validation:vrn_ux_pass:step:0"],
      },
      {
        id: "performance_delta",
        label: "Performance Delta",
        status: "passed",
        score: 1,
        summary: "Backtest comparison available: verdict WARN -> PASS.",
        evidence_refs: [EVIDENCE_REF],
      },
      {
        id: "human_preference",
        label: "Human Preference",
        status: "pending",
        summary: "Proposal is still waiting for operator decision.",
        evidence_refs: [`proposal:${READY_PROPOSAL_ID}`],
      },
    ],
  };
}

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page) {
  const offenders = await page.evaluate(() => {
    const ignored = new Set(["SCRIPT", "STYLE", "META", "LINK"]);
    const visible = (el: Element) => {
      if (ignored.has(el.tagName)) return false;
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    return Array.from(document.querySelectorAll("body *"))
      .filter(visible)
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        return rect.left < -2 || rect.right > window.innerWidth + 2;
      })
      .slice(0, 20)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          tag: el.tagName.toLowerCase(),
          className: String(el.getAttribute("class") || "").slice(0, 120),
          text: String(el.textContent || "").trim().slice(0, 120),
          rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        };
      });
  });
  expect(offenders).toEqual([]);
}
