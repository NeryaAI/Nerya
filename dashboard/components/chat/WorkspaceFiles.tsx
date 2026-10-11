"use client";
import { copy as i18nCopy } from "../../lib/i18n";
import { useEffect, useState } from 'react';
import { useLocale } from 'next-intl';
import { clientApi, type WorkspaceFileEntry } from '../../lib/clientApi';
import { FileIcon, ChevronRightIcon } from '../icons';

export function WorkspaceFiles({ onOpenFile }: { onOpenFile?: (path: string) => void }) {
  const zh = useLocale().startsWith('zh');
  const [directory, setDirectory] = useState('.');
  const [entries, setEntries] = useState<WorkspaceFileEntry[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [truncated, setTruncated] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true); setError(''); setContent(''); setEntries([]); setTruncated(false);
    async function load() {
      try {
        if (selected) {
          const data = await clientApi.workspaceFileRead(selected);
          if (!data.ok) throw new Error(data.detail || data.error);
          if (!alive) return;
          setContent(data.binary ? (i18nCopy(zh, "copy.components_chat_WorkspaceFiles.001")) : data.content || '');
          setTruncated(!!data.truncated);
        } else {
          const data = await clientApi.workspaceFilesList(directory);
          if (!data.ok) throw new Error(data.detail || data.error);
          if (!alive) return;
          setEntries((data.entries || []).sort((a,b) => a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'dir' ? -1 : 1));
          setTruncated(!!data.truncated);
        }
      } catch (reason) { if (alive) setError(String(reason)); }
      finally { if (alive) setLoading(false); }
    }
    void load(); return () => { alive = false; };
  }, [directory, selected, revision, zh]);
  return <div className="flex h-full min-h-0 flex-col" data-testid="workspace-files">
    <header className="flex min-h-11 shrink-0 items-center gap-2 border-b border-[color:var(--line)] px-3 text-xs">
      <button type="button" className="btn-ghost shrink-0" disabled={!selected && directory === '.'} onClick={() => selected ? setSelected('') : setDirectory(directory.split('/').slice(0,-1).join('/') || '.')}>{i18nCopy(zh, "copy.components_chat_WorkspaceFiles.002")}</button>
      <span className="min-w-0 flex-1 truncate" title={selected || directory}>{selected || (directory === '.' ? (i18nCopy(zh, "copy.components_chat_WorkspaceFiles.009")) : directory)}</span>
      <button type="button" className="btn-ghost" onClick={() => setRevision(r => r+1)}>{i18nCopy(zh, "copy.components_chat_WorkspaceFiles.004")}</button>
    </header>
    <div className="min-h-0 flex-1 overflow-auto p-3">
      {loading ? <p role="status" className="p-3 text-sm">{i18nCopy(zh, "copy.components_chat_WorkspaceFiles.005")}</p> : error ? <p role="alert" className="break-words p-3 text-sm text-danger">{error}</p> : selected ? <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-6">{content || (i18nCopy(zh, "copy.components_chat_WorkspaceFiles.006"))}</pre> : entries.length ? entries.map(entry => <button type="button" key={entry.path} className="flex min-h-10 w-full items-center gap-2 rounded px-3 text-left text-sm hover:bg-[color:var(--panel-bg)] focus-visible:ring-2" onClick={() => entry.kind === 'dir' ? setDirectory(entry.path) : onOpenFile ? onOpenFile(entry.path) : setSelected(entry.path)}>
        {entry.kind === 'dir' ? <ChevronRightIcon size={15}/> : <FileIcon size={15}/>}<span className="min-w-0 truncate">{entry.name}</span>
      </button>) : <p className="p-3 text-sm text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_WorkspaceFiles.007")}</p>}
      {truncated && <p role="status" className="p-3 text-xs text-warn">{i18nCopy(zh, "copy.components_chat_WorkspaceFiles.008")}</p>}
    </div>
  </div>;
}
