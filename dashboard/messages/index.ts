import enSdkAccounts from "./en/sdk-accounts.json";
import zhSdkAccounts from "./zh/sdk-accounts.json";
import enAccountConnection from "./en/account-connection.json";
import zhAccountConnection from "./zh/account-connection.json";
import enChatHistory from "./en/chat-history.json";
import zhChatHistory from "./zh/chat-history.json";
import enComposer from "./en/composer.json";
import zhComposer from "./zh/composer.json";
import enResearchWorkspace from "./en/research-workspace.json";
import zhResearchWorkspace from "./zh/research-workspace.json";
import enWorkflowExperience from "./en/workflow-experience.json";
import zhWorkflowExperience from "./zh/workflow-experience.json";
import enDesktop from "./en/desktop.json";
import zhDesktop from "./zh/desktop.json";
import enBrowser from "./en/browser.json";
import zhBrowser from "./zh/browser.json";
import enChat from "./en/chat.json";
import zhChat from "./zh/chat.json";
import enCore from "./en/core.json";
import zhCore from "./zh/core.json";
import enEvolution from "./en/evolution.json";
import zhEvolution from "./zh/evolution.json";
import enMisc from "./en/misc.json";
import zhMisc from "./zh/misc.json";
import enOnboarding from "./en/onboarding.json";
import zhOnboarding from "./zh/onboarding.json";
import enSettings from "./en/settings.json";
import zhSettings from "./zh/settings.json";
import enSkills from "./en/skills.json";
import zhSkills from "./zh/skills.json";
import enStrategies from "./en/strategies.json";
import zhStrategies from "./zh/strategies.json";
import enTrading from "./en/trading.json";
import zhTrading from "./zh/trading.json";
import enWorkflows from "./en/workflows.json";
import zhWorkflows from "./zh/workflows.json";

type MessageValue = string | { [key: string]: MessageValue };

function isMessageMap(value: unknown): value is Record<string, MessageValue> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function copyOf(resource: unknown): Record<string, MessageValue> {
  if (!isMessageMap(resource)) return {};
  return Object.entries(resource).reduce<Record<string, MessageValue>>((messages, [key, value]) =>
    mergeMessages(messages, key === "copy" && isMessageMap(value) ? value : mergeMessages({}, copyOf(value))), {});
}

function mergeMessages(
  target: Record<string, MessageValue>,
  source: Record<string, MessageValue>,
): Record<string, MessageValue> {
  return Object.entries(source).reduce<Record<string, MessageValue>>((merged, [key, value]) => ({
    ...merged,
    [key]: isMessageMap(merged[key]) && isMessageMap(value)
      ? mergeMessages(merged[key] as Record<string, MessageValue>, value)
      : value,
  }), target);
}

export const en = {
  ...enSdkAccounts,
  ...enAccountConnection,
  ...enChatHistory,
  ...enComposer,
  ...enResearchWorkspace,
  ...enWorkflowExperience,
  ...enDesktop,
  ...enBrowser,
  ...enChat,
  ...enCore,
  ...enEvolution,
  ...enMisc,
  ...enOnboarding,
  ...enSettings,
  ...enSkills,
  ...enStrategies,
  ...enTrading,
  ...enWorkflows,
  copy: [enBrowser, enChat, enCore, enEvolution, enMisc, enOnboarding, enSettings, enSkills, enStrategies, enTrading, enWorkflows]
    .reduce<Record<string, MessageValue>>((copy, resource) => mergeMessages(copy, copyOf(resource)), {}),
} as const;

export const zh = {
  ...zhSdkAccounts,
  ...zhAccountConnection,
  ...zhChatHistory,
  ...zhComposer,
  ...zhResearchWorkspace,
  ...zhWorkflowExperience,
  ...zhDesktop,
  ...zhBrowser,
  ...zhChat,
  ...zhCore,
  ...zhEvolution,
  ...zhMisc,
  ...zhOnboarding,
  ...zhSettings,
  ...zhSkills,
  ...zhStrategies,
  ...zhTrading,
  ...zhWorkflows,
  copy: [zhBrowser, zhChat, zhCore, zhEvolution, zhMisc, zhOnboarding, zhSettings, zhSkills, zhStrategies, zhTrading, zhWorkflows]
    .reduce<Record<string, MessageValue>>((copy, resource) => mergeMessages(copy, copyOf(resource)), {}),
} as const;
