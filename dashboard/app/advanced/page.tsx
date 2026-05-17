"use client";

/**
 * /advanced — power-user runtime knobs that don't belong in the
 * everyday Settings page or the setup wizard.
 *
 * The only resident today is the Runtime Feature Flags panel. The
 * same controls also live in Settings → Capability Gates so operators
 * can manage the workspace config from the main settings workflow;
 * this page remains as a power-user shortcut for direct access.
 *
 * Add more advanced/diagnostic surfaces here as they appear (e.g.
 * tool-compaction debug, capability-catalog inspector). The page is
 * intentionally not linked from the primary nav — operators reach it
 * via the Settings → Runtime "Open advanced" pointer or directly
 * from `/advanced`.
 */

import { useTranslations } from "next-intl";
import { PageBody, PageHeader } from "../../components/Page";
import { RuntimeFlagsPanel } from "../../components/RuntimeFlagsPanel";

export default function AdvancedPage() {
  const t = useTranslations("advancedPage");
  return (
    <PageBody>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
      />
      <RuntimeFlagsPanel />
    </PageBody>
  );
}
