import { ChatView } from "../../../components/chat/ChatView";

export const metadata = {
  title: "Chat · Nerya",
};

export default function ChatSessionPage({
  params,
}: {
  params: { sessionId: string };
}) {
  return (
    <div className="-mx-8 -my-6 h-screen">
      <ChatView sessionId={params.sessionId} />
    </div>
  );
}
