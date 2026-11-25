/**
 * Decision record (GUI deep modules C2, 2026-11-08): fixture state construction
 * moved to `dev/fixtures/fixture-state.ts`; this module keeps only formal
 * state. `confirm-logout` resets to the formal empty state; development
 * compositions install the dev fixture reducer wrapper to keep the historical
 * fixture reset.
 * Decision record (WP1 product composition, 2026-11-09): `applyStateSeed`
 * overlays a composition-supplied presentation seed onto the reducer state;
 * only seed keys that are present replace reducer values, so the reducer keeps
 * authority over everything the composition did not inject.
 * Historical baselines supply their own chat lists; empty product/journey
 * states track explicitly opened targets separately from the Agent directory.
 * Decision record (artifact-neutral composition inputs, 2026-11-10): the
 * baseline type is named `StateBaseline`; the product composition interface
 * carries no fixture/mock vocabulary, so the release bundle cannot leak
 * mock-composition identifiers (production copy gate scans dist content).
 */
import type { ConversationMessage } from './gui-view-model';
import { upsertMessage } from './messages';
import type {
  AppAction,
  AppState,
  ChatSeed,
  FirstRunMode,
  FirstRunStep,
  StateSeed,
  TargetChatState,
} from './types';
import { isChatTarget } from './types';

export function applyStateSeed(state: AppState, seed: StateSeed | undefined): AppState {
  if (!seed) {
    return state;
  }
  return {
    ...state,
    ...(seed.firstRun ? { firstRun: seed.firstRun } : {}),
    ...(seed.targets ? { targets: seed.targets } : {}),
    ...(seed.connections ? { connections: seed.connections } : {}),
    ...(seed.threads ? { threads: seed.threads } : {}),
  };
}

let noticeSeq = 0;

function nextNoticeId(): number {
  noticeSeq += 1;
  return noticeSeq;
}
export const LOGOUT_NOTICE = '已清空当前会话。账号登出暂不可用。';

const FIRST_RUN_STEPS: readonly FirstRunStep[] = [
  'account',
  'model',
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

export function createEmptyState(
  firstRunMode: FirstRunMode = 'unavailable',
  notice: string | null = null,
): AppState {
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
    targets: [],
    connections: [],
    threads: {},
    activeTargetId: '',
    drafts: {},
    targetChat: {},
    openedChatTargetIds: [],
    infoCollapsed: false,
    chatSendMode: 'unavailable',
    chatReplyText: '',
    messageSeq: 1,
    lastChatTargetId: null,
    chatIntentTargetId: null,
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

export type StateBaseline = Pick<AppState, 'targets' | 'connections' | 'threads'>;

export function createStateFromBaseline(
  baseline: StateBaseline,
  chatSeed: ChatSeed = {},
  firstRunMode: FirstRunMode = 'complete',
): AppState {
  const seedThreads = chatSeed.threads ?? {};
  const agents = chatSeed.targets ?? baseline.targets;
  if (agents.length === 0) throw new RangeError('Chat seed targets must be nonempty.');
  const targetIds = new Set(agents.map((agent) => agent.id));
  const threads: Record<string, readonly ConversationMessage[]> = {};
  for (const targetId of targetIds) {
    const seeded = seedThreads[targetId];
    threads[targetId] = seeded
      ? seeded.map((message) => ({ ...message }))
      : (baseline.threads[targetId] ?? []).map((message) => ({ ...message }));
  }
  return {
    ...createEmptyState(firstRunMode),
    openedChatTargetIds: undefined,
    targets: agents.map((agent) => ({
      ...agent,
      availability: agent.id === chatSeed.availableTargetId ? 'available' : agent.availability,
      capabilities: [...agent.capabilities],
    })),
    connections: baseline.connections.map((connection) => ({ ...connection })),
    threads,
    activeTargetId: targetIds.has(chatSeed.activeTargetId ?? 'agent-squire')
      ? (chatSeed.activeTargetId ?? 'agent-squire')
      : agents[0].id,
    chatSendMode: chatSeed.sendMode ?? 'unavailable',
    chatReplyText: chatSeed.replyText ?? '',
  };
}

export function appReducer(state: AppState, action: AppAction): AppState {
  if (action.type.startsWith('first-run-') && action.type !== 'first-run-complete') {
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

    case 'first-run-complete':
      if (state.firstRun.status === 'complete' && state.page === 'start') return state;
      return {
        ...state,
        page: 'start',
        firstRun: {
          ...state.firstRun,
          status: 'complete',
          error: null,
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
        model: 'conversation',
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
        chatIntentTargetId: null,
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
        chatIntentTargetId: null,
        activeTargetId: target.id,
        lastChatTargetId: target.id,
        accountMenuOpen: false,
        notice: null,
      };
    }

    case 'open-agent-chat':
      return {
        ...state,
        page: 'chat',
        chatIntentTargetId: action.targetId,
        openedChatTargetIds: [...new Set([...(state.openedChatTargetIds ?? []), action.targetId])],
        activeTargetId: action.targetId,
        lastChatTargetId: action.targetId,
        accountMenuOpen: false,
        notice: null,
      };

    case 'agent-deleted':
      return {
        ...state,
        targets: state.targets.filter(target => target.id !== action.targetId),
        openedChatTargetIds: state.openedChatTargetIds?.filter(id => id !== action.targetId),
        activeTargetId: state.activeTargetId === action.targetId ? '' : state.activeTargetId,
        lastChatTargetId: state.lastChatTargetId === action.targetId ? null : state.lastChatTargetId,
        chatIntentTargetId: state.chatIntentTargetId === action.targetId ? null : state.chatIntentTargetId,
      };

    case 'clear-chat-intent':
      return state.chatIntentTargetId === null
        ? state
        : { ...state, chatIntentTargetId: null };

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
      const fresh = createEmptyState('complete', LOGOUT_NOTICE);
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
          { id, tone: action.tone, text: action.text, source: action.source, autoDismiss: action.autoDismiss },
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

    case 'session-identity': {
      const previous = state.sessionIdentity;
      const next = action.identity;
      if (previous === next) return state;
      const changed = previous !== undefined && previous !== null
        && (next === null || (next !== '' && previous !== '' && next !== previous));
      const sessionIdentity = next === '' && previous ? previous : next;
      return changed
        ? { ...state, sessionIdentity, notices: [], centerNotices: [], centerUnread: 0 }
        : { ...state, sessionIdentity };
    }

    case 'clear-notice':
      return { ...state, notice: null, notices: [] };
  }
}
