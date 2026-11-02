import type { ConversationMessage } from './gui-view-model';
import {
  FIXTURE_AGENTS,
  FIXTURE_CONNECTIONS,
  FIXTURE_THREADS,
} from '../fixtures/demo';
import { upsertMessage } from './messages';
import type {
  AppAction,
  AppState,
  ChatSeed,
  FirstRunMode,
  FirstRunStep,
  TargetChatState,
} from './types';
import { isChatTarget } from './types';

export const DEFAULT_TARGET_ID = 'agent-squire';

let noticeSeq = 0;

function nextNoticeId(): number {
  noticeSeq += 1;
  return noticeSeq;
}
export const LOGOUT_NOTICE = '已清空当前会话。账号登出暂不可用。';

const FIRST_RUN_STEPS: readonly FirstRunStep[] = [
  'account',
  'model',
  'squire',
  'conversation',
];

function initialFirstRunStatus(mode: FirstRunMode) {
  if (mode === 'complete') return 'complete' as const;
  if (mode === 'unavailable') return 'unavailable' as const;
  return 'idle' as const;
}

function previousFirstRunStep(step: FirstRunStep): FirstRunStep {
  const index = FIRST_RUN_STEPS.indexOf(step);
  return FIRST_RUN_STEPS[Math.max(0, index - 1)];
}

function getTargetChat(
  state: AppState,
  targetId: string,
): TargetChatState {
  return (
    state.targetChat[targetId] ?? {
      selectedMessageId: null,
      focusIntent: null,
    }
  );
}

function isSelectableMessage(
  state: AppState,
  targetId: string,
  messageId: string,
): boolean {
  if (messageId.startsWith('local-')) {
    return false;
  }
  if (!state.targets.some((target) => target.id === targetId)) {
    return false;
  }
  return (state.threads[targetId] ?? []).some(
    (message) => message.id === messageId,
  );
}

export function createFixtureState(
  firstRunMode: FirstRunMode = 'complete',
  notice: string | null = null,
  chatSeed: ChatSeed = {},
): AppState {
  const seedThreads = chatSeed.threads ?? {};
  const agents = chatSeed.targets ?? FIXTURE_AGENTS;
  if (agents.length === 0) throw new RangeError('Chat seed targets must be nonempty.');
  const targetIds = new Set(agents.map((agent) => agent.id));
  const threads: Record<string, readonly ConversationMessage[]> = {};
  for (const targetId of targetIds) {
    const seeded = seedThreads[targetId];
    threads[targetId] = seeded
      ? seeded.map((message) => ({ ...message }))
      : (FIXTURE_THREADS[targetId] ?? []).map((message) => ({ ...message }));
  }

  return {
    firstRun: {
      mode: firstRunMode,
      step: 'account',
      status: initialFirstRunStatus(firstRunMode),
      provider: null,
      modelMethod: null,
      progress: 0,
      error: null,
    },
    page: 'start',
    targets: agents.map((agent) => ({
      ...agent,
      availability:
        agent.id === chatSeed.availableTargetId
          ? ('available' as const)
          : agent.availability,
      capabilities: [...agent.capabilities],
    })),
    connections: FIXTURE_CONNECTIONS.map((connection) => ({
      ...connection,
    })),
    threads,
    activeTargetId: targetIds.has(chatSeed.activeTargetId ?? DEFAULT_TARGET_ID)
      ? (chatSeed.activeTargetId ?? DEFAULT_TARGET_ID)
      : agents[0].id,
    drafts: {},
    targetChat: {},
    infoCollapsed: false,
    chatSendMode: chatSeed.sendMode ?? 'unavailable',
    chatReplyText: chatSeed.replyText ?? '',
    messageSeq: 1,
    quickChatOpen: false,
    lastChatTargetId: null,
    selectedAgentId: null,
    agentPanel: { kind: 'list' },
    agentQuery: '',
    agentFilter: 'all',
    accountMenuOpen: false,
    logoutDialogOpen: false,
    notice,
    notices: [],
    centerNotices: [],
    notificationCenterOpen: false,
    centerUnread: 0,
  };
}

