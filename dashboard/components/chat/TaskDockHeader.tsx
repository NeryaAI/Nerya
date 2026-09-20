"use client";

import { useLocale } from 'next-intl';
import * as Menu from '@radix-ui/react-dropdown-menu';
import { WorkspaceTabs, type WorkspaceTab } from './WorkspaceTabs';
import { PanelLeftIcon } from '../icons';

export function TaskDockHeader({ tabs, selected, onSelect, expanded, onToggleSize, onClose }: {
  tabs: WorkspaceTab[]; selected: string; onSelect: (tab: string) => void;
  expanded: boolean; onToggleSize: () => void; onClose: () => void;
}) {
  const zh = useLocale().startsWith('zh');
  const quiet = 'inline-flex h-9 w-8 shrink-0 items-center justify-center rounded text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--text-muted)]';
  return <header className="flex min-h-11 shrink-0 items-center gap-1 border-b border-[color:var(--line)] px-1" data-testid="task-dock-header">
    <div className="min-w-0 flex-1 [&>div]:border-0 [&_button]:text-xs [&_button]:px-3">
      <WorkspaceTabs id="task-dock" label={zh ? '任务工作区' : 'Task workspace'} tabs={tabs} value={selected} onChange={onSelect}/>
    </div>
    <Menu.Root>
      <Menu.Trigger asChild><button className={quiet} aria-label={zh ? '工作区布局' : 'Workspace layout'}>⋯</button></Menu.Trigger>
      <Menu.Portal><Menu.Content className="ui-select-menu min-w-40" align="end" sideOffset={6} collisionPadding={8}>
        <Menu.Item className="ui-select-option" onSelect={onToggleSize}>{expanded ? (zh ? '还原侧栏' : 'Restore side panel') : (zh ? '放大工作区' : 'Expand workspace')}</Menu.Item>
      </Menu.Content></Menu.Portal>
    </Menu.Root>
    <button className={quiet} aria-label={zh ? '收起工作区' : 'Hide workspace'} title={zh ? '收起工作区' : 'Hide workspace'} onClick={onClose}><PanelLeftIcon size={16}/></button>
  </header>;
}
