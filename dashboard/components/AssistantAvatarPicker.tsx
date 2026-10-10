"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { AssistantAvatar } from "./NeryaLogo";
import { roles } from "../lib/roleAvatars";

const presets = [{value: "", label: "mascot"}, ...roles.map(role => ({value: `role:${role}`, label: role}))];
const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
const MAX_AVATAR_LENGTH = "data:image/png;base64,".length + 4 * Math.ceil(96 * 1024 / 3);

async function avatarImage(file: File): Promise<string> {
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw new Error("avatarUnsupported");
  if (file.size > MAX_IMAGE_BYTES) throw new Error("avatarTooLarge");
  const image = await createImageBitmap(file);
  try {
    if (!image.width || !image.height || image.width * image.height > 36_000_000) throw new Error("avatarTooLarge");
    const canvas = document.createElement("canvas"), context = canvas.getContext("2d");
    if (!context) throw new Error("avatarInvalid");
    const side = Math.min(image.width, image.height);
    for (const size of [256, 128]) {
      canvas.width = size; canvas.height = size;
      context.drawImage(image, (image.width - side) / 2, (image.height - side) / 2, side, side, 0, 0, size, size);
      const value = canvas.toDataURL("image/png");
      if (value.length <= MAX_AVATAR_LENGTH) return value;
    }
    throw new Error("avatarTooLarge");
  } finally { image.close(); }
}

export function AssistantAvatarPicker({value, onChange, disabled, main, onProcessing}: {
  value: string; onChange: (value: string) => void; disabled?: boolean; main?: boolean; onProcessing: (processing: boolean) => void;
}) {
  const t = useTranslations("assistants"), group = useId();
  const picker = useRef<HTMLInputElement>(null), generation = useRef(0);
  const [processing, setProcessing] = useState(false), [error, setError] = useState("");
  const initial = !presets.some(preset => preset.value === value) && !(main && value === "N") ? value : "";
  const [custom, setCustom] = useState(initial);
  useEffect(() => () => { generation.current++; }, []);
  async function upload(file?: File) {
    if (!file || disabled || processing) return;
    const attempt = ++generation.current;
    setProcessing(true); onProcessing(true); setError("");
    try {
      const image = await avatarImage(file);
      if (attempt === generation.current) { setCustom(image); onChange(image); }
    } catch (error) {
      if (attempt === generation.current) setError(error instanceof Error && ["avatarUnsupported", "avatarTooLarge"].includes(error.message) ? error.message : "avatarInvalid");
    } finally { if (attempt === generation.current) { setProcessing(false); onProcessing(false); } }
  }
  const options = [...presets, ...(custom ? [{value: custom, label: "custom" as const}] : [])];
  return <fieldset disabled={disabled || processing} className="min-w-0" data-testid="assistant-avatar-picker">
    <legend className="mb-3 text-sm font-medium">{t("avatar")}</legend>
    <div className="grid max-h-64 grid-cols-5 gap-2 overflow-y-auto overscroll-contain p-1">
      {options.map(option => {
        const selected = value === option.value || (option.value === "" && main && value === "N");
        return <label key={option.label} className="relative min-w-0 cursor-pointer" title={t("avatarPresets." + option.label)}>
          <input type="radio" name={group} value={option.label} checked={Boolean(selected)} onChange={() => { setError(""); onChange(option.value); }}
            onFocus={event => event.currentTarget.parentElement?.scrollIntoView({block: "nearest", inline: "nearest"})} className="peer sr-only"/>
          <span className="flex min-h-16 flex-col items-center gap-1.5 rounded-xl border border-transparent px-1 py-2 text-xs text-[color:var(--text-muted)] peer-checked:border-brand-400 peer-checked:bg-brand-500/10 peer-focus-visible:ring-2 peer-focus-visible:ring-brand-400 hover:bg-[color:var(--panel-bg)]">
            <AssistantAvatar profile={{id: main ? "main" : "", name: "", avatar: option.value}} size={40}/><span className="block max-w-full truncate">{t("avatarPresets." + option.label)}</span>
          </span>
        </label>;
      })}
    </div>
    <input ref={picker} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" aria-label={t("uploadAvatar")}
      onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; void upload(file); }}/>
    <button type="button" className="mt-3 min-h-11 rounded-lg border border-[color:var(--line)] px-3 text-sm hover:bg-[color:var(--panel-bg)]" onClick={() => picker.current?.click()}>{t(processing ? "processingAvatar" : "uploadAvatar")}</button>
    <p className="mt-2 text-xs text-[color:var(--text-muted)]">{t("avatarUploadHint")}</p>
    {error ? <p role="alert" className="mt-2 text-xs text-danger">{t(error)}</p> : null}
  </fieldset>;
}
