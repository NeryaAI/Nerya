import type { ChatThread, ChatRunSettings } from './chat';
import type { AssistantProfile } from './assistants';
import type { ConversationCommand } from './conversationCommands';

export function composerSettings(defaults: ChatRunSettings, assistant: AssistantProfile | null, draft?: ChatRunSettings): ChatRunSettings {
  if (draft) return draft;
  if (!assistant || !(assistant.model_provider || assistant.model_id || assistant.model_tier)) return defaults;
  return { ...defaults, model_provider: assistant.model_provider, model_id: assistant.model_id, model_tier: assistant.model_tier };
}

function accepted(context: Record<string, unknown> | undefined): string | null {
  const value = context?.accepted_model;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const { provider, model } = value as Record<string, unknown>;
  if (typeof model !== 'string' || !model.trim()) return null;
  return [typeof provider === 'string' ? provider.trim() : '', model.trim()].filter(Boolean).join(' · ');
}

/** Durable admission only; composer preferences describe the next send. */
export function admittedModel(thread: ChatThread | null, commands: ConversationCommand[]): string | null {
  const message = thread?.messages.findLast(item => item.role === 'assistant');
  const command = commands.filter(item => item.session_id === thread?.id && item.kind !== 'guide'
    && !['queued', 'removed', 'not_consumed'].includes(item.state))
    .sort((a, b) => b.created_at - a.created_at || b.position - a.position)[0];
  if (!command) return message?.role === 'assistant' ? accepted(message.turn?.context_snapshot) : null;
  if (message?.role === 'assistant') {
    const matching = message.command_id === command.command_id || message.turn?.command_id === command.command_id;
    if (matching) return accepted(command.context) || accepted(message.turn?.context_snapshot);
    if (message.ts > command.created_at * 1000) return accepted(message.turn?.context_snapshot);
  }
  return accepted(command.context);
}