export function appReducer(state: AppState, action: AppAction): AppState {
  if (action.type.startsWith('first-run-')) {
    if (
      state.firstRun.mode === 'complete' ||
      state.firstRun.mode === 'unavailable' ||
      state.firstRun.status === 'complete'
    ) {
      return state;
    }
  }

  switch (action.type) {
    case 'first-run-select-provider':
      if (
        state.firstRun.step !== 'model' ||
        state.firstRun.status === 'pending' ||
        state.firstRun.status === 'unavailable'
      ) {
        return state;
      }
      return {
        ...state,
        firstRun: { ...state.firstRun, provider: action.provider },
      };

    case 'first-run-select-model-method':
      if (
        state.firstRun.step !== 'model' ||
        state.firstRun.status === 'pending' ||
        state.firstRun.status === 'unavailable'
      ) {
        return state;
      }
      return {
        ...state,
        firstRun: { ...state.firstRun, modelMethod: action.method },
      };

    case 'first-run-submit':
      if (
        action.step !== state.firstRun.step ||
        state.firstRun.status !== 'idle'
      ) {
        return state;
      }
      return {
        ...state,
        firstRun: {
          ...state.firstRun,
          status: 'pending',
          error: null,
          progress: 0,
        },
      };

    case 'first-run-succeed': {
      if (
        action.step !== state.firstRun.step ||
        state.firstRun.status !== 'pending'
      ) {
        return state;
      }
      if (action.step === 'conversation') {
        return {
          ...state,
          page: 'start',
          firstRun: {
            ...state.firstRun,
            status: 'complete',
            error: null,
          },
        };
      }
      const next: Record<Exclude<FirstRunStep, 'conversation'>, FirstRunStep> = {
        account: 'model',
        model: 'squire',
        squire: 'conversation',
      };
      const step = next[action.step];
      return {
        ...state,
        firstRun: {
          ...state.firstRun,
          step,
          status:
            step === 'model' && state.firstRun.mode === 'model-unavailable'
              ? 'unavailable'
              : 'idle',
          error: null,
          progress: 0,
        },
      };
    }

    case 'first-run-fail':
      if (
        action.step !== state.firstRun.step ||
        state.firstRun.status !== 'pending'
      ) {
        return state;
      }
      return {
        ...state,
        firstRun: {
          ...state.firstRun,
          status: 'failed',
          error: action.message,
        },
      };

    case 'first-run-progress':
      if (
        state.firstRun.step !== 'squire' ||
        state.firstRun.status !== 'pending'
      ) {
        return state;
      }
      return {
        ...state,
        firstRun: {
          ...state.firstRun,
          progress: Math.min(100, Math.max(0, action.value)),
        },
      };

    case 'first-run-retry':
      if (state.firstRun.status !== 'failed') {
        return state;
      }
      return {
        ...state,
        firstRun: {
          ...state.firstRun,
          status: 'pending',
          error: null,
          progress: 0,
        },
      };

    case 'first-run-back': {
      if (
        state.firstRun.step === 'account' ||
        state.firstRun.status === 'pending'
      ) {
        return state;
      }
      const step = previousFirstRunStep(state.firstRun.step);
      return {
        ...state,
        firstRun: {
          ...state.firstRun,
          step,
          status:
            step === 'model' && state.firstRun.mode === 'model-unavailable'
              ? 'unavailable'
              : 'idle',
          error: null,
          progress: 0,
        },
      };
    }

    case 'navigate':
      return {
        ...state,
        page: action.page,
        accountMenuOpen: false,
        notice: null,
      };

    case 'select-target': {
      const target = state.targets.find(
        (candidate) => candidate.id === action.targetId,
      );
      if (!target) {
        return state;
      }
      return {
        ...state,
        page: 'chat',
        activeTargetId: target.id,
        lastChatTargetId: target.id,
        accountMenuOpen: false,
        notice: null,
      };
    }

    case 'open-chat': {
      const target = state.targets.find(
        (candidate) => candidate.id === action.targetId,
      );
      if (!target) {
        return state;
      }
      return {
        ...state,
        page: 'chat',
        activeTargetId: target.id,
        lastChatTargetId: target.id,
        accountMenuOpen: false,
        notice: null,
      };
    }

    case 'open-agent': {
      const target = state.targets.find(
        (candidate) => candidate.id === action.agentId,
      );
      if (!target) {
        return state;
      }
      return {
        ...state,
        page: 'contacts',
        selectedAgentId: target.id,
        agentPanel: { kind: 'detail', agentId: target.id },
        agentQuery: '',
        agentFilter: 'all',
        quickChatOpen: false,
        accountMenuOpen: false,
        notice: null,
      };
    }

    case 'open-agent-detail':
      return {
        ...state,
        page: 'contacts',
        selectedAgentId: action.agentId,
        agentPanel: { kind: 'detail', agentId: action.agentId },
        accountMenuOpen: false,
        notice: null,
      };

    case 'open-agent-create':
      return {
        ...state,
        page: 'contacts',
        selectedAgentId: null,
        agentPanel: { kind: 'create' },
        agentQuery: '',
        agentFilter: 'all',
        accountMenuOpen: false,
        notice: null,
      };

    case 'close-agent-panel':
      return {
        ...state,
        agentPanel: { kind: 'list' },
        selectedAgentId: null,
      };

    case 'open-chat-draft': {
      const target = state.targets.find((candidate) => candidate.id === action.targetId);
      const existing = state.drafts[action.targetId];
      // No inferred Squire ID, auto-send, or silent replacement of another draft.
      if (!target || !action.draft.trim() || (existing?.trim() && existing !== action.draft)) return state;
      return {
        ...state, page: 'chat', activeTargetId: target.id, accountMenuOpen: false, notice: null,
        drafts: { ...state.drafts, [target.id]: action.draft },
        targetChat: { ...state.targetChat, [target.id]: { ...getTargetChat(state, target.id), focusIntent: { kind: 'composer' } } },
      };
    }

    case 'set-draft': {
      const targetId = action.targetId ?? state.activeTargetId;
      if (!state.targets.some((target) => target.id === targetId)) {
        return state;
      }
      return {
        ...state,
        drafts: {
          ...state.drafts,
          [targetId]: action.value,
        },
      };
    }

    case 'chat-send': {
      const target = state.targets.find(
        (candidate) => candidate.id === action.targetId,
      );
      const text = action.text.trim();
      if (!target || !text || !isChatTarget(target)) {
        return state;
      }
      const existing = state.threads[target.id] ?? [];
      if (existing.some((message) => message.delivery === 'pending')) {
        return state;
      }

      const message: ConversationMessage = {
        id: `local-${target.id}-${state.messageSeq}`,
        targetId: target.id,
        author: 'user',
        text,
        delivery: 'pending',
      };

      return {
        ...state,
        messageSeq: state.messageSeq + 1,
        drafts: {
          ...state.drafts,
          [target.id]: '',
        },
        threads: {
          ...state.threads,
          [target.id]: upsertMessage(existing, message),
        },
        lastChatTargetId: target.id,
        notice: null,
      };
    }

    case 'chat-resolve': {
      const thread = state.threads[action.targetId] ?? [];
      const pending = thread.find(
        (message) =>
          message.id === action.messageId && message.delivery === 'pending',
      );
      if (!pending) {
        return state;
      }

      if (action.outcome === 'failed') {
        const currentDraft = state.drafts[action.targetId] ?? '';
        return {
          ...state,
          drafts: {
            ...state.drafts,
            [action.targetId]: currentDraft || pending.text,
          },
          threads: {
            ...state.threads,
            [action.targetId]: upsertMessage(thread, {
              ...pending,
              delivery: 'failed',
            }),
          },
        };
      }

      if (!state.chatReplyText) {
        return {
          ...state,
          threads: {
            ...state.threads,
            [action.targetId]: upsertMessage(thread, {
              ...pending,
              delivery: 'failed',
            }),
          },
        };
      }

      const sent = upsertMessage(thread, { ...pending, delivery: 'sent' });
      const reply: ConversationMessage = {
        id: `local-${action.targetId}-reply-${state.messageSeq}`,
        targetId: action.targetId,
        author: 'assistant',
        text: state.chatReplyText,
        delivery: 'sent',
      };

      return {
        ...state,
        messageSeq: state.messageSeq + 1,
        threads: {
          ...state.threads,
          [action.targetId]: upsertMessage(sent, reply),
        },
      };
    }

    case 'chat-retry': {
      const thread = state.threads[action.targetId] ?? [];
      const failed = thread.find(
        (message) =>
          message.id === action.messageId && message.delivery === 'failed',
      );
      if (!failed) {
        return state;
      }
      const draft = (state.drafts[action.targetId] ?? '').trim();
      return {
        ...state,
        drafts: {
          ...state.drafts,
          [action.targetId]: '',
        },
        threads: {
          ...state.threads,
          [action.targetId]: upsertMessage(thread, {
            ...failed,
            text: draft || failed.text,
            delivery: 'pending',
          }),
        },
      };
    }

    case 'set-quick-chat':
      return { ...state, quickChatOpen: action.open };

    case 'select-message': {
      if (
        !isSelectableMessage(state, action.targetId, action.messageId)
      ) {
        return state;
      }
      const current = getTargetChat(state, action.targetId);
      return {
        ...state,
        targetChat: {
          ...state.targetChat,
          [action.targetId]: {
            ...current,
            selectedMessageId: action.messageId,
          },
        },
      };
    }

    case 'set-focus-intent': {
      if (!state.targets.some((target) => target.id === action.targetId)) {
        return state;
      }
      if (
        action.intent.kind === 'message' &&
        !isSelectableMessage(
          state,
          action.targetId,
          action.intent.messageId,
        )
      ) {
        return state;
      }
      const current = getTargetChat(state, action.targetId);
      return {
        ...state,
        targetChat: {
          ...state.targetChat,
          [action.targetId]: {
            ...current,
            focusIntent: action.intent,
          },
        },
      };
    }

    case 'clear-focus-intent': {
      if (!state.targets.some((target) => target.id === action.targetId)) {
        return state;
      }
      const current = getTargetChat(state, action.targetId);
      return {
        ...state,
        targetChat: {
          ...state.targetChat,
          [action.targetId]: {
            ...current,
            focusIntent: null,
          },
        },
      };
    }

    case 'set-info-collapsed':
      return { ...state, infoCollapsed: action.collapsed };

    case 'set-agent-query':
      return { ...state, agentQuery: action.value };

    case 'set-agent-filter':
      return { ...state, agentFilter: action.filter };

    case 'clear-agent-filters':
      return { ...state, agentQuery: '', agentFilter: 'all' };

    case 'toggle-account-menu':
      return { ...state, accountMenuOpen: action.open };

    case 'request-logout':
      return {
        ...state,
        accountMenuOpen: false,
        logoutDialogOpen: true,
      };

    case 'cancel-logout':
      return { ...state, logoutDialogOpen: false };

    case 'confirm-logout': {
      const fresh = createFixtureState('complete', LOGOUT_NOTICE);
      return {
        ...fresh,
        notices: [
          { id: nextNoticeId(), tone: 'info' as const, text: LOGOUT_NOTICE },
        ],
      };
    }

    case 'set-notice': {
      // Legacy single-slot entry: mirrors into the toast queue so every
      // existing caller flows through the unified notification surface.
      // The mirror carries no source (callers do not provide one); it only
      // replaces other source-less mirrors, never a sourced event.
      const id = nextNoticeId();
      const persistent =
        action.text.includes('暂不可用') || action.text.includes('操作失败');
      const deduped = state.notices.filter(
        (item) => !(item.text === action.text && item.source === undefined),
      );
      return {
        ...state,
        notice: action.text,
        notices: [...deduped, { id, tone: 'info' as const, text: action.text }].slice(-4),
        centerNotices: persistent
          ? [{ id, tone: 'warning' as const, text: action.text }, ...state.centerNotices].slice(0, 20)
          : state.centerNotices,
        centerUnread: persistent ? state.centerUnread + 1 : state.centerUnread,
      };
    }

    case 'push-notice': {
      const id = nextNoticeId();
      const persistent =
        action.tone === 'warning' || action.tone === 'error';
      // Dedupe key is text + source: two operations may share the same copy
      // (e.g. 功能暂不可用。) and are distinct events, both in the toast
      // queue and in the center history.
      const sameNotice = (item: { readonly text: string; readonly source?: string }) =>
        item.text === action.text && item.source === action.source;
      const deduped = state.notices.filter((item) => !sameNotice(item));
      const alreadyInCenter = state.centerNotices.some(sameNotice);
      return {
        ...state,
        notices: [
          ...deduped,
          { id, tone: action.tone, text: action.text, source: action.source },
        ].slice(-4),
        centerNotices:
          persistent && !alreadyInCenter
            ? [
                { id, tone: action.tone, text: action.text, source: action.source },
                ...state.centerNotices,
              ].slice(0, 20)
            : state.centerNotices,
        centerUnread:
          persistent && !alreadyInCenter ? state.centerUnread + 1 : state.centerUnread,
      };
    }

    case 'dismiss-notice':
      return {
        ...state,
        notices: state.notices.filter((item) => item.id !== action.id),
      };

    case 'toggle-notification-center':
      return {
        ...state,
        notificationCenterOpen: action.open,
        centerUnread: action.open ? 0 : state.centerUnread,
      };

    case 'clear-center-notifications':
      return {
        ...state,
        centerNotices: [],
        centerUnread: 0,
      };

    case 'clear-notice':
      return { ...state, notice: null, notices: [] };
  }
}
