"use client";

/**
 * Self-contained Gateway channels workspace.
 *
 * Renders the full pick-a-platform → configure-just-that-platform UX:
 *
 *  • Left/top: list of currently configured channels, each clickable
 *    to load the draft into the form.
 *  • Right/bottom: full per-platform configuration form. The platform
 *    picker drives which secret fields and inline setup steps appear
 *    so the operator never sees docs for a platform they're not
 *    configuring. The selected platform's "open setup docs" link is
 *    pinned at the top of the form so they can jump to the official
 *    docs without leaving the page.
 *  • Save / Test / Delete / New buttons sit at the bottom of the
 *    form and round-trip via `clientApi.gatewayConfig*`.
 *
 * This is the surface that `/gateway` mounts as its primary "Channels"
 * tab. It used to live inside `SettingsWorkspace.tsx` (Access tab) +
 * required the operator to jump out of the Gateway page to configure
 * anything; that arrangement was contradictory ("go to /gateway, see a
 * channel is broken, can't fix it from here, go to /settings, come
 * back to /gateway to retest"). This component owns its own data so
 * neither the Settings page nor the Gateway page have to coordinate
 * state.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Advanced, Card, Pill } from "./Page";
import { SwitchControl } from "./SwitchControl";
import { Select as PortalSelect } from "./Select";
import { Select } from "./Select";
import { CheckIcon, RefreshIcon, SettingsIcon, SparkIcon } from "./icons";
import { clientApi } from "../lib/clientApi";
import type {
  GatewayChannelConfig,
  GatewayPlatformSpec,
  GatewayUpsertRequest,
} from "../lib/clientApi";
import { toast } from "../lib/dialogs";
import {
  emptyGatewayDraft,
  gatewayCsvList,
  gatewayDraftFromChannel,
  gatewayTopics,
  type GatewayDraft,
} from "../lib/gatewayDraft";

function Row({
  label,
  desc,
  children,
}: {
  label: string;
  desc?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-brand-500/10 py-3 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] text-ink-100">{label}</div>
        {desc ? <div className="mt-0.5 text-[11px] text-ink-400">{desc}</div> : null}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="block text-[12px] text-ink-300">
      <span className="flex items-center justify-between gap-2">
        <span>{label}</span>
        {hint ? <span className="text-[10px] text-ink-500">{hint}</span> : null}
      </span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

export function GatewayChannelsPanel() {
  const t = useTranslations("settings.gatewayCard");
  const tCommon = useTranslations("common");

  const [platforms, setPlatforms] = useState<GatewayPlatformSpec[]>([]);
  const [channels, setChannels] = useState<GatewayChannelConfig[]>([]);
  const [draft, setDraft] = useState<GatewayDraft>(() => emptyGatewayDraft());
  const [busy, setBusy] = useState("");
  const [testText, setTestText] = useState("Nerya gateway test message.");
  const [result, setResult] = useState<string | null>(null);
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);

  const platformMap = useMemo(() => {
    const map = new Map<string, GatewayPlatformSpec>();
    for (const p of platforms) map.set(p.id, p);
    return map;
  }, [platforms]);

  const platformOptions = useMemo(() => {
    const rows = platforms.filter((p) => p.id !== "local");
    return rows.length
      ? rows
      : [
          { id: "telegram", title: "Telegram", alias_id: "telegram", status: "native", inbound: "polling", outbound: "bot_api", typing: "sendChatAction", menu: "setMyCommands", support_level: "tested" },
          { id: "discord", title: "Discord", alias_id: "discord", status: "webhook", inbound: "generic_inbound", outbound: "webhook", typing: "status_webhook", menu: "slash_commands_scaffold", support_level: "send_only" },
          { id: "webhook", title: "Generic Webhook", alias_id: "webhook", status: "native", inbound: "http", outbound: "json_webhook", typing: "status_webhook", menu: "none", support_level: "full_duplex" },
        ];
  }, [platforms]);

  const configuredCount = useMemo(
    () => channels.filter((c) => c.configured && c.enabled).length,
    [channels],
  );

  const selectedPlatform = platformMap.get(draft.kind);
  const isTelegram = draft.kind === "telegram";

  const applyConfig = useCallback(
    (cfg: {
      channels?: GatewayChannelConfig[];
      platforms?: GatewayPlatformSpec[];
      status?: Record<string, unknown>;
    } | null) => {
      if (!cfg) return;
      const ch = cfg.channels || [];
      const ps = cfg.platforms || [];
      setChannels(ch);
      setPlatforms(ps);
      setStatus(cfg.status || null);
      const lookup = new Map<string, GatewayPlatformSpec>();
      for (const p of ps) lookup.set(p.id, p);
      setDraft((current) => {
        const match = ch.find((row) => row.channel === current.channel);
        if (match) return gatewayDraftFromChannel(match, lookup.get(match.kind));
        if (ch[0] && (!current.channel || current.channel === "telegram")) {
          const head = ch[0];
          return gatewayDraftFromChannel(head, lookup.get(head.kind));
        }
        return current;
      });
    },
    [],
  );

  const load = useCallback(async () => {
    try {
      const cfg = await clientApi.gatewayConfig();
      if (!cfg.ok) throw new Error(cfg.error || "cannot load gateway config");
      applyConfig(cfg);
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  useEffect(() => {
    void load();
  }, [load]);

  function setKind(kind: string) {
    setDraft((current) => {
      const next = emptyGatewayDraft(kind);
      return {
        ...next,
        channel:
          current.channel && current.channel !== "telegram"
            ? current.channel
            : next.channel,
      };
    });
    setResult(null);
  }

  function setSecret(key: string, value: string) {
    setDraft((cur) => ({ ...cur, secrets: { ...cur.secrets, [key]: value } }));
  }

  function setSecretRef(key: string, value: string) {
    setDraft((cur) => ({
      ...cur,
      secretRefs: { ...cur.secretRefs, [key]: value },
    }));
  }

  function buildUpsertPayload(): GatewayUpsertRequest {
    const channel = draft.channel.trim().toLowerCase();
    const body: GatewayUpsertRequest = {
      channel,
      kind: draft.kind,
      enabled: draft.enabled,
      mode: draft.mode,
      trade_notifications: draft.trade_notifications,
      approvals: draft.approvals,
      auto_reply: draft.auto_reply,
      allow_unknown_users: draft.allow_unknown_users,
      group_sessions_per_user: draft.group_sessions_per_user,
      thread_sessions_per_user: draft.thread_sessions_per_user,
      topics: gatewayTopics(draft.topicsCsv),
      allowed_chat_ids: gatewayCsvList(draft.allowedChatIdsCsv),
      allowed_user_ids: gatewayCsvList(draft.allowedUserIdsCsv),
      denied_user_ids: gatewayCsvList(draft.deniedUserIdsCsv),
    };
    const spec = platformMap.get(draft.kind);
    const fields = spec?.secret_fields || [];
    if (fields.length) {
      for (const field of fields) {
        const plain = (draft.secrets[field.key] || "").trim();
        const ref = (draft.secretRefs[field.key] || "").trim();
        const isVaulted = field.kind === "secret" || field.kind === "url";
        if (isVaulted) {
          if (plain) body[field.key] = plain;
          else if (ref) body[field.ref_key] = ref;
        } else if (plain) {
          body[field.key] = plain;
        }
      }
    } else {
      const botToken = (draft.secrets["bot_token"] || "").trim();
      const botRef = (draft.secretRefs["bot_token"] || "").trim();
      if (botToken) body.bot_token = botToken;
      else if (botRef) body.bot_token_ref = botRef;
      const webhook = (draft.secrets["webhook_url"] || "").trim();
      const webhookRef = (draft.secretRefs["webhook_url"] || "").trim();
      if (draft.kind === "webhook") {
        if (webhook) body.url = webhook;
        else if (webhookRef) body.url_ref = webhookRef;
      } else {
        if (webhook) body.webhook_url = webhook;
        else if (webhookRef) body.webhook_url_ref = webhookRef;
      }
      const statusUrl = (draft.secrets["status_webhook_url"] || "").trim();
      const statusRef = (draft.secretRefs["status_webhook_url"] || "").trim();
      if (statusUrl) body.status_webhook_url = statusUrl;
      else if (statusRef) body.status_webhook_url_ref = statusRef;
      const chatId = (draft.secrets["chat_id"] || "").trim();
      if (chatId) body.chat_id = chatId;
    }
    if (isTelegram) {
      body.polling = draft.polling;
      if (draft.parse_mode.trim()) body.parse_mode = draft.parse_mode.trim();
      body.disable_web_page_preview = draft.disable_web_page_preview;
    }
    if (draft.username.trim()) body.username = draft.username.trim();
    if (draft.avatar_url.trim()) body.avatar_url = draft.avatar_url.trim();
    const timeout = Number(draft.timeout_s);
    if (Number.isFinite(timeout) && timeout > 0) body.timeout_s = timeout;
    return body;
  }

  async function save() {
    setBusy("save");
    setResult(null);
    try {
      const res = await clientApi.gatewayConfigUpsert(buildUpsertPayload());
      if (!res.ok) throw new Error(res.error || "gateway save failed");
      if (res.config) applyConfig(res.config);
      if (res.channel) {
        setDraft(gatewayDraftFromChannel(res.channel, platformMap.get(res.channel.kind)));
      } else {
        setDraft((current) => ({ ...current, secrets: {} }));
      }
      toast({ tone: "ok", message: t("saved") });
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (!draft.channel.trim()) return;
    setBusy("delete");
    setResult(null);
    try {
      const res = await clientApi.gatewayConfigDelete(draft.channel.trim().toLowerCase());
      if (!res.ok) throw new Error(res.error || "gateway delete failed");
      if (res.config) applyConfig(res.config);
      setDraft(emptyGatewayDraft(draft.kind));
      toast({ tone: "ok", message: t("deleted") });
    } catch (e) {
      toast({
        tone: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  }

  async function runTest() {
    if (!draft.channel.trim()) return;
    setBusy("test");
    setResult(null);
    try {
      const res = await clientApi.gatewayConfigTest({
        channel: draft.channel.trim().toLowerCase(),
        text: testText.trim() || "Nerya gateway test message.",
        mode: "agent",
      });
      const note = res.delivery?.delivery_note ? String(res.delivery.delivery_note) : "";
      if (!res.ok) throw new Error(res.detail || res.error || note || "gateway test failed");
      const turnId = res.agent?.turn_id || "";
      const reply = res.reply_text ? String(res.reply_text).slice(0, 220) : "";
      const summary = turnId
        ? `${t("agentTestDelivered")} ${turnId}${reply ? ` · ${reply}` : ""}`
        : note || t("testDelivered");
      // Keep the test summary visible inline (it can be long, like a full
      // agent reply) AND toast the success so the operator sees the win
      // without scrolling back to the form.
      setResult(summary);
      toast({ tone: "ok", message: t("testDelivered") });
      await load();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setResult(msg);
      toast({ tone: "error", message: msg });
    } finally {
      setBusy("");
    }
  }

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <div className="flex items-center gap-2">
          <Pill tone={configuredCount ? "ok" : "warn"}>
            {t("configuredCount", { count: configuredCount })}
          </Pill>
          <button
            type="button"
            className="btn btn-ghost text-[11px]"
            onClick={() => void load()}
            disabled={Boolean(busy) || loading}
          >
            <RefreshIcon size={14} /> {tCommon("refresh")}
          </button>
        </div>
      }
    >
      <div className="space-y-3">
        <div className="embedded-list-scroll-sm rounded-lg border border-brand-500/10 bg-ink-950/35">
          {channels.length ? (
            channels.map((c) => (
              <button
                key={c.channel}
                type="button"
                className={`flex w-full items-center justify-between gap-3 border-b border-brand-500/10 px-3 py-2 text-left text-xs last:border-b-0 ${
                  draft.channel === c.channel ? "bg-brand-500/10" : "hover:bg-white/5"
                }`}
                onClick={() => {
                  setDraft(gatewayDraftFromChannel(c, platformMap.get(c.kind)));
                  setResult(null);
                }}
              >
                <span className="min-w-0">
                  <span className="block truncate font-mono text-ink-100">{c.channel}</span>
                  <span className="text-[10px] text-ink-500">
                    {c.title} · {c.mode}
                  </span>
                </span>
                <span className="flex shrink-0 gap-1">
                  <Pill tone={c.configured && c.enabled ? "ok" : "warn"}>
                    {c.configured && c.enabled ? t("ready") : t("incomplete")}
                  </Pill>
                </span>
              </button>
            ))
          ) : (
            <div className="px-3 py-6 text-center text-[12px] text-ink-500">{t("empty")}</div>
          )}
        </div>

        <div className="grid grid-cols-1 gap-3">
          <Field
            label={t("platformLabel")}
            hint={selectedPlatform?.support_level || "gateway"}
          >
            <PortalSelect
              value={draft.kind}
              onChange={(value) => setKind(value)}
              options={platformOptions.map((p) => ({
                value: p.id,
                label: `${p.title} (${p.id})`,
              }))}
              size="sm"
              ariaLabel={t("platformLabel")}
            />
          </Field>
          <Field label={t("channelLabel")} hint={t("channelHint")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.channel}
              onChange={(e) =>
                setDraft((cur) => ({ ...cur, channel: e.target.value.toLowerCase() }))
              }
              placeholder={t("channelPlaceholder")}
            />
          </Field>

          <Row label={t("enabled")} desc={t("enabledDesc")}>
            <SwitchControl
              checked={draft.enabled}
              label={t("enabled")}
              onCheckedChange={(v) => setDraft((cur) => ({ ...cur, enabled: v }))}
            />
          </Row>

          <div className="space-y-1 pt-2">
            <div className="text-[11px] font-medium text-ink-300">
              {t("groupBehaviorTitle")}
            </div>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              <Row label={t("tradeNotifications")} desc={t("tradeNotificationsDesc")}>
                <SwitchControl
                  checked={draft.trade_notifications}
                  label={t("tradeNotifications")}
                  onCheckedChange={(v) =>
                    setDraft((cur) => ({ ...cur, trade_notifications: v }))
                  }
                />
              </Row>
              <Row label={t("autoReply")} desc={t("autoReplyDesc")}>
                <SwitchControl
                  checked={draft.auto_reply}
                  label={t("autoReply")}
                  onCheckedChange={(v) => setDraft((cur) => ({ ...cur, auto_reply: v }))}
                />
              </Row>
            </div>
            <Field label={t("topicsLabel")} hint={t("topicsHint")}>
              <input
                className="input-dark text-xs"
                value={draft.topicsCsv}
                onChange={(e) =>
                  setDraft((cur) => ({ ...cur, topicsCsv: e.target.value }))
                }
                placeholder={t("topicsPlaceholder")}
              />
            </Field>
          </div>

          <Advanced
            title={t("groupAccessTitle")}
            storageKey="nerya.gateway.advanced.access"
          >
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              <Row label={t("allowUnknownUsers")} desc={t("allowUnknownUsersDesc")}>
                <SwitchControl
                  checked={draft.allow_unknown_users}
                  label={t("allowUnknownUsers")}
                  onCheckedChange={(v) =>
                    setDraft((cur) => ({ ...cur, allow_unknown_users: v }))
                  }
                />
              </Row>
              <Row label={t("groupSessionsPerUser")} desc={t("groupSessionsPerUserDesc")}>
                <SwitchControl
                  checked={draft.group_sessions_per_user}
                  label={t("groupSessionsPerUser")}
                  onCheckedChange={(v) =>
                    setDraft((cur) => ({ ...cur, group_sessions_per_user: v }))
                  }
                />
              </Row>
              <Row label={t("threadSessionsPerUser")} desc={t("threadSessionsPerUserDesc")}>
                <SwitchControl
                  checked={draft.thread_sessions_per_user}
                  label={t("threadSessionsPerUser")}
                  onCheckedChange={(v) =>
                    setDraft((cur) => ({ ...cur, thread_sessions_per_user: v }))
                  }
                />
              </Row>
            </div>
            <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
              <Field label={t("allowedUsers")} hint={t("csvHint")}>
                <input
                  className="input-dark font-mono text-xs"
                  value={draft.allowedUserIdsCsv}
                  onChange={(e) =>
                    setDraft((cur) => ({ ...cur, allowedUserIdsCsv: e.target.value }))
                  }
                  placeholder={t("allowedUsersPlaceholder")}
                />
              </Field>
              <Field label={t("allowedChats")} hint={t("csvHint")}>
                <input
                  className="input-dark font-mono text-xs"
                  value={draft.allowedChatIdsCsv}
                  onChange={(e) =>
                    setDraft((cur) => ({ ...cur, allowedChatIdsCsv: e.target.value }))
                  }
                  placeholder={t("allowedChatsPlaceholder")}
                />
              </Field>
              <Field label={t("deniedUsers")} hint={t("csvHint")}>
                <input
                  className="input-dark font-mono text-xs"
                  value={draft.deniedUserIdsCsv}
                  onChange={(e) =>
                    setDraft((cur) => ({ ...cur, deniedUserIdsCsv: e.target.value }))
                  }
                  placeholder={t("deniedUsersPlaceholder")}
                />
              </Field>
            </div>
          </Advanced>

          <div className="space-y-3 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
            {selectedPlatform ? (
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-brand-500/10 pb-2">
                <div className="min-w-0">
                  <div className="text-[12px] text-ink-100">
                    {selectedPlatform.title}
                    <span className="ml-2 text-[11px] text-ink-500 font-medium">
                      {selectedPlatform.support_level}
                    </span>
                  </div>
                  {selectedPlatform.notes ? (
                    <div className="mt-0.5 text-[10px] text-ink-500">{selectedPlatform.notes}</div>
                  ) : null}
                </div>
                {selectedPlatform.docs_url ? (
                  <a
                    href={selectedPlatform.docs_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-md border border-brand-500/30 bg-brand-500/10 px-2 py-1 text-[10px] text-brand-100 hover:bg-brand-500/20"
                    title={selectedPlatform.docs_url}
                  >
                    {t("setupDocs")} ↗
                  </a>
                ) : null}
              </div>
            ) : null}

            {selectedPlatform?.setup_steps?.length ? (
              <details
                className="rounded-md border border-brand-500/10 bg-ink-950/40 p-2 text-[11px] text-ink-300"
                open
              >
                <summary className="cursor-pointer text-[11px] text-ink-200">
                  {t("setupChecklist")}
                </summary>
                <ol className="mt-2 ml-4 list-decimal space-y-1 text-[11px] text-ink-400">
                  {selectedPlatform.setup_steps.map((step, idx) => (
                    <li key={idx}>{step}</li>
                  ))}
                </ol>
              </details>
            ) : null}

            {(selectedPlatform?.secret_fields || []).map((field) => {
              const isVaulted = field.kind === "secret" || field.kind === "url";
              const refValue = draft.secretRefs[field.key] || "";
              const plainValue = draft.secrets[field.key] || "";
              const reqHint = field.required ? t("required") : t("optional");
              if (!isVaulted) {
                return (
                  <Field
                    key={field.key}
                    label={field.label}
                    hint={`${field.kind} · ${reqHint}`}
                  >
                    <input
                      className="input-dark font-mono text-xs"
                      value={plainValue}
                      onChange={(e) => setSecret(field.key, e.target.value)}
                      placeholder={field.placeholder || ""}
                    />
                    {field.description ? (
                      <div className="mt-1 text-[10px] text-ink-500">{field.description}</div>
                    ) : null}
                  </Field>
                );
              }
              return (
                <div key={field.key} className="space-y-1">
                  <Field
                    label={`${field.label} · ${t("vaultRefSuffix")}`}
                    hint={t("vaultRefHint")}
                  >
                    <input
                      className="input-dark font-mono text-xs"
                      value={refValue}
                      onChange={(e) => setSecretRef(field.key, e.target.value)}
                      placeholder={`vault://gateway_${draft.kind}_${field.key}`}
                    />
                  </Field>
                  <Field
                    label={`${t("newPrefix")} ${field.label}`}
                    hint={`${reqHint} · ${t("secretHint")}`}
                  >
                    <input
                      className="input-dark font-mono text-xs"
                      type={field.kind === "secret" ? "password" : "text"}
                      value={plainValue}
                      onChange={(e) => setSecret(field.key, e.target.value)}
                      placeholder={refValue ? t("unchangedSecret") : field.placeholder || ""}
                    />
                  </Field>
                  {field.description ? (
                    <div className="text-[10px] text-ink-500">{field.description}</div>
                  ) : null}
                </div>
              );
            })}

            {isTelegram ? (
              <div className="grid grid-cols-2 gap-2 border-t border-brand-500/10 pt-3">
                <Field label={t("mode")}>
                  <Select
                    value={draft.mode}
                    onChange={(v) =>
                      setDraft((cur) => ({
                        ...cur,
                        mode: v,
                        polling: v === "polling",
                      }))
                    }
                    options={[
                      { value: "polling", label: t("polling") },
                      { value: "webhook", label: t("webhookMode") },
                    ]}
                  />
                </Field>
                <Row label={t("polling")} desc={t("pollingDesc")}>
                  <SwitchControl
                    checked={draft.polling}
                    label={t("polling")}
                    onCheckedChange={(v) =>
                      setDraft((cur) => ({
                        ...cur,
                        polling: v,
                        mode: v ? "polling" : "webhook",
                      }))
                    }
                  />
                </Row>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 border-t border-brand-500/10 pt-3">
                <Field label={t("username")} hint={t("optional")}>
                  <input
                    className="input-dark text-xs"
                    value={draft.username}
                    onChange={(e) => setDraft((cur) => ({ ...cur, username: e.target.value }))}
                    placeholder={t("usernamePlaceholder")}
                  />
                </Field>
                <Field label={t("timeout")} hint="sec">
                  <input
                    className="input-dark font-mono text-xs"
                    value={draft.timeout_s}
                    onChange={(e) => setDraft((cur) => ({ ...cur, timeout_s: e.target.value }))}
                    placeholder={t("timeoutPlaceholder")}
                  />
                </Field>
              </div>
            )}
          </div>

          <Field label={t("testMessage")} hint={t("testHint")}>
            <input
              className="input-dark text-xs"
              value={testText}
              onChange={(e) => setTestText(e.target.value)}
            />
          </Field>

          {result ? (
            <div className="rounded-md border border-brand-500/10 bg-ink-950/35 px-3 py-2 text-[11px] text-ink-300">
              {result}
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void save()}
              disabled={Boolean(busy) || !draft.channel.trim()}
            >
              <CheckIcon size={14} />
              {busy === "save" ? tCommon("saving") : t("save")}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void runTest()}
              disabled={Boolean(busy) || !draft.channel.trim()}
            >
              <SparkIcon size={14} />
              {busy === "test" ? t("testing") : t("test")}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                setDraft(emptyGatewayDraft(draft.kind));
                setResult(null);
              }}
              disabled={Boolean(busy)}
            >
              <SettingsIcon size={14} />
              {t("new")}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void remove()}
              disabled={
                Boolean(busy) || !channels.some((row) => row.channel === draft.channel)
              }
            >
              {busy === "delete" ? t("deleting") : tCommon("delete")}
            </button>
          </div>

          {status ? (
            <div className="text-[10px] text-ink-500">
              {t("runtimeStatus", {
                count: Number(status.configured_gateway_count || 0),
              })}
            </div>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
