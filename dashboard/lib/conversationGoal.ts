"use client";

import { ApiError, callApi } from "./clientApi";
import { getWorkspaceIdentity, workspaceGeneration } from "./workspaceIdentity";

export type GoalState = "active" | "paused" | "blocked" | "usage_limited" | "budget_limited" | "complete";
export type GoalAction = "pause" | "resume";
export type ConversationGoal = {
  protocol: "nerya.goal.v1";
  session_id: string;
  goal_id: string;
  objective: string;
  status: GoalState;
  revision: number;
  token_budget: number | null;
  tokens_used: number;
  time_used_seconds: number;
  remaining_tokens: number | null;
  stop_reason: string | null;
  completion_requested: boolean;
};

const states: GoalState[] = ["active", "paused", "blocked", "usage_limited", "budget_limited", "complete"];
const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/** Only the durable server protocol establishes a goal, including completion. */
export function readConversationGoal(value: unknown, sessionId: string): ConversationGoal | null {
  if (!value || typeof value !== "object") return null;
  const goal = value as ConversationGoal;
  if (goal.protocol !== "nerya.goal.v1" || goal.session_id !== sessionId ||
    typeof goal.goal_id !== "string" || !goal.goal_id.trim() ||
    typeof goal.objective !== "string" || !goal.objective.trim() || !states.includes(goal.status) ||
    !count(goal.revision) || !count(goal.tokens_used) ||
    !(goal.token_budget === null || count(goal.token_budget)) ||
    !(goal.remaining_tokens === null || count(goal.remaining_tokens)) ||
    typeof goal.time_used_seconds !== "number" || !Number.isFinite(goal.time_used_seconds) || goal.time_used_seconds < 0 ||
    !(goal.stop_reason === null || typeof goal.stop_reason === "string") ||
    typeof goal.completion_requested !== "boolean") return null;
  return goal;
}

export function goalFromSnapshot(snapshot: unknown, sessionId: string, previous: ConversationGoal | null = null): ConversationGoal | null {
  const goal = readConversationGoal(snapshot && typeof snapshot === "object" && "goal" in snapshot ? snapshot.goal : null, sessionId);
  // A poll started before a control response must not undo its newer revision.
  return goal && previous?.session_id === sessionId && previous.goal_id === goal.goal_id && previous.revision > goal.revision ? previous : goal;
}

export function goalAction(goal: ConversationGoal): GoalAction | null {
  if (goal.status === "active") return "pause";
  return ["paused", "blocked", "usage_limited"].includes(goal.status) ? "resume" : null;
}

export class GoalControlError extends Error {}

export async function controlConversationGoal(goal: ConversationGoal, action: GoalAction): Promise<ConversationGoal> {
  const workspace = getWorkspaceIdentity(), generation = workspaceGeneration();
  if (!workspace || goalAction(goal) !== action) throw new GoalControlError("unavailable");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await callApi<{ ok: boolean; goal: unknown }>("/agent/goals/control", {
      method: "POST", signal: controller.signal,
      body: { session_id: goal.session_id, action, expected_revision: goal.revision },
    });
    if (workspace !== getWorkspaceIdentity() || generation !== workspaceGeneration()) throw new GoalControlError("workspaceChanged");
    const confirmed = readConversationGoal(response?.goal, goal.session_id);
    if (response?.ok !== true || !confirmed || confirmed.goal_id !== goal.goal_id || confirmed.revision <= goal.revision) {
      throw new GoalControlError("unconfirmed");
    }
    return confirmed;
  } finally { clearTimeout(timer); }
}

export function goalControlErrorKey(error: unknown): string {
  if (error instanceof ApiError) {
    const code = error.payload && typeof error.payload === "object" && "error" in error.payload ? error.payload.error : null;
    if (error.status === 409) {
      if (code === "decision_pending") return "decisionPending";
      if (code === "execution_unconfirmed") return "executionUnconfirmed";
      return "conflict";
    }
    if ([401, 403].includes(error.status)) return "unauthorized";
    if ([404, 405].includes(error.status)) return "unavailable";
  }
  if (error instanceof GoalControlError && ["unavailable", "workspaceChanged", "unconfirmed"].includes(error.message)) return error.message;
  return "failed";
}

// Protocol reason codes are localized; authored reasons remain server content.
export const goalReasonKeys: Record<string, string> = {
  operator_pause: "operatorPause", operator_resume: "operatorResume",
  repeated_blocker: "repeatedBlocker", blocker_reported: "blockerReported", empty_response: "emptyResponse",
  command_interrupted: "commandInterrupted", awaiting_input: "awaitingInput", awaiting_approval: "awaitingApproval",
  awaiting_child_task: "awaitingChildTask", new_user_input: "newUserInput",
  turn_blocked: "turnBlocked", failed: "failed", unconfirmed: "unconfirmed",
  rate_limited: "rateLimited", model_quota_exhausted: "quotaExhausted",
  token_budget_exhausted: "budgetExhausted", completion_verified: "completionVerified", completion_unverified: "completionUnverified",
};
