"use client";

/**
 * Workspace files drawer.
 *
 * Renders as a right-side slide-in panel and lets the operator browse
 * the workspace, view/edit text files, and create or delete entries.
 * The drawer escapes its mount point via ``createPortal`` so it stays
 * above every layout (chat, dashboard, settings) without z-index
 * gymnastics.
 *
 * The backend endpoints live under ``/workspace/file*`` in
 * :mod:`nerya.api.routes_workspace`. They reuse
 * :func:`resolve_workspace_path` for safety and refuse mutations on
 * sensitive globs (``.env``, ``accounts/*`` …) so a stray click in
 * the drawer can't blast through credentials.
 *
 * V1 capabilities: list, read, save, create file/dir, delete. File
 * uploads are deferred — they need multipart through the JSON proxy.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import {
  ChevronLeftIcon,
  CopyIcon,
  FileIcon,
  FilePlusIcon,
  FolderIcon,
  FolderPlusIcon,
  RefreshIcon,
  SaveIcon,
  TrashIcon,
  XIcon,
} from "./icons";
import { clientApi, type WorkspaceFileEntry } from "../lib/clientApi";
import { confirm as confirmDialog } from "../lib/dialogs";

interface FilesDrawerProps {
  open: boolean;
  onClose: () => void;
}

type Stage =
  | { kind: "list"; path: string }
  | { kind: "view"; path: string };

interface ListData {
  path: string;
  entries: WorkspaceFileEntry[];
  truncated: boolean;
  breadcrumbs: Array<{ name: string; path: string }>;
  show_hidden: boolean;
}

interface FileData {
  path: string;
  size: number;
  binary: boolean;
  content: string;
  truncated: boolean;
}

function fmtBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function fmtTime(ms: number): string {
  try {
    return new Date(ms).toLocaleString();
  } catch {
    return "";
  }
}

function joinPath(parent: string, name: string): string {
  if (!parent || parent === "." || parent === "/") return name;
  return `${parent}/${name}`.replace(/\\/g, "/");
}

export function FilesDrawer({ open, onClose }: FilesDrawerProps) {
  const t = useTranslations("files");
  const [stage, setStage] = useState<Stage>({ kind: "list", path: "." });
  const [list, setList] = useState<ListData | null>(null);
  const [file, setFile] = useState<FileData | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [creating, setCreating] = useState<null | "file" | "dir">(null);
  const [createName, setCreateName] = useState("");
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const loadList = useMemo(
    () =>
      async (path: string) => {
        setLoading(true);
        setError(null);
        try {
          const res = await clientApi.workspaceFilesList(path, showHidden);
          if (!res.ok) {
            setError(res.detail || res.error || "load_failed");
            setLoading(false);
            return;
          }
          setList({
            path: res.path ?? path,
            entries: res.entries ?? [],
            truncated: res.truncated ?? false,
            breadcrumbs: res.breadcrumbs ?? [],
            show_hidden: res.show_hidden ?? showHidden,
          });
          setLoading(false);
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
          setLoading(false);
        }
      },
    [showHidden],
  );

  const loadFile = useMemo(
    () => async (path: string) => {
      setLoading(true);
      setError(null);
      try {
        const res = await clientApi.workspaceFileRead(path);
        if (!res.ok) {
          setError(res.detail || res.error || "read_failed");
          setLoading(false);
          return;
        }
        const next: FileData = {
          path: res.path ?? path,
          size: res.size ?? 0,
          binary: res.binary ?? false,
          content: res.content ?? "",
          truncated: res.truncated ?? false,
        };
        setFile(next);
        setDraft(next.content);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!open) return;
    if (stage.kind === "list") {
      loadList(stage.path);
    } else {
      loadFile(stage.path);
    }
  }, [open, stage, loadList, loadFile]);

  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!open) {
      setError(null);
      setCreating(null);
      setCreateName("");
      setSavedAt(null);
    }
  }, [open]);

  if (!mounted || !open) return null;

  function reset() {
    setStage({ kind: "list", path: list?.path ?? "." });
    setFile(null);
    setDraft("");
    setError(null);
    setSavedAt(null);
  }

  async function handleCreate() {
    if (!list) return;
    const name = createName.trim();
    if (!name) {
      setCreating(null);
      return;
    }
    const target = joinPath(list.path, name);
    setError(null);
    try {
      const res = await clientApi.workspaceFileCreate({
        path: target,
        kind: creating === "dir" ? "dir" : "file",
      });
      if (!res.ok) {
        setError(res.detail || res.error || "create_failed");
        return;
      }
      setCreating(null);
      setCreateName("");
      await loadList(list.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleDelete(entry: WorkspaceFileEntry) {
    const confirmed = await confirmDialog({
      title: t("delete"),
      message: t("confirmDelete", { name: entry.name }),
      okLabel: t("delete"),
      cancelLabel: t("cancel"),
      tone: "danger",
    });
    if (!confirmed) return;
    setError(null);
    try {
      const res = await clientApi.workspaceFileDelete({
        path: entry.path,
        recursive: entry.kind === "dir",
      });
      if (!res.ok) {
        setError(res.detail || res.error || "delete_failed");
        return;
      }
      if (list) await loadList(list.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleSave() {
    if (!file) return;
    setError(null);
    try {
      const res = await clientApi.workspaceFileSave({
        path: file.path,
        content: draft,
      });
      if (!res.ok) {
        setError(res.detail || res.error || "save_failed");
        return;
      }
      setFile({
        ...file,
        size: res.size ?? draft.length,
        content: draft,
        truncated: false,
      });
      setSavedAt(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const dirty = file != null && draft !== file.content;

  return createPortal(
    <div className="fixed inset-0 z-[1100] pointer-events-none">
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className={`absolute inset-0 bg-black/40 backdrop-blur-[2px] transition-opacity duration-200 pointer-events-auto ${
          open ? "opacity-100" : "opacity-0"
        }`}
      />
      <aside
        role="dialog"
        aria-label={t("title")}
        className={`absolute top-0 right-0 h-full w-full sm:w-[480px] max-w-[92vw] flex flex-col pointer-events-auto bg-ink-900/95 backdrop-blur-airy border-l border-brand-500/15 shadow-airy transition-transform duration-200 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        {/* Header */}
        <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-brand-500/15">
          <div className="flex items-center gap-2 min-w-0">
            <FolderIcon size={16} className="text-brand-300 shrink-0" />
            <h2 className="text-[14px] font-semibold tracking-tight text-white truncate">
              {t("title")}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="topnav-pill-icon cursor-pointer"
            aria-label={t("close")}
            title={t("close")}
          >
            <XIcon size={16} />
          </button>
        </header>

        {stage.kind === "list" ? (
          <ListPane
            list={list}
            loading={loading}
            error={error}
            showHidden={showHidden}
            creating={creating}
            createName={createName}
            t={t}
            onSetShowHidden={setShowHidden}
            onCreateStart={(kind) => {
              setCreating(kind);
              setCreateName("");
            }}
            onCreateNameChange={setCreateName}
            onCreateCommit={handleCreate}
            onCreateCancel={() => {
              setCreating(null);
              setCreateName("");
            }}
            onRefresh={() => list && loadList(list.path)}
            onDelete={handleDelete}
            onOpenFile={(entry) => setStage({ kind: "view", path: entry.path })}
            onOpenDir={(entry) => setStage({ kind: "list", path: entry.path })}
            onCrumbClick={(p) => setStage({ kind: "list", path: p })}
          />
        ) : (
          <ViewPane
            file={file}
            draft={draft}
            dirty={dirty}
            loading={loading}
            error={error}
            savedAt={savedAt}
            t={t}
            onBack={reset}
            onSave={handleSave}
            onDraftChange={setDraft}
            onCopy={() => {
              if (!file) return;
              try {
                navigator.clipboard.writeText(draft);
              } catch {
                /* ignore */
              }
            }}
            onDeleteFile={() => {
              if (!file) return;
              const entry: WorkspaceFileEntry = {
                name: file.path.split("/").pop() || file.path,
                kind: "file",
                size: file.size,
                mtime_ms: Date.now(),
                path: file.path,
              };
              handleDelete(entry).then(() => reset());
            }}
          />
        )}
      </aside>
    </div>,
    document.body,
  );
}

