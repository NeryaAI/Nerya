export type FieldChange = { path: string; before: unknown; after: unknown };
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
/** Compare actual values, retaining unknown extension fields and removals. */
export function workflowDiff(before: unknown, after: unknown, path = ""): FieldChange[] {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  if (object(before) && object(after)) return [...new Set([...Object.keys(before), ...Object.keys(after)])].flatMap((key) => workflowDiff(before[key], after[key], path ? `${path}.${key}` : key));
  return [{ path, before, after }];
}
export function diffValue(value: unknown, absent: string): string {
  return value === undefined ? absent : typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
