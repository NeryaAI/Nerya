"use client";

import { useEffect, useRef } from "react";
import { getWorkspaceIdentity, useWorkspaceIdentity, workspaceGeneration } from "./workspaceIdentity";

/** Capture before an await; re-check before writes and applying their receipts.
 * Generation also rejects A -> B -> A switches and disposed confirmation dialogs.
 */
export function useActionScope(resource: string) {
  const workspace = useWorkspaceIdentity();
  const generation = workspaceGeneration();
  const key = JSON.stringify([workspace, generation, resource]);
  const scope = useRef({ key });
  if (scope.current.key !== key) scope.current = { key };
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  return {
    key,
    capture() {
      const version = scope.current;
      return () => Boolean(workspace) && mounted.current && version === scope.current
        && version.key === key && getWorkspaceIdentity() === workspace && workspaceGeneration() === generation;
    },
  };
}