interface ListPaneProps {
  list: ListData | null;
  loading: boolean;
  error: string | null;
  showHidden: boolean;
  creating: null | "file" | "dir";
  createName: string;
  t: (k: string, p?: Record<string, string | number>) => string;
  onSetShowHidden: (v: boolean) => void;
  onCreateStart: (kind: "file" | "dir") => void;
  onCreateNameChange: (v: string) => void;
  onCreateCommit: () => void | Promise<void>;
  onCreateCancel: () => void;
  onRefresh: () => void;
  onDelete: (entry: WorkspaceFileEntry) => void | Promise<void>;
  onOpenFile: (entry: WorkspaceFileEntry) => void;
  onOpenDir: (entry: WorkspaceFileEntry) => void;
  onCrumbClick: (path: string) => void;
}

function ListPane({
  list,
  loading,
  error,
  showHidden,
  creating,
  createName,
  t,
  onSetShowHidden,
  onCreateStart,
  onCreateNameChange,
  onCreateCommit,
  onCreateCancel,
  onRefresh,
  onDelete,
  onOpenFile,
  onOpenDir,
  onCrumbClick,
}: ListPaneProps) {
  const createInputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (creating) createInputRef.current?.focus();
  }, [creating]);

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-brand-500/15 bg-brand-500/[0.03]">
        <div className="flex flex-wrap items-center gap-1 min-w-0 flex-1 text-[11px] text-ink-300 font-mono">
          {(list?.breadcrumbs ?? []).map((crumb, idx, arr) => (
            <span key={`${crumb.path}-${idx}`} className="flex items-center gap-1 min-w-0">
              <button
                type="button"
                onClick={() => onCrumbClick(crumb.path)}
                className={`truncate max-w-[120px] cursor-pointer transition-colors ${
                  idx === arr.length - 1
                    ? "text-white"
                    : "text-ink-400 hover:text-ink-100"
                }`}
                title={crumb.path}
              >
                {idx === 0 ? t("root") : crumb.name}
              </button>
              {idx < arr.length - 1 ? (
                <span className="text-ink-500">/</span>
              ) : null}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={() => onCreateStart("file")}
            className="topnav-pill-icon cursor-pointer"
            title={t("newFile")}
            aria-label={t("newFile")}
          >
            <FilePlusIcon size={15} />
          </button>
          <button
            type="button"
            onClick={() => onCreateStart("dir")}
            className="topnav-pill-icon cursor-pointer"
            title={t("newFolder")}
            aria-label={t("newFolder")}
          >
            <FolderPlusIcon size={15} />
          </button>
          <button
            type="button"
            onClick={onRefresh}
            className="topnav-pill-icon cursor-pointer"
            title={t("refresh")}
            aria-label={t("refresh")}
          >
            <RefreshIcon size={15} />
          </button>
          <label className="ml-1 text-[11px] text-ink-400 font-medium flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => onSetShowHidden(e.target.checked)}
              className="accent-brand-500 cursor-pointer"
            />
            {t("hidden")}
          </label>
        </div>
      </div>

      {creating ? (
        <div className="flex items-center gap-2 px-4 py-2 border-b border-brand-500/15 bg-brand-500/[0.05]">
          {creating === "dir" ? (
            <FolderPlusIcon size={14} className="text-brand-300" />
          ) : (
            <FilePlusIcon size={14} className="text-brand-300" />
          )}
          <input
            ref={createInputRef}
            value={createName}
            onChange={(e) => onCreateNameChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCreateCommit();
              if (e.key === "Escape") onCreateCancel();
            }}
            placeholder={
              creating === "dir" ? t("newFolderHint") : t("newFileHint")
            }
            className="flex-1 rounded-md border border-brand-500/20 bg-ink-900/80 px-2 py-1 text-[12px] text-ink-100 focus:outline-none focus:border-brand-500/50"
          />
          <button
            type="button"
            onClick={onCreateCommit}
            className="px-2.5 py-1 rounded-md bg-brand-500 text-white text-[11px] font-semibold cursor-pointer hover:bg-brand-400 transition-colors"
          >
            {t("create")}
          </button>
          <button
            type="button"
            onClick={onCreateCancel}
            className="px-2 py-1 rounded-md border border-brand-500/20 text-ink-300 text-[11px] cursor-pointer hover:bg-brand-500/[0.06] transition-colors"
          >
            {t("cancel")}
          </button>
        </div>
      ) : null}

      {error ? (
        <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-rose-500/30 bg-rose-500/[0.08] text-[12px] text-rose-200 font-mono break-all">
          {error}
        </div>
      ) : null}

      <div className="flex-1 min-h-0 overflow-y-auto">
        {loading && !list ? (
          <div className="px-4 py-8 text-center text-ink-400 text-[12px]">
            {t("loading")}
          </div>
        ) : null}
        {list && list.entries.length === 0 && !loading ? (
          <div className="px-4 py-8 text-center text-ink-400 text-[12px]">
            {t("empty")}
          </div>
        ) : null}
        <ul className="py-1">
          {list?.entries.map((entry) => (
            <li
              key={entry.path}
              className="group flex items-center gap-2 px-4 py-2 hover:bg-brand-500/[0.06] cursor-pointer transition-colors border-b border-brand-500/[0.06]"
              onClick={() =>
                entry.kind === "dir" ? onOpenDir(entry) : onOpenFile(entry)
              }
            >
              {entry.kind === "dir" ? (
                <FolderIcon size={14} className="text-brand-300 shrink-0" />
              ) : (
                <FileIcon size={14} className="text-ink-400 shrink-0" />
              )}
              <span className="flex-1 min-w-0 truncate text-[13px] text-ink-100">
                {entry.name}
                {entry.is_symlink ? (
                  <span className="ml-1 text-[11px] text-amber-400 font-mono">
                    sym
                  </span>
                ) : null}
              </span>
              <span className="hidden sm:inline text-[10px] font-mono text-ink-500 tabular-nums">
                {entry.kind === "file" ? fmtBytes(entry.size) : ""}
              </span>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(entry);
                }}
                className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded-md hover:bg-rose-500/20 text-ink-400 hover:text-rose-200 cursor-pointer"
                title={t("delete")}
                aria-label={t("delete")}
              >
                <TrashIcon size={13} />
              </button>
            </li>
          ))}
        </ul>
        {list?.truncated ? (
          <div className="px-4 py-3 text-[11px] text-amber-200/80 text-center font-mono">
            {t("truncated")}
          </div>
        ) : null}
      </div>
    </div>
  );
}

