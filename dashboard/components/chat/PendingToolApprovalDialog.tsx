"use client";

/** Single conversation-scoped permission dialog, backed by durable approvals.
 *
 * ZCode's permission requests appear at the point of execution, not hidden in
 * an expanded tool trace. The existing inline ApprovalRequestCard remains the
 * review/restore affordance after closing; this modal never manufactures an
 * approval or silently replays a tool.
 */
import * as Dialog from "@radix-ui/react-dialog";
import { useTranslations } from "next-intl";
import type { ApprovalCard } from "../../lib/clientApi";
import { ApprovalRequestCard } from "./ApprovalRequestCard";

export function PendingToolApprovalDialog({
  approvalId,
  card,
  busy,
  onDismiss,
  onAction,
}: {
  approvalId: string;
  card: ApprovalCard;
  busy: boolean;
  onDismiss: () => void;
  onAction: (callbackData: string) => void | Promise<void>;
}) {
  const t = useTranslations("approvals");
  const kind = String(card.record?.kind || "");
  // Trade and wallet approvals have their own specialised, mandatory risk
  // confirmation flow. Do not substitute an ordinary native-tool prompt.
  if (kind !== "tool_permission" && kind !== "tool_permission_batch") return null;
  return (
    <Dialog.Root open onOpenChange={open => { if (!open && !busy) onDismiss(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="ui-modal-overlay" />
        <Dialog.Content className="ui-dialog max-h-[85vh] overflow-y-auto" style={{width:"min(600px,calc(100vw - 24px))"}} data-testid="native-permission-dialog">
          <div className="space-y-4">
            <Dialog.Title className="text-base font-semibold text-[color:var(--text-base)]">{t("dialogTitle")}</Dialog.Title>
            <Dialog.Description className="text-sm text-[color:var(--text-muted)]">{t("dialogDescription")}</Dialog.Description>
            <ApprovalRequestCard
              event={{kind:"approval_request", approval_id:approvalId, status:"pending", prompt:card.prompt, record:card.record}}
              card={card}
              busy={busy}
              onAction={async callbackData => { await onAction(callbackData); }}
            />
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-[color:var(--text-muted)]">{t("dialogOneTime")}</span>
              <button type="button" className="btn btn-secondary min-h-9" onClick={onDismiss} disabled={busy}>{t("dialogLater")}</button>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
