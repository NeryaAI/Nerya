"use client";

import { Children, Fragment, isValidElement, useEffect, useId, useMemo, useRef, useState, type ButtonHTMLAttributes, type KeyboardEvent, type ReactNode } from "react";
import * as Popover from "@radix-ui/react-popover";
import { useLocale } from "next-intl";
import { CheckIcon, ChevronDownIcon, SearchIcon, XIcon } from "./icons";

type Choice = { value: string; label: ReactNode; text: string; disabled: boolean; group?: string };
type OptionProps = { value?: string | number; children?: ReactNode; disabled?: boolean; label?: string };

function plainText(node: ReactNode): string {
  return Children.toArray(node).map((child) => isValidElement<OptionProps>(child) ? plainText(child.props.children) : String(child)).join("");
}

/** Option children are a declarative catalogue, not extra visible DOM controls. */
function catalogue(children: ReactNode, group?: string, groupDisabled = false): Choice[] {
  const result: Choice[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement<OptionProps>(child)) return;
    if (child.type === Fragment) result.push(...catalogue(child.props.children, group, groupDisabled));
    else if (child.type === "optgroup") result.push(...catalogue(child.props.children, child.props.label, Boolean(child.props.disabled)));
    else if (child.type === "option") {
      const text = child.props.label ?? plainText(child.props.children);
      result.push({ value: String(child.props.value ?? text), label: child.props.children ?? text, text, group, disabled: groupDisabled || Boolean(child.props.disabled) });
    }
  });
  return result;
}

type Props = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "value" | "onChange" | "children"> & {
  value: string | number;
  onValueChange: (value: string) => void;
  children: ReactNode;
  searchable?: boolean;
  required?: boolean;
  placeholder?: string;
};

/** Select-only combobox for short lists; searchable catalogue for longer ones.
 * Radix owns the portal, positioning, dismissal and return focus. Navigation
 * previews an option; only explicit selection changes the controlled value.
 */
