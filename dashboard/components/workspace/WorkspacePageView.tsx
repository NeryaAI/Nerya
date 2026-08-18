"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { PageBody, PageHeader } from "../Page";
import { useWorkspaceUi } from "../../lib/useWorkspaceUi";
import { WorkspaceCustomizeButton } from "./WorkspaceCustomizeButton";
import { WorkspaceUiRenderer } from "./WorkspaceUiRenderer";

export function WorkspacePageView({ pageId }: { pageId: string }) {
  const t = useTranslations("workspaceUi");
  const ui = useWorkspaceUi();
  const page = ui.manifest.pages.find((candidate) => candidate.id === pageId);

  if (ui.loading) {
    return (
      <div className="py-16 text-center text-[13px] text-[color:var(--text-muted)]">
        {t("loadingPage")}
      </div>
    );
  }

  if (!page) {
    return (
      <PageBody>
        <PageHeader
          eyebrow={t("workspacePageEyebrow")}
          title={t("pageNotFoundTitle")}
          description={
            ui.error
              ? t("pageLoadFailedDescription")
              : t("pageNotFoundDescription", { pageId })
          }
          actions={<WorkspaceCustomizeButton context={{ pageId }} />}
        />
        <div className="rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] px-4 py-5 text-[13px] text-[color:var(--text-muted)]">
          <Link href="/dashboard" className="text-brand-300 hover:text-brand-200">
            ← {t("backToDashboard")}
          </Link>
        </div>
      </PageBody>
    );
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("workspacePageEyebrow")}
        title={page.title}
        description={page.description || t("workspacePageDescription")}
        actions={
          <WorkspaceCustomizeButton
            context={{ pageId: page.id, title: page.title }}
          />
        }
      />
      <WorkspaceUiRenderer
        widgets={page.widgets}
        empty={
          <div className="rounded-lg border border-dashed border-[color:var(--line)] px-4 py-10 text-center">
            <p className="text-[13px] text-[color:var(--text-muted)]">{t("emptyPage")}</p>
            <div className="mt-3">
              <WorkspaceCustomizeButton
                context={{ pageId: page.id, title: page.title }}
                compact
              />
            </div>
          </div>
        }
      />
      {ui.data?.warnings?.length ? (
        <div className="text-[11px] text-amber-400/90">
          {ui.data.warnings.join(" · ")}
        </div>
      ) : null}
    </PageBody>
  );
}

