import { ChatView } from "../../components/chat/ChatView";

export const metadata = {
  title: "Chat · Nerya",
};

export default function ChatPage() {
  return (
    <div className="h-full">
      <ChatView sessionId={undefined} />
    </div>
  );
}
