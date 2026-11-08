import { WorkspacePageView } from "../../../../components/workspace/WorkspacePageView";

export const metadata = {
  title: "Workspace Page · Nerya",
};

export default function WorkspacePage({
  params,
  searchParams,
}: {
  params: { pageId: string };
  searchParams?: { proposal_id?: string };
}) {
  return <WorkspacePageView pageId={decodeURIComponent(params.pageId)} proposalId={typeof searchParams?.proposal_id === "string" ? searchParams.proposal_id : undefined} />;
}

