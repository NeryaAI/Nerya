/**
 * Gateway channel draft type + helpers shared between the dedicated
 * `/gateway` workspace page (`components/GatewayChannelsPanel.tsx`) and
 * any legacy callers in `components/SettingsWorkspace.tsx`.
 *
 * The shape mirrors a single row in `messages/channels.yml` plus the
 * UI-only CSV strings the operator types into the form. `secrets` holds
 * plaintext credentials typed in for this session (they get vaulted on
 * save when the platform spec marks the field `secret` or `url`).
 * `secretRefs` holds the `vault://...` pointers returned by the
 * backend after a previous save (or that the operator manually pasted
 * in to reuse a vault entry).
 */

import type { GatewayChannelConfig, GatewayPlatformSpec } from "./clientApi";

export type GatewayDraft = {
  channel: string;
  kind: string;
  enabled: boolean;
  mode: string;
  polling: boolean;
  trade_notifications: boolean;
  approvals: boolean;
  auto_reply: boolean;
  allow_unknown_users: boolean;
  group_sessions_per_user: boolean;
  thread_sessions_per_user: boolean;
  topicsCsv: string;
  allowedChatIdsCsv: string;
  allowedUserIdsCsv: string;
  deniedUserIdsCsv: string;
  secrets: Record<string, string>;
  secretRefs: Record<string, string>;
  username: string;
  avatar_url: string;
  parse_mode: string;
  disable_web_page_preview: boolean;
  timeout_s: string;
};

export function emptyGatewayDraft(kind = "telegram"): GatewayDraft {
  return {
    channel: kind === "telegram" ? "telegram" : `${kind}_ops`,
    kind,
    enabled: true,
    mode: kind === "telegram" ? "polling" : "send_only",
    polling: kind === "telegram",
    trade_notifications: true,
    approvals: true,
    auto_reply: true,
    allow_unknown_users: true,
    group_sessions_per_user: true,
    thread_sessions_per_user: false,
    topicsCsv: "trades, approvals",
    allowedChatIdsCsv: "",
    allowedUserIdsCsv: "",
    deniedUserIdsCsv: "",
    secrets: {},
    secretRefs: {},
    username: "Nerya",
    avatar_url: "",
    parse_mode: "HTML",
    disable_web_page_preview: true,
    timeout_s: "10",
  };
}

function cfgString(cfg: Record<string, unknown>, key: string, fallback = ""): string {
  const value = cfg[key];
  return value === undefined || value === null ? fallback : String(value);
}

function cfgBool(cfg: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const value = cfg[key];
  return typeof value === "boolean" ? value : fallback;
}

function cfgList(cfg: Record<string, unknown>, key: string): string {
  const value = cfg[key];
  if (Array.isArray(value)) return value.map(String).join(", ");
  return typeof value === "string" ? value : "";
}

function refOf(channel: GatewayChannelConfig, ...keys: string[]): string {
  for (const key of keys) {
    const ref = channel.secret_refs?.[key]?.ref;
    if (ref) return ref;
  }
  return "";
}

export function gatewayDraftFromChannel(
  channel: GatewayChannelConfig,
  spec?: GatewayPlatformSpec,
): GatewayDraft {
  const cfg = channel.config || {};
  const topics = Array.isArray(cfg.topics) ? cfg.topics.map(String).join(", ") : "";
  const secrets: Record<string, string> = {};
  const secretRefs: Record<string, string> = {};
  if (spec?.secret_fields) {
    for (const field of spec.secret_fields) {
      const ref = channel.secret_refs?.[field.ref_key]?.ref;
      if (ref) secretRefs[field.key] = ref;
      if (field.kind === "id" || field.kind === "opaque") {
        const direct = cfgString(cfg, field.key);
        if (direct) secrets[field.key] = direct;
      }
    }
  } else {
    const botTokenRef = refOf(channel, "bot_token_ref", "token_ref");
    if (botTokenRef) secretRefs["bot_token"] = botTokenRef;
    const webhookRef = refOf(channel, "webhook_url_ref", "url_ref", "incoming_webhook_url_ref");
    if (webhookRef) secretRefs["webhook_url"] = webhookRef;
    const statusRef = refOf(channel, "status_webhook_url_ref");
    if (statusRef) secretRefs["status_webhook_url"] = statusRef;
    const directChat = cfgString(cfg, "chat_id");
    if (directChat) secrets["chat_id"] = directChat;
  }
  return {
    ...emptyGatewayDraft(channel.kind),
    channel: channel.channel,
    kind: channel.kind,
    enabled: channel.enabled,
    mode: cfgString(cfg, "mode", channel.mode),
    polling: cfgBool(cfg, "polling", channel.kind === "telegram"),
    trade_notifications: cfgBool(cfg, "trade_notifications", true),
    approvals: cfgBool(cfg, "approvals", true),
    auto_reply: cfgBool(cfg, "auto_reply", true),
    allow_unknown_users: cfgBool(cfg, "allow_unknown_users", true),
    group_sessions_per_user: cfgBool(cfg, "group_sessions_per_user", true),
    thread_sessions_per_user: cfgBool(cfg, "thread_sessions_per_user", false),
    topicsCsv: topics || "trades, approvals",
    allowedChatIdsCsv: cfgList(cfg, "allowed_chat_ids"),
    allowedUserIdsCsv: cfgList(cfg, "allowed_user_ids"),
    deniedUserIdsCsv: cfgList(cfg, "denied_user_ids"),
    secrets,
    secretRefs,
    username: cfgString(cfg, "username", "Nerya"),
    avatar_url: cfgString(cfg, "avatar_url"),
    parse_mode: cfgString(cfg, "parse_mode", "HTML"),
    disable_web_page_preview: cfgBool(cfg, "disable_web_page_preview", true),
    timeout_s: cfgString(cfg, "timeout_s", "10"),
  };
}

export function gatewayTopics(csv: string): string[] {
  return csv.split(",").map((part) => part.trim()).filter(Boolean);
}

export function gatewayCsvList(csv: string): string[] {
  return csv.split(/[,\n]/).map((part) => part.trim()).filter(Boolean);
}
