/**
 * Tool-name predicates. Each tool family (todo / skill / file / shell /
 * web) has a dedicated card; ``TurnBlocks`` uses these predicates to
 * dispatch a ``NativeBlock`` to the right renderer and to suppress the
 * duplicate ``tool_use`` once the matching ``tool_result`` arrives.
 */

import type { NativeBlock } from "../../../lib/chat";

export function isTodoWrite(block: NativeBlock): boolean {
  const action = String(block.action || "").toLowerCase();
  return action === "todo_write" || action === "todowrite";
}

export function isSkillTool(block: NativeBlock): boolean {
  const action = String(block.action || "");
  return action === "Skill" || action.toLowerCase() === "skill";
}

export const FILE_OPS: ReadonlySet<string> = new Set([
  "read_file",
  "edit_file",
  "write_file",
  "list_dir",
  "glob",
  "grep",
]);

export function isFileOp(block: NativeBlock): boolean {
  return FILE_OPS.has(String(block.action || "").toLowerCase());
}

export function isShellTool(block: NativeBlock): boolean {
  const action = String(block.action || "").toLowerCase();
  return action === "run_shell" || action === "bash";
}

export const WEB_TOOLS: ReadonlySet<string> = new Set([
  "web_search",
  "web_fetch",
  "web_search_fetch",
]);

export function isWebTool(block: NativeBlock): boolean {
  return WEB_TOOLS.has(String(block.action || "").toLowerCase());
}

/** True if any of the friendly tool-card renderers will handle ``block``. */
export function isFriendlyToolCard(block: NativeBlock): boolean {
  return (
    isTodoWrite(block) ||
    isSkillTool(block) ||
    isFileOp(block) ||
    isShellTool(block) ||
    isWebTool(block)
  );
}
