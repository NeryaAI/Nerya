import { callApi, invalidateReadCache } from "./clientApi";
import type { Verification } from "./workflowVerification";
import type { WorkflowProposalRequest, WorkflowSaveResult, WorkflowSummary, WorkflowTemplate, WorkflowView } from "./workflowTypes";

function checked<T extends { ok: boolean; error?: string }>(value: T): T {
  if (!value.ok) {
    const report = value as T & { validation?: { blockers?: Array<{ message: string }> } };
    const details = report.validation?.blockers?.map((b) => b.message).join("\n");
    throw new Error(details || value.error || "Workflow request failed");
  }
  return value;
}
export const workflowApi = {
  async list() {
    return checked(await callApi<{ ok: boolean; error?: string; workflows: WorkflowSummary[]; total: number }>("/strategies/runtime/workflows"));
  },
  async get(strategyId: string, proposalId?: string | null) {
    const query = new URLSearchParams({ strategy_id: strategyId });
    if (proposalId) query.set("proposal_id", proposalId);
    return checked(await callApi<WorkflowView>(`/strategies/runtime/workflow?${query}`));
  },
  async check(strategyId: string, proposalId: string | null, revision: string, signal?: AbortSignal) {
    const query = new URLSearchParams({ strategy_id: strategyId, base_revision: revision });
    if (proposalId) query.set("proposal_id", proposalId);
    return checked(await callApi<Verification>(`/strategies/runtime/workflow/check?${query}`, { signal }));
  },
  async propose(body: WorkflowProposalRequest) {
    const out = checked(await callApi<WorkflowSaveResult>("/strategies/runtime/workflow/propose", { method: "POST", body }));
    invalidateReadCache();
    return out;
  },
  async template(body: { template: WorkflowTemplate; accounts: string[]; markets: string[]; title?: string }) {
    const out = checked(await callApi<WorkflowSaveResult>("/strategies/runtime/workflow/template", { method: "POST", body }));
    invalidateReadCache();
    return out;
  },
};
