"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, ErrorBanner, Pill } from "./Page";
import { SwitchControl } from "./SwitchControl";
import {
  clientApi,
  type WorkspaceSyncConfig,
  type WorkspaceSyncStatus,
} from "../lib/clientApi";

const EMPTY_CONFIG: WorkspaceSyncConfig = {
  enabled: false,
  provider: "git",
  remote: "",
  branch: "main",
  git_path: "nerya-workspace",
  remote_path: "nerya-workspace.tar.gz",
  username_ref: "",
  password_ref: "",
  includes: [],
  excludes: [],
};

export function WorkspaceSyncPanel() {
  const t = useTranslations("workspaceSync");
  const [status, setStatus] = useState<WorkspaceSyncStatus | null>(null);
  const [draft, setDraft] = useState<WorkspaceSyncConfig>(EMPTY_CONFIG);
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string>("");
  const [force, setForce] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await clientApi.workspaceSyncStatus();
      if (!next.ok) throw new Error(next.detail || next.error || t("loadFailed"));
      setStatus(next);
      setDraft(next.config);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  function patch<K extends keyof WorkspaceSyncConfig>(key: K, value: WorkspaceSyncConfig[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    setBusy("save");
    setResult("");
    try {
      const next = await clientApi.workspaceSyncConfig(draft);
      if (!next.ok) throw new Error(next.detail || next.error || t("saveFailed"));
      setStatus(next);
      setDraft(next.config);
      setError(null);
      setResult(t("saved"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy("");
    }
  }

  async function run(action: "pull" | "push" | "sync") {
    setBusy(action);
    setResult("");
    try {
      const response = await clientApi.workspaceSyncRun({ action, force });
      if (!response.ok) {
        const conflicts = response.conflicts?.length
          ? ` (${response.conflicts.join(", ")})`
          : "";
        throw new Error(`${response.detail || response.error || t("runFailed")}${conflicts}`);
      }
      setError(null);
      setResult(t("runDone", { action: t(action) }));
      setForce(false);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy("");
    }
  }

  const configured = Boolean(draft.remote.trim());
  const lastSync = status?.last_sync?.finished_at;

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <Pill tone={draft.enabled && configured ? "ok" : "neutral"}>
          {draft.enabled && configured ? t("ready") : t("disabled")}
        </Pill>
      }
    >
      {error ? <ErrorBanner error={error} /> : null}
      {result ? (
        <div className="mb-3 rounded-lg border border-positive/20 bg-positive/5 px-3 py-2 text-[12px] text-positive">
          {result}
        </div>
      ) : null}

      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-[180px_180px_1fr]">
          <label className="space-y-1.5">
            <span className="block text-[11px] font-medium text-ink-300">{t("enabled")}</span>
            <div className="flex min-h-9 items-center">
              <SwitchControl
                checked={draft.enabled}
                label={draft.enabled ? t("on") : t("off")}
                onCheckedChange={(value) => patch("enabled", value)}
              />
            </div>
          </label>
          <label className="space-y-1.5">
            <span className="block text-[11px] font-medium text-ink-300">{t("provider")}</span>
            <select
              className="input-dark text-xs"
              value={draft.provider}
              onChange={(event) => patch("provider", event.target.value as "git" | "webdav")}
            >
              <option value="git">Git</option>
              <option value="webdav">WebDAV</option>
            </select>
          </label>
          <label className="space-y-1.5">
            <span className="block text-[11px] font-medium text-ink-300">{t("remote")}</span>
            <input
              className="input-dark font-mono text-xs"
              value={draft.remote}
              onChange={(event) => patch("remote", event.target.value)}
              placeholder={draft.provider === "git" ? "git@github.com:org/workspace.git" : "https://dav.example.com/nerya"}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
            />
          </label>
        </div>

        {draft.provider === "git" ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="block text-[11px] font-medium text-ink-300">{t("branch")}</span>
              <input
                className="input-dark font-mono text-xs"
                value={draft.branch}
                onChange={(event) => patch("branch", event.target.value)}
                placeholder="main"
              />
            </label>
            <label className="space-y-1.5">
              <span className="block text-[11px] font-medium text-ink-300">{t("gitPath")}</span>
              <input
                className="input-dark font-mono text-xs"
                value={draft.git_path}
                onChange={(event) => patch("git_path", event.target.value)}
                placeholder="nerya-workspace"
              />
            </label>
            <span className="block text-[10.5px] text-ink-500 md:col-span-2">{t("gitCredentialHint")}</span>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="space-y-1.5">
              <span className="block text-[11px] font-medium text-ink-300">{t("remotePath")}</span>
              <input
                className="input-dark font-mono text-xs"
                value={draft.remote_path}
                onChange={(event) => patch("remote_path", event.target.value)}
              />
            </label>
            <label className="space-y-1.5">
              <span className="block text-[11px] font-medium text-ink-300">{t("usernameRef")}</span>
              <input
                className="input-dark font-mono text-xs"
                value={draft.username_ref}
                onChange={(event) => patch("username_ref", event.target.value)}
                placeholder="vault://webdav_username"
              />
            </label>
            <label className="space-y-1.5">
              <span className="block text-[11px] font-medium text-ink-300">{t("passwordRef")}</span>
              <input
                className="input-dark font-mono text-xs"
                value={draft.password_ref}
                onChange={(event) => patch("password_ref", event.target.value)}
                placeholder="vault://webdav_password"
              />
            </label>
          </div>
        )}

        <div className="rounded-lg border border-brand-500/15 bg-white/[0.02] px-3 py-2.5 text-[11px] leading-relaxed text-ink-500">
          <div>{t("safetyHint")}</div>
          {lastSync ? <div className="mt-1">{t("lastSync", { time: new Date(lastSync).toLocaleString() })}</div> : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-primary" disabled={Boolean(busy)} onClick={() => void save()}>
            {busy === "save" ? t("saving") : t("save")}
          </button>
          <button className="btn btn-ghost" disabled={Boolean(busy) || !draft.enabled || !configured} onClick={() => void run("pull")}>
            {busy === "pull" ? t("running") : t("pull")}
          </button>
          <button className="btn btn-ghost" disabled={Boolean(busy) || !draft.enabled || !configured} onClick={() => void run("sync")}>
            {busy === "sync" ? t("running") : t("sync")}
          </button>
          <button className="btn btn-ghost" disabled={Boolean(busy) || !draft.enabled || !configured} onClick={() => void run("push")}>
            {busy === "push" ? t("running") : t("push")}
          </button>
          <label className="ml-auto flex items-center gap-2 text-[11px] text-ink-500">
            <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
            {t("force")}
          </label>
        </div>
      </div>
    </Card>
  );
}
