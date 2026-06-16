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
    <div className="h-full">
      <ChatView sessionId={params.sessionId} />
    </div>
  );
}
