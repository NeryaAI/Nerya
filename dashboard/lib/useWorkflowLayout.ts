"use client";

import { useEffect, useState } from "react";
import type { WorkflowPosition } from "./workflowTypes";
import { useWorkspaceIdentity } from "./workspaceIdentity";
import { validPositions } from "./workflowLayout";

/** Personal arrangement is not strategy code and must not create a proposal. */
export function useWorkflowLayout(strategyId: string, view: string) {
  const workspace = useWorkspaceIdentity();
  const key = JSON.stringify(["nerya.workflow.layout.v1", workspace, strategyId, view]);
  const [saved, setSaved] = useState<{ key: string; positions: Record<string, WorkflowPosition> }>({ key: "", positions: {} });
  useEffect(() => {
    let positions = {};
    try { positions = validPositions(JSON.parse(localStorage.getItem(key) || "{}")); } catch { /* Preferences may be unavailable; execution is unaffected. */ }
    setSaved({ key, positions });
  }, [key]);
  const positions = saved.key === key ? saved.positions : {};
  function write(next: Record<string, WorkflowPosition>) {
    setSaved({ key, positions: next });
    try { if (Object.keys(next).length) localStorage.setItem(key, JSON.stringify(next)); else localStorage.removeItem(key); } catch { /* Keep this session's layout when storage is full/blocked. */ }
  }
  return { positions, move: (id: string, position: WorkflowPosition) => write({ ...positions, ...validPositions({ [id]: position }) }), reset: () => write({}) };
}
