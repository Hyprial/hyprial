import type {
  ConversationMessage,
  MessageDeliveryState,
} from './gui-view-model';
import type { FixtureAgent } from '../fixtures/demo';
import { mergeMessagesById } from './messages';
import type { AgentFilter, AppState, ChatFocusIntent } from './types';
import { isChatTarget } from './types';

export const FILTER_LABELS: Readonly<Record<AgentFilter, string>> = {
  all: '全部',
  available: '可用',
  'needs-connection': '需要连接',
  disabled: '已停用',
};

export const AVAILABILITY_LABELS: Readonly<
  Record<FixtureAgent['availability'], string>
> = {
  available: '可用',
  'needs-connection': '需要连接',
  disabled: '已停用',
};

export const DELIVERY_LABELS: Readonly<
  Record<MessageDeliveryState, string>
> = {
  'not-sent': '未发出',
  pending: '处理中',
  sent: '已发送',
  failed: '发送失败',
  unknown: '结果未知 · 先核对原请求',
};

export function getActiveTarget(state: AppState): FixtureAgent {
  return (
    state.targets.find((target) => target.id === state.activeTargetId) ??
    state.targets[0]
  );
}

export function getActiveDraft(state: AppState): string {
  return state.drafts[state.activeTargetId] ?? '';
}

export function getSelectedMessage(
  state: AppState,
  targetId: string = state.activeTargetId,
): ConversationMessage | undefined {
  const selectedId = state.targetChat[targetId]?.selectedMessageId;
  if (!selectedId) {
    return undefined;
  }
  return mergeMessagesById(state.threads[targetId] ?? []).find(
    (message) => message.id === selectedId,
  );
}

export function getFocusIntent(
  state: AppState,
  targetId: string = state.activeTargetId,
): ChatFocusIntent | null {
  return state.targetChat[targetId]?.focusIntent ?? null;
}

export function getActiveThread(
  state: AppState,
): readonly ConversationMessage[] {
  return mergeMessagesById(state.threads[state.activeTargetId] ?? []);
}

export function getLastMessage(
  state: AppState,
  targetId: string,
): ConversationMessage | undefined {
  const thread = mergeMessagesById(state.threads[targetId] ?? []);
  return thread[thread.length - 1];
}

export function getLastMessageText(
  state: AppState,
  targetId: string,
): string {
  return getLastMessage(state, targetId)?.text ?? '还没有对话';
}

/**
 * The conversation the start page offers to resume.
 *
 * Returns null before any conversation was opened, so the start page never
 * shows an empty shell entry.
 */
export function getRecentChatTarget(state: AppState): FixtureAgent | null {
  if (!state.lastChatTargetId) {
    return null;
  }
  return (
    state.targets.find((target) => target.id === state.lastChatTargetId) ??
    null
  );
}

/** Pending outgoing message for one conversation, if any. */
export function getPendingMessage(
  state: AppState,
  targetId: string,
): ConversationMessage | undefined {
  return (state.threads[targetId] ?? []).find(
    (message) => message.delivery === 'pending',
  );
}

/** Last message that failed to send, which drives the retry affordance. */
export function getFailedMessage(
  state: AppState,
  targetId: string,
): ConversationMessage | undefined {
  const thread = mergeMessagesById(state.threads[targetId] ?? []);
  const last = thread[thread.length - 1];
  return last?.delivery === 'failed' ? last : undefined;
}

export function getVisibleAgents(state: AppState): readonly FixtureAgent[] {
  const query = state.agentQuery.trim().toLowerCase();
  return state.targets.filter((target) => {
    if (state.agentFilter !== 'all' && target.availability !== state.agentFilter) {
      return false;
    }
    if (!query) {
      return true;
    }
    const haystack = [
      target.displayName,
      target.role,
      target.modelLabel,
      ...target.capabilities,
    ]
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  });
}

export function getAvailableTargetCount(state: AppState): number {
  return state.targets.filter(isChatTarget).length;
}

export function getDefaultModel(state: AppState) {
  return state.connections.find(
    (connection) =>
      connection.kind === 'model' && connection.isDefault === true,
  );
}

export function canSendToTarget(target: FixtureAgent): boolean {
  return isChatTarget(target);
}
