import { redirect } from "next/navigation";

export default function AssistantsPage({searchParams}: {searchParams: {assistant?: string; edit?: string}}) {
  const query = new URLSearchParams({assistant: searchParams.assistant || "main"});
  if (searchParams.edit) query.set("editAssistant", searchParams.edit);
  redirect("/chat?" + query.toString());
}