interface ViewPaneProps {
  file: FileData | null;
  draft: string;
  dirty: boolean;
  loading: boolean;
  error: string | null;
  savedAt: number | null;
  t: (k: string, p?: Record<string, string | number>) => string;
  onBack: () => void;
  onSave: () => void;
  onDraftChange: (v: string) => void;
  onCopy: () => void;
  onDeleteFile: () => void;
}

function ViewPane({
  file,
  draft,
  dirty,
  loading,
  error,
  savedAt,
  t,
  onBack,
  onSave,
  onDraftChange,
  onCopy,
  onDeleteFile,
}: ViewPaneProps) {
  const fileName = file?.path.split("/").pop() ?? "";

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-brand-500/15 bg-brand-500/[0.03]">
        <button
          type="button"
          onClick={onBack}
          className="topnav-pill-icon cursor-pointer"
          title={t("back")}
          aria-label={t("back")}
        >
          <ChevronLeftIcon size={16} />
        </button>
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-semibold text-white truncate">
            {fileName}
          </div>
          <div
            className="text-[10px] font-mono text-ink-500 truncate"
            title={file?.path}
          >
            {file?.path}
            {file ? ` · ${fmtBytes(file.size)}` : null}
            {savedAt ? ` · ${t("savedAt", { time: fmtTime(savedAt) })}` : null}
          </div>
        </div>
        <button
          type="button"
          onClick={onCopy}
          className="topnav-pill-icon cursor-pointer"
          title={t("copy")}
          aria-label={t("copy")}
        >
          <CopyIcon size={14} />
        </button>
        <button
          type="button"
          onClick={onDeleteFile}
          className="topnav-pill-icon cursor-pointer hover:!text-rose-300"
          title={t("delete")}
          aria-label={t("delete")}
        >
          <TrashIcon size={14} />
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={!dirty || loading || file?.binary}
          className={`flex items-center gap-1 px-3 py-1.5 rounded-full text-[11px] font-semibold transition-colors cursor-pointer ${
            !dirty || loading || file?.binary
              ? "bg-white/[0.04] text-ink-500 cursor-not-allowed"
              : "bg-brand-500 text-white hover:bg-brand-400"
          }`}
        >
          <SaveIcon size={12} />
          {t("save")}
        </button>
      </div>

      {error ? (
        <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-rose-500/30 bg-rose-500/[0.08] text-[12px] text-rose-200 font-mono break-all">
          {error}
        </div>
      ) : null}

      {file?.binary ? (
        <div className="flex-1 flex flex-col items-center justify-center gap-2 p-6 text-center">
          <FileIcon size={32} className="text-ink-500" />
          <div className="text-[13px] text-ink-200">{t("binaryFile")}</div>
          <div className="text-[11px] font-mono text-ink-500">
            {fmtBytes(file.size)}
          </div>
        </div>
      ) : (
        <textarea
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          spellCheck={false}
          className="flex-1 min-h-0 m-3 rounded-xl border border-brand-500/15 bg-ink-950/80 p-3 text-[12px] font-mono text-ink-100 leading-relaxed focus:outline-none focus:border-brand-500/40 resize-none"
          placeholder={loading ? t("loading") : ""}
        />
      )}

      {file?.truncated ? (
        <div className="px-4 py-2 text-[10px] text-amber-200/80 text-center font-mono border-t border-brand-500/10">
          {t("fileTruncated")}
        </div>
      ) : null}
    </div>
  );
}

export default FilesDrawer;