export function ChoiceSelect({ value, onValueChange, children, searchable, required, placeholder, disabled, className = "", name, ...props }: Props) {
  const zh = useLocale().startsWith("zh");
  const id = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const [invalid, setInvalid] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: "", time: 0 });
  const choices = useMemo(() => catalogue(children), [children]);
  const selected = choices.find((choice) => choice.value === String(value));
  const canSearch = searchable ?? choices.length > 7;
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return choices.filter((choice) => !needle || `${choice.text} ${choice.group ?? ""}`.toLocaleLowerCase().includes(needle));
  }, [choices, query]);
  const label = props["aria-label"];
  const activeId = active >= 0 && filtered[active] ? `${id}-option-${active}` : undefined;
  useEffect(() => { setInvalid(false); }, [value]);

  function changeOpen(next: boolean) {
    if (disabled) return;
    setOpen(next);
    if (next) {
      setQuery("");
      const index = choices.findIndex((choice) => choice.value === String(value) && !choice.disabled);
      setActive(index >= 0 ? index : choices.findIndex((choice) => !choice.disabled));
      typeahead.current = { text: "", time: 0 };
    }
  }
  function choose(index: number) {
    const choice = filtered[index];
    if (!choice || choice.disabled || disabled) return;
    setInvalid(false);
    if (choice.value !== String(value)) onValueChange(choice.value);
    setOpen(false);
  }
  function onKeys(event: KeyboardEvent<HTMLElement>) {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    const enabled = filtered.map((choice, index) => choice.disabled ? -1 : index).filter((index) => index >= 0);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) && (!canSearch || !["Home", "End"].includes(event.key))) {
      event.preventDefault();
      const position = enabled.indexOf(active);
      const next = event.key === "Home" ? enabled[0] : event.key === "End" ? enabled.at(-1) : event.key === "ArrowDown" ? enabled[(position + 1) % enabled.length] : position < 0 ? enabled.at(-1) : enabled[(position - 1 + enabled.length) % enabled.length];
      setActive(next ?? -1);
    } else if (event.key === "Enter" || (!canSearch && event.key === " ")) {
      event.preventDefault();
      choose(active);
    } else if (!canSearch && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      const now = Date.now();
      const text = `${now - typeahead.current.time < 700 ? typeahead.current.text : ""}${event.key}`.toLocaleLowerCase();
      typeahead.current = { text, time: now };
      const index = filtered.findIndex((choice) => !choice.disabled && choice.text.toLocaleLowerCase().startsWith(text));
      if (index >= 0) setActive(index);
    }
  }
  useEffect(() => {
    if (!open || !activeId) return;
    const option = document.getElementById(activeId), container = list.current;
    if (!option || !container) return;
    const item = option.getBoundingClientRect(), box = container.getBoundingClientRect();
    if (item.top < box.top) container.scrollTop -= box.top - item.top;
    else if (item.bottom > box.bottom) container.scrollTop += item.bottom - box.bottom;
  }, [open, activeId]);

  return <Popover.Root open={open} onOpenChange={changeOpen}>
    <Popover.Trigger asChild>
      <button {...props} ref={trigger} type="button" role="combobox" aria-haspopup="listbox" aria-expanded={open}
        aria-controls={`${id}-list`} aria-required={required || undefined} aria-invalid={invalid || props["aria-invalid"]}
        disabled={disabled} data-choice data-value={String(value)} className={`ui-choice ${className}`}
        title={props.title ?? selected?.text} onKeyDown={(event) => {
          props.onKeyDown?.(event);
          if (!event.defaultPrevented && ["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); changeOpen(true); }
        }}>
        <span className="min-w-0 flex-1 truncate text-left">{selected?.label ?? (value || placeholder || (zh ? "请选择" : "Choose an option"))}</span>
        <ChevronDownIcon size={14} className="shrink-0 text-[color:var(--text-muted)]" />
      </button>
    </Popover.Trigger>
    {(name || required) ? <select aria-hidden="true" tabIndex={-1} className="ui-choice-form-value" name={name} value={value} required={required} disabled={disabled}
      onChange={(event) => onValueChange(event.target.value)} onInvalid={(event) => { event.preventDefault(); setInvalid(true); trigger.current?.focus(); }}>{children}</select> : null}
    <Popover.Portal>
      <Popover.Content className="ui-choice-panel" align="start" sideOffset={6} collisionPadding={8}
        onOpenAutoFocus={(event) => { event.preventDefault(); (canSearch ? search.current : list.current)?.focus(); }}>
        {canSearch ? <div className="ui-choice-search">
          <SearchIcon size={15} aria-hidden="true" />
          <input ref={search} type="search" value={query} aria-label={zh ? "搜索选项" : "Search options"} placeholder={zh ? "输入关键词筛选…" : "Search options…"}
            aria-controls={`${id}-list`} aria-activedescendant={activeId} onKeyDown={onKeys}
            onChange={(event) => { setQuery(event.target.value); setActive(0); }} />
          {query ? <button type="button" className="ui-icon-button" aria-label={zh ? "清除搜索" : "Clear search"} onClick={() => { setQuery(""); setActive(choices.findIndex((choice) => !choice.disabled)); search.current?.focus(); }}><XIcon size={14} /></button> : null}
        </div> : null}
        <div ref={list} id={`${id}-list`} role="listbox" aria-label={label ?? (zh ? "可用选项" : "Available options")}
          tabIndex={canSearch ? -1 : 0} aria-activedescendant={!canSearch ? activeId : undefined} className="ui-choice-options" onKeyDown={!canSearch ? onKeys : undefined}>
          {filtered.map((choice, index) => <Fragment key={choice.value}>
            {choice.group && choice.group !== filtered[index - 1]?.group ? <div className="ui-choice-group" role="presentation">{choice.group}</div> : null}
            <div id={`${id}-option-${index}`} role="option" data-value={choice.value} aria-selected={choice.value === String(value)} aria-disabled={choice.disabled || undefined}
              data-active={index === active} className="ui-choice-option" onPointerMove={() => { if (!choice.disabled) setActive(index); }}
              onMouseDown={(event) => event.preventDefault()} onClick={() => choose(index)}>
              <span className="min-w-0 flex-1 break-words">{choice.label}</span>
              {choice.value === String(value) ? <CheckIcon size={14} className="shrink-0" /> : null}
            </div>
          </Fragment>)}
          {!filtered.length ? <p className="px-3 py-6 text-center text-sm text-[color:var(--text-muted)]" role="status">{zh ? "没有匹配的选项" : "No matching options"}</p> : null}
        </div>
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>;
}
