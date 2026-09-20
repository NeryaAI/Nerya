"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useMemo, useState } from "react";
import { useLocale } from "next-intl";
import { parseRecordedDiff, type WorkbenchFile } from "../../lib/canvasWorkbench";
import { readableResult } from "../../lib/agentConversation";
import { ChevronRightIcon, FileIcon, SearchIcon } from "../icons";
import { CopyButton } from "./tool-cards/atoms";
import { StreamedMarkdown } from "./TurnBlocks";

const quiet = "min-h-9 rounded-md px-2 text-xs text-[color:var(--text-muted)] hover:bg-ink-800/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400";
export function RecordedDiff({ source }: { source: string }) {
  const zh = useLocale().startsWith("zh");
  const { lines } = useMemo(() => parseRecordedDiff(source), [source]);
  const [limit, setLimit] = useState(400);
  return <div className="max-h-[440px] overflow-auto" data-testid="recorded-diff">
    <div className="min-w-full font-mono text-xs leading-6">
      {lines.slice(0, limit).map((line, i) => <div key={i} data-diff-kind={line.kind} className={`flex min-w-full ${line.kind === "add" ? "bg-ok/10 text-ok" : line.kind === "remove" ? "bg-danger/10 text-danger" : line.kind === "meta" ? "bg-ink-800/20 text-[color:var(--text-muted)]" : "text-[color:var(--text-base)]"}`}>
        <span aria-hidden className="w-9 shrink-0 select-none pr-1 text-right opacity-70">{line.old ?? ""}</span><span aria-hidden className="w-9 shrink-0 select-none pr-2 text-right opacity-70">{line.next ?? ""}</span>
        <span className="whitespace-pre pr-4">{line.text || " "}</span>
      </div>)}
    </div>
    {lines.length > limit ? <button type="button" className={`${quiet} w-full`} onClick={() => setLimit((n) => n + 400)}>{zh ? `再显示 400 行（共 ${lines.length} 行）` : `Show 400 more lines (${lines.length} total)`}</button> : null}
  </div>;
}
function FileDetails({ file, onPreview }: { file: WorkbenchFile; onPreview: (id: string) => void }) {
  const zh = useLocale().startsWith("zh");
  const [version, setVersion] = useState("");
  const item = file.versions.find((v) => v.id === version) || file.diff || file.latest;
  return <div className="border-t border-[color:var(--line)]">
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[color:var(--line)] px-3 py-1">
      {file.versions.length > 1 ? <ChoiceSelect aria-label={zh ? "文件记录版本" : "File record version"} value={item.id} onValueChange={setVersion} className="min-h-9 min-w-0 max-w-full flex-1 text-xs">
        {[...file.versions].reverse().map((v, i) => <option key={v.id} value={v.id}>{file.versions.length - i}. {v.kind === "diff" ? (zh ? "差异记录" : "Recorded diff") : (zh ? "内容记录" : "Recorded content")} · {v.title}</option>)}
      </ChoiceSelect> : <span className="text-xs text-[color:var(--text-muted)]">{item.kind === "diff" ? (zh ? "工具返回的差异" : "Diff returned by the tool") : (zh ? "已保存的文件内容" : "Saved file content")}</span>}
      <div className="flex items-center gap-1">{item.body || item.html ? <CopyButton text={item.body || item.html || ""} /> : null}<button type="button" className={quiet} onClick={() => onPreview(item.id)}>{zh ? "打开预览" : "Open preview"}</button></div>
    </div>
    {item.kind === "diff" && item.body ? <RecordedDiff source={item.body} /> : item.body ? <div className="max-h-64 overflow-auto p-4 text-sm"><StreamedMarkdown text={readableResult(item.body, zh)} /></div>
      : <p className="px-4 py-4 text-sm text-[color:var(--text-muted)]">{item.html ? (zh ? "HTML 页面已保存，打开预览查看渲染结果。" : "HTML page saved. Open preview to view the rendered result.") : (zh ? "此文件需要在预览中查看。" : "Open this file in preview to inspect its contents.")}</p>}
  </div>;
}
export function CanvasFileReview({ files, onPreview }: { files: WorkbenchFile[]; onPreview: (id: string) => void }) {
  const zh = useLocale().startsWith("zh");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const filtered = files.filter((f) => (filter !== "changed" || f.changed) && f.path.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const expanded = filtered.length > 0 && filtered.every((f) => opened.has(f.key));
  const diffs = files.filter((f) => f.diff?.body).map((f) => parseRecordedDiff(f.diff!.body!));
  const additions = diffs.reduce((n, d) => n + d.additions, 0), deletions = diffs.reduce((n, d) => n + d.deletions, 0);
  const statusLabels = { created: zh ? "已创建" : "Created", modified: zh ? "已修改" : "Modified", read: zh ? "已读取" : "Read", artifact: zh ? "产物" : "Artifact" };
  function toggleAll() { setOpened((old) => { const next = new Set(old); filtered.forEach((f) => expanded ? next.delete(f.key) : next.add(f.key)); return next; }); }
  return <div className="flex h-full min-h-0 flex-col" data-testid="canvas-file-review">
    <div className="shrink-0 space-y-2 border-b border-[color:var(--line)] px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs"><span>{zh ? `${files.length} 个文件与产物` : `${files.length} files and artifacts`}</span>
        {diffs.length ? <span title={zh ? "每个文件最近一次已保存差异，不是仓库净变更" : "Latest saved diff per file, not repository-wide net changes"}><span className="text-ok">+{additions}</span> <span className="text-danger">−{deletions}</span> <span className="text-[color:var(--text-muted)]">{zh ? "已记录差异" : "recorded diff"}</span></span> : null}
      </div>
      <div className="flex items-center gap-2"><label className="flex min-w-0 flex-1 items-center gap-2 rounded-md border border-[color:var(--line)] px-2"><SearchIcon size={13} /><input aria-label={zh ? "筛选文件" : "Filter files"} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={zh ? "搜索文件路径" : "Search file paths"} className="min-h-9 min-w-0 flex-1 bg-transparent text-xs outline-none" /></label>
        <ChoiceSelect aria-label={zh ? "文件范围" : "File scope"} value={filter} onValueChange={setFilter} className="min-h-9 max-w-[45%] text-xs"><option value="all">{zh ? "全部" : "All files"}</option><option value="changed">{zh ? "已修改" : "Changed"}</option></ChoiceSelect>
      </div>
      {filtered.length ? <button type="button" className={quiet} onClick={toggleAll}>{expanded ? (zh ? "全部收起" : "Collapse all") : (zh ? "全部展开" : "Expand all")}</button> : null}
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      {filtered.length ? <div className="space-y-2">{filtered.map((file) => {
        const stats = file.diff?.body ? parseRecordedDiff(file.diff.body) : null;
        const open = opened.has(file.key);
        return <details key={file.key} open={open} data-testid="workbench-file" onToggle={(e) => { const value = e.currentTarget.open; setOpened((old) => { if (old.has(file.key) === value) return old; const next = new Set(old); value ? next.add(file.key) : next.delete(file.key); return next; }); }} className="overflow-hidden rounded-lg border border-[color:var(--line)]">
          <summary className="flex min-h-12 cursor-pointer list-none items-center gap-2 bg-ink-800/15 px-3 py-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-400"><ChevronRightIcon size={13} className={`shrink-0 ${open ? "rotate-90" : ""}`} /><FileIcon size={14} className="shrink-0 text-[color:var(--text-muted)]" /><span className="min-w-0 flex-1 break-all text-xs" title={file.path}>{file.path}</span><span className="shrink-0 text-[11px] text-[color:var(--text-muted)]">{statusLabels[file.status]}</span>{stats ? <span className="shrink-0 font-mono text-[11px]"><span className="text-ok">+{stats.additions}</span> <span className="text-danger">−{stats.deletions}</span></span> : null}</summary>
          {open ? <FileDetails file={file} onPreview={onPreview} /> : null}
        </details>;
      })}</div> : <div className="py-12 text-center text-sm text-[color:var(--text-muted)]"><FileIcon size={24} className="mx-auto mb-3" /><p>{query || filter !== "all" ? (zh ? "没有匹配的文件" : "No matching files") : (zh ? "文件和改动会显示在这里" : "Files and changes will appear here")}</p>{query || filter !== "all" ? <button type="button" className={`${quiet} mt-3`} onClick={() => { setQuery(""); setFilter("all"); }}>{zh ? "清除筛选" : "Clear filters"}</button> : null}</div>}
    </div>
  </div>;
}
