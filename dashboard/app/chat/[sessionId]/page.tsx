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
    <div className="-mx-4 -mt-2 h-[calc(100dvh-5.5rem)] lg:-mx-8">
      <ChatView sessionId={params.sessionId} />
    </div>
  );
}
