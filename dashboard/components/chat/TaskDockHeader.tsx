"use client";
import { Icon as NeryaGlyph } from "../icons";
import { copy as i18nCopy } from "../../lib/i18n";
import { useLocale } from 'next-intl';
import { useEffect } from 'react';
import * as Menu from '@radix-ui/react-dropdown-menu';
import type { WorkspaceTab } from './WorkspaceTabs';
import { PanelLeftIcon, PlusIcon, XIcon } from '../icons';

export function TaskDockHeader({ tabs, choices, selected, onSelect, onRemove, expanded, compact = false, onToggleSize, onClose }: {
  tabs: WorkspaceTab[]; choices: WorkspaceTab[]; selected: string; onSelect: (tab: string) => void; onRemove: (tab: string) => void;
  expanded: boolean; compact?: boolean; onToggleSize: () => void; onClose: () => void;
}) {
  const zh = useLocale().startsWith('zh');
  useEffect(() => {
    document.getElementById('task-dock-tab-' + selected)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [selected, tabs.length]);
  const quiet = 'inline-flex h-9 min-w-8 shrink-0 items-center justify-center rounded text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--text-muted)]';
  const closeLabel = compact || expanded ? (i18nCopy(zh, "copy.components_chat_TaskDockHeader.009")) : i18nCopy(zh, "copy.components_chat_TaskDockHeader.007");
  return <header className="flex min-h-11 shrink-0 items-center gap-1 border-b border-[color:var(--line)] px-1" data-testid="task-dock-header">
    <div role="tablist" aria-label={i18nCopy(zh, "copy.components_chat_TaskDockHeader.001")} className="flex min-w-0 flex-1 overflow-x-auto">
      {tabs.map((tab, index) => <div key={tab.id} className={'flex shrink-0 items-center border-b-2 ' + (selected === tab.id ? 'border-[color:var(--text-base)]' : 'border-transparent')}>
        <button type="button" role="tab" id={'task-dock-tab-' + tab.id} aria-controls={'task-dock-panel-' + tab.id} aria-selected={selected === tab.id} tabIndex={selected === tab.id ? 0 : -1}
          className="min-h-11 max-w-48 truncate px-3 text-xs focus-visible:outline focus-visible:outline-2" title={tab.label} onClick={() => onSelect(tab.id)} onKeyDown={event => {
            if (event.key === 'Delete') { event.preventDefault(); onRemove(tab.id); return; }
            const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
            if (next < 0) return;
            event.preventDefault(); onSelect(tabs[next].id);
          }}>{tab.label}</button>
        <button type="button" className={quiet} aria-label={(i18nCopy(zh, "copy.components_chat_TaskDockHeader.002")) + tab.label} onClick={() => onRemove(tab.id)}><XIcon size={12}/></button>
      </div>)}
    </div>
    <Menu.Root>
      <Menu.Trigger asChild><button id="task-dock-add" type="button" className={quiet} aria-label={i18nCopy(zh, "copy.components_chat_TaskDockHeader.003")} title={i18nCopy(zh, "copy.components_chat_TaskDockHeader.004")}><PlusIcon size={16}/></button></Menu.Trigger>
      <Menu.Portal><Menu.Content className="ui-select-menu min-w-44 max-h-80 overflow-y-auto" align="end" sideOffset={6} collisionPadding={8} onCloseAutoFocus={e => { e.preventDefault(); requestAnimationFrame(() => (document.querySelector<HTMLElement>('#task-workspace [role="tab"][aria-selected="true"]') || document.getElementById('task-dock-add'))?.focus()); }}>
        {choices.map(tab => <Menu.Item key={tab.id} className="ui-select-option" onSelect={() => onSelect(tab.id)}>{tab.label}</Menu.Item>)}
      </Menu.Content></Menu.Portal>
    </Menu.Root>
    <button type="button" className={quiet} aria-label={expanded ? (i18nCopy(zh, "copy.components_chat_TaskDockHeader.005")) : (i18nCopy(zh, "copy.components_chat_TaskDockHeader.006"))} onClick={onToggleSize}><NeryaGlyph name="arrowUpRight" size={18} className={expanded ? 'rotate-180' : ''} /></button>
    <button type="button" className={`${quiet} gap-1 px-2 text-xs`} aria-label={closeLabel} title={closeLabel} onClick={onClose}><PanelLeftIcon size={16} className="rotate-180"/>{(compact || expanded) && <span>{closeLabel}</span>}</button>
  </header>;
}
