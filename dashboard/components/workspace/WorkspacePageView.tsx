"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { PageBody, PageHeader } from "../Page";
import { useWorkspaceUi } from "../../lib/useWorkspaceUi";
import { clientApi } from "../../lib/clientApi";
import type { WorkspaceUiPreview } from "../../lib/workspaceUiTypes";
import { WorkspaceCustomizeButton } from "./WorkspaceCustomizeButton";
import { WorkspaceUiRenderer } from "./WorkspaceUiRenderer";

export function WorkspacePageView({ pageId, proposalId }: { pageId: string; proposalId?: string }) {
  return proposalId !== undefined
    ? <CandidatePreview key={JSON.stringify([proposalId, pageId])} pageId={pageId} proposalId={proposalId} />
    : <LiveWorkspacePage pageId={pageId} />;
}

function CandidatePreview({ pageId, proposalId }: { pageId: string; proposalId: string }) {
  const t = useTranslations("workspaceUi");
  const [data, setData] = useState<WorkspaceUiPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewDataVersion, setPreviewDataVersion] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void clientApi.workspaceUiPreview(proposalId).then(result => {
      if (!result.ok) throw new Error("invalid_candidate_manifest");
      if (!cancelled) setData(result);
    }).catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { cancelled = true; };
  }, [proposalId]);
  if (error) return <PageBody><div role="alert">{t("previewLoadFailed")}: {error}</div></PageBody>;
  if (!data) return <PageBody>{t("loadingPage")}</PageBody>;
  const containers = pageId === "preview"
    ? [{ ...data.manifest.home, id: "home", title: data.manifest.home.title || t("previewHome") }, ...data.manifest.pages]
    : pageId === "home" ? [{ ...data.manifest.home, id: "home", title: data.manifest.home.title || t("previewHome") }]
    : data.manifest.pages.filter(page => page.id === pageId);
  return <PageBody>
    <PageHeader
      title={t("previewTitle")}
      description={t(previewDataVersion ? "previewReadOnlyDescription" : "previewStructureOnly")}
      actions={containers.some(page => page.widgets.length > 0) ? (
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn btn-primary" onClick={() => setPreviewDataVersion(version => version + 1)}>
            {t(previewDataVersion ? "previewReloadData" : "previewLoadData")}
          </button>
          {previewDataVersion > 0 ? (
            <button type="button" className="btn" onClick={() => setPreviewDataVersion(0)}>
              {t("previewShowStructure")}
            </button>
          ) : null}
        </div>
      ) : undefined}
    />
    <div role="status" className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-sm">
      {data.applied ? t("previewApplied") : t("previewUnapplied")} · {t(previewDataVersion ? "previewReadOnlyData" : "previewDataNotLoaded")}
      <p className="mt-1 text-xs">{t("previewDataDisclaimer")}</p>
      <div className="mt-1 text-xs text-[color:var(--text-muted)]">{data.proposal_id} · {data.state}</div>
    </div>
    {!containers.length ? <p>{t("pageNotFoundDescription", { pageId })}</p> : containers.map(page => (
      <section key={page.id} className="space-y-4">
        <h2 className="text-lg font-medium">{page.title}</h2>
        {page.description ? <p className="text-sm text-[color:var(--text-muted)]">{page.description}</p> : null}
        <WorkspaceUiRenderer key={previewDataVersion} widgets={page.widgets} preview={previewDataVersion === 0} empty={<p>{t("emptyPage")}</p>} />
      </section>
    ))}
    {data.warnings?.length ? <p className="text-xs text-warn">{data.warnings.join(" · ")}</p> : null}
  </PageBody>;
}

function LiveWorkspacePage({ pageId }: { pageId: string }) {
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

