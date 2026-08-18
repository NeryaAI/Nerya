"use client";

import { useTranslations } from "next-intl";
import { useWorkspaceUi } from "../../lib/useWorkspaceUi";
import { WorkspaceCustomizeButton } from "./WorkspaceCustomizeButton";
import { WorkspaceUiRenderer } from "./WorkspaceUiRenderer";

/** Renders only manifest-authored home widgets; the built-in cockpit remains. */
export function WorkspaceUiHome() {
  const t = useTranslations("workspaceUi");
  const ui = useWorkspaceUi();
  const widgets = ui.manifest.home.widgets;

  // A runtime without the optional UI route should not add a noisy warning to
  // the built-in dashboard; the customize affordance remains available above
  // and the next refresh will pick the manifest up once the runtime updates.
  if (ui.loading || !widgets.length) return null;

  return (
    <section className="min-w-0">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-medium text-[color:var(--text-base)]">
            {ui.manifest.home.title || t("customSectionTitle")}
          </h2>
          <p className="mt-0.5 text-[12px] text-[color:var(--text-muted)]">
            {ui.manifest.home.description || t("customSectionDescription")}
          </p>
        </div>
        <WorkspaceCustomizeButton context="home" compact />
      </div>

      <WorkspaceUiRenderer widgets={widgets} />

      {ui.data?.warnings?.length ? (
        <div className="mt-2 text-[11px] text-amber-400/90">
          {ui.data.warnings.join(" · ")}
        </div>
      ) : null}
    </section>
  );
}
