/** Presentation adapters for persisted native results, not model prompts or execution logic. */
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** Read one complete JSON value without guessing missing bytes or swallowing trailing prose. */
export function readJsonPrefix(source: string, start = 0): { value: unknown; end: number } | null {
  while (/\s/.test(source[start] || "x")) start++;
  if (source[start] !== "{" && source[start] !== "[") return null;
  let depth = 0, quoted = false, escaped = false;
  for (let i = start; i < source.length; i++) {
    const char = source[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
    } else if (char === '"') quoted = true;
    else if (char === "{" || char === "[") depth++;
    else if (char === "}" || char === "]") {
      if (--depth === 0) {
        try { return { value: JSON.parse(source.slice(start, i + 1)), end: i + 1 }; } catch { return null; }
      }
    }
  }
  return null;
}

export function isUnifiedDiff(value: string): boolean {
  return /^(?:diff --git |--- [^\n]*\n\+\+\+ )/.test(value.trimStart()) || /^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/m.test(value);
}

/** A completed typed shell snapshot supersedes partial live stdout, including empty output. */
export function shellResult(value: unknown): Record<string, unknown> | null {
  const data = object(value);
  if (typeof data.stdout === "string" || typeof data.stderr === "string" || typeof data.exit_code === "number") return data;
  if (!Array.isArray(data.content)) return null;
  const part = data.content.map(object).find(item => item.type === "shell" && item.data && typeof item.data === "object");
  return part ? object(part.data) : null;
}

/** Old turns store compacted provider text. Decode only the documented transport marker. */
export function legacyToolValue(value: unknown): unknown {
  if (typeof value !== "string") return value;
  if (isUnifiedDiff(value)) {
    const start = value.lastIndexOf("\n{");
    const metadata = start >= 0 ? readJsonPrefix(value, start + 1) : null;
    if (metadata && !value.slice(metadata.end).trim() && ("path" in object(metadata.value)) && ["bytes_after", "lines_after", "occurrences_replaced"].some(key => key in object(metadata.value))) {
      return { content: [{ type: "diff", text: value.slice(0, start), metadata: { path: object(metadata.value).path } }, { type: "json", data: metadata.value }] };
    }
  }
  const marker = value.indexOf("[compacted_kept]");
  if (marker < 0) return value;
  const parsed = readJsonPrefix(value, marker + "[compacted_kept]".length);
  return parsed && !value.slice(parsed.end).trim() ? parsed.value : value;
}

export type DiffLine = { text: string; kind: "added" | "removed" | "context" | "meta"; oldLine?: number; newLine?: number };
export function diffLines(source: string): DiffLine[] {
  let oldLine: number | undefined, newLine: number | undefined;
  return source.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n").map(text => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) { oldLine = Number(hunk[1]); newLine = Number(hunk[2]); }
    if (hunk || text.startsWith("--- ") || text.startsWith("+++ ") || text.startsWith("diff ") || oldLine === undefined || newLine === undefined || text.startsWith("\\")) return { text, kind: "meta" };
    if (text.startsWith("+")) return { text, kind: "added", newLine: newLine++ };
    if (text.startsWith("-")) return { text, kind: "removed", oldLine: oldLine++ };
    return { text, kind: "context", oldLine: oldLine++, newLine: newLine++ };
  });
}

export type InteractionReceipt = { title: string; action: string; rows: { id: string; question: string; answers: string[] }[]; note: string };
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && !!item.trim()) : [];

/** Recover only the human title from a persisted continuation feedback leak. */
export function interactionTitle(text: string): string | null {
  const prefix = "[completion gate continuation]\n";
  const visible = text.startsWith(prefix) ? text.slice(prefix.length) : text;
  if (!visible.startsWith("User response to ")) return null;
  const boundary = visible.indexOf(": {", "User response to ".length);
  if (boundary < 0) return null;
  const title = visible.slice("User response to ".length, boundary).trim();
  return title || null;
}

/** Exact legacy feedback prefix only. User prose and malformed payloads are left untouched. */
export function interactionReceipt(text: string): InteractionReceipt | null {
  const continuationPrefix = "[completion gate continuation]\n";
  const visible = text.startsWith(continuationPrefix) ? text.slice(continuationPrefix.length) : text;
  if (!visible.startsWith("User response to ")) return null;
  const boundary = visible.indexOf(": {");
  if (boundary < 0) return null;
  const parsed = readJsonPrefix(visible, boundary + 2);
  if (!parsed) return null;
  const response = object(parsed.value);
  if (!["answer", "accept", "revise", "reject"].includes(String(response.action))) return null;
  const remainder = visible.slice(parsed.end).trimStart();
  const questions = remainder.startsWith("Questions:") ? readJsonPrefix(remainder, "Questions:".length) : null;
  // 只隐藏已知续跑协议，不能因一个相似前缀吞掉普通用户文字或损坏的问题记录。
  if (remainder && (!questions || !Array.isArray(questions.value))) return null;
  const suffix = questions ? remainder.slice(questions.end).trim() : "";
  if (suffix && !/^Omitted answers are unknown\. (?:Continue using|Continuing using) best judgment within existing permissions\. This is not approval for trading or other gated actions\.$/.test(suffix)) return null;
  const definitions = Array.isArray(questions?.value) ? questions.value.map(object) : [];
  const answers = object(response.answers);
  const ids = [...new Set([...definitions.map(row => String(row.id || "")), ...Object.keys(answers)])].filter(Boolean);
  const rows = ids.map(id => {
    const answer = object(answers[id]), question = definitions.find(row => row.id === id);
    return { id, question: String(question?.question || id), answers: [...strings(answer.selected), ...(typeof answer.text === "string" && answer.text.trim() ? [answer.text] : [])] };
  });
  if (!rows.length && Array.isArray(response.selected)) rows.push({ id: "selected", question: visible.slice(17, boundary), answers: strings(response.selected) });
  return { title: interactionTitle(text) || visible.slice("User response to ".length, boundary), action: String(response.action), rows, note: typeof response.text === "string" ? response.text : "" };
}

export function reasoningPreview(text: string): string {
  return (text.replace(/\r\n?/g, "\n").split("\n").filter(line => line.trim()).at(-1) || "").replace(/^\s*#+\s*/, "").trim();
}
