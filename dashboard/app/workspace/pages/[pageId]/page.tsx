import { WorkspacePageView } from "../../../../components/workspace/WorkspacePageView";

export const metadata = {
  title: "Workspace Page · Nerya",
};

export default function WorkspacePage({
  params,
}: {
  params: { pageId: string };
}) {
  return <WorkspacePageView pageId={decodeURIComponent(params.pageId)} />;
}

