import { copy as i18nCopy } from "../../lib/i18n";
import { statusLabel, type TaskStatus } from '../../lib/workbench';

/** Compact visible copy; keep waiting states ahead of a previous completion. */
export function shortTaskStatus(status: TaskStatus, zh: boolean): string {
  if (status.waiting_for === 'user') return i18nCopy(zh, "copy.components_chat_taskStatusCopy.001");
  if (status.waiting_for === 'approval') return i18nCopy(zh, "copy.components_chat_taskStatusCopy.002");
  if (status.waiting_for === 'configuration') return i18nCopy(zh, "copy.components_chat_taskStatusCopy.003");
  if (status.completion === 'external_reported') return i18nCopy(zh, "copy.components_chat_taskStatusCopy.004");
  if (status.execution === 'unconfirmed') return i18nCopy(zh, "copy.components_chat_taskStatusCopy.005");
  return statusLabel(status, zh);
}
