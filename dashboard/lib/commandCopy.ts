import { copy as i18nCopy } from "./i18n";
import type { CommandState } from "./conversationCommands";

const states: Record<CommandState, string> = {
  queued:"copy.lib_commandCopy.001", running:"copy.lib_commandCopy.002",
  stopping:"copy.lib_commandCopy.003",
  delivering:"copy.lib_commandCopy.004", delivered:"copy.lib_commandCopy.005",
  injected:"copy.lib_commandCopy.006", succeeded:"copy.lib_commandCopy.007",
  failed:"copy.lib_commandCopy.008", blocked:"copy.lib_commandCopy.009",
  awaiting_input:"copy.lib_commandCopy.010",
  awaiting_approval:"copy.lib_commandCopy.011", interrupted:"copy.lib_commandCopy.012",
  unconfirmed:"copy.lib_commandCopy.013",
  not_consumed:"copy.lib_commandCopy.014", removed:"copy.lib_commandCopy.015",
};
export function commandStateText(state: string, zh: boolean) { return i18nCopy(zh, states[state as CommandState] ?? "") || (i18nCopy(zh, "copy.lib_commandCopy.016")); }
const errors: Record<string,string> = {
  delivery_unconfirmed:"copy.lib_commandCopy.017",
  command_not_found:"copy.lib_commandCopy.018",
  connection_lost:"copy.lib_commandCopy.019",
  execution_unconfirmed:"copy.lib_commandCopy.020",
  decision_pending:"copy.lib_commandCopy.021",
  queue_pause_required:"copy.lib_commandCopy.022",
  interaction_response_required:"copy.lib_commandCopy.023",
  command_revision_conflict:"copy.lib_commandCopy.024",
  command_already_claimed:"copy.lib_commandCopy.025",
  no_running_turn:"copy.lib_commandCopy.026",
  guide_text_only:"copy.lib_commandCopy.027",
  strategy_version_changed:"copy.lib_commandCopy.028",
  strategy_binding_conflict:"copy.lib_commandCopy.029",
  external_session_read_only:"copy.lib_commandCopy.030",
  rate_limited:"copy.lib_commandCopy.031",
  event_persistence_failed:"copy.lib_commandCopy.032",
  pending_limit:"copy.lib_commandCopy.033",
  queue_full:"copy.lib_commandCopy.034",
  command_storage_unavailable:"copy.lib_commandCopy.035",
  turn_failed:"copy.lib_commandCopy.036",
};
export function commandErrorText(code: string, zh: boolean) {
  return i18nCopy(zh, errors[code] ?? "") || (i18nCopy(zh, "copy.lib_commandCopy.037"));
}
