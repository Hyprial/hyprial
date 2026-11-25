import type {
  ConnectionSummary,
  ConversationMessage,
  GuiPage,
  SessionAgent,
  SessionTarget,
} from './gui-view-model';

/** Toast severity; also decides auto-dismiss vs persist-into-center. */
export type NoticeTone = 'info' | 'success' | 'warning' | 'error';

/** One queued toast. Error/warning toasts also land in the center history. */
export interface NoticeItem {
  readonly id: number;
  readonly tone: NoticeTone;
  readonly text: string;
  /**
   * Where the notice came from: an operation id (e.g.
   * connection.adapter.add) or a flow id (provider-login). Legacy set-notice
   * mirrors carry none and render as 来源未确认.
   */
  readonly source?: string;
  /** Overrides the tone default: true dismisses the toast after the usual delay, the center entry stays. */
  readonly autoDismiss?: boolean;
}
export type AgentFilter =
  | 'all'
  | 'available'
  | 'needs-connection'
  | 'disabled'
  | 'unknown';

export type FirstRunMode =
  | 'simulate'
  | 'unavailable'
  | 'live'
  | 'model-unavailable'
  | 'account-failure'
  | 'model-failure'
  | 'conversation-failure'
  | 'complete';
export type FirstRunStep = 'account' | 'model' | 'conversation';
export type FirstRunStatus =
  | 'idle'
  | 'pending'
  | 'failed'
  | 'complete'
  | 'unavailable';
export type ModelProvider = 'service-a' | 'service-b';
export type ModelConnectionMethod = 'account' | 'api-key';

export interface FirstRunState {
  readonly mode: FirstRunMode;
  readonly step: FirstRunStep;
  readonly status: FirstRunStatus;
  readonly provider: ModelProvider | null;
  readonly modelMethod: ModelConnectionMethod | null;
  readonly progress: number;
  readonly error: string | null;
}

/**
 * Where the UI should put focus when a chat view is restored or revisited.
 * This is presentation intent only; it names no protocol operation and no
 * canonical message by itself.
 */
export type ChatFocusIntent =
  | { readonly kind: 'composer' }
  | { readonly kind: 'message'; readonly messageId: string }
  | { readonly kind: 'info-toggle' };

/**
 * How a local send resolves. `unavailable` means no send interface exists in
 * this build, so nothing may be reported as delivered.
 */
export type ChatSendMode = 'reply' | 'fail' | 'unavailable';

/**
 * Optional chat data supplied by a composition entry (tests or the development
 * preview). The product entry passes nothing and keeps its formal state.
 */
export interface ChatSeed {
  /** Local fixture ChatPage requires at least one target; contract empty uses its controller. */
  readonly targets?: readonly [SessionAgent, ...SessionAgent[]];
  readonly activeTargetId?: string;
  readonly availableTargetId?: string;
  readonly threads?: Readonly<
    Record<string, readonly ConversationMessage[]>
  >;
  readonly sendMode?: ChatSendMode;
  readonly replyText?: string;
}

/** Which Agent-page panel is open. Presentation state only. */
export type AgentPanel =
  | { readonly kind: 'list' }
  | { readonly kind: 'create' }
  | { readonly kind: 'detail'; readonly agentId: string };

export interface TargetChatState {
  /** Authoritative message id, or null when nothing is selected. */
  readonly selectedMessageId: string | null;
  readonly focusIntent: ChatFocusIntent | null;
}

export interface AppState {
  readonly firstRun: FirstRunState;
  readonly page: GuiPage;
  readonly targets: readonly SessionAgent[];
  readonly connections: readonly ConnectionSummary[];
  readonly threads: Readonly<
    Record<string, readonly ConversationMessage[]>
  >;
  readonly activeTargetId: string;
  /** Per-target drafts. Switching target must not discard text. */
  readonly drafts: Readonly<Record<string, string>>;
  /** Per-target selection and focus restoration intent. */
  readonly targetChat: Readonly<Record<string, TargetChatState>>;
  readonly infoCollapsed: boolean;
  /** Delivery behaviour for local sends in this composition. */
  readonly chatSendMode: ChatSendMode;
  /** Text a local send shows as the Agent reply; empty when none is wired. */
  readonly chatReplyText: string;
  /** Monotonic counter for deterministic local message ids. */
  readonly messageSeq: number;
  /** Most recently opened conversation, or null before any is opened. */
  readonly lastChatTargetId: string | null;
  /** Agent detail "开始对话" intent; consumed by the connected chat page. */
  readonly chatIntentTargetId: string | null;
  readonly openedChatTargetIds?: readonly string[];
  /** Agent selected on the Agent page, or null when none is selected. */
  readonly selectedAgentId: string | null;
  /** Agent page panel: list, create form, or one Agent's detail. */
  readonly agentPanel: AgentPanel;
  readonly agentQuery: string;
  readonly agentFilter: AgentFilter;
  readonly accountMenuOpen: boolean;
  readonly logoutDialogOpen: boolean;
  readonly notice: string | null;
  /** Queued toasts; newest last. */
  readonly notices: readonly NoticeItem[];
  /** Persistent center history (warning/error toasts), newest first. */
  readonly centerNotices: readonly NoticeItem[];
  readonly notificationCenterOpen: boolean;
  readonly centerUnread: number;
  /** Identity the notices were raised under: null signed out, '' owner unknown, undefined before the first confirmation. */
  readonly sessionIdentity?: string | null;
}

/**
 * Presentation-state overlay injected by a composition entry (the development
 * preview's linked session, or a test). The product entry passes nothing; the
 * reducer stays the only in-app state authority.
 */
export interface StateSeed {
  readonly firstRun?: FirstRunState;
  readonly targets?: readonly SessionAgent[];
  readonly connections?: readonly ConnectionSummary[];
  readonly threads?: Readonly<
    Record<string, readonly ConversationMessage[]>
  >;
}

export type AppAction =
  | {
      readonly type: 'first-run-select-provider';
      readonly provider: ModelProvider;
    }
  | {
      readonly type: 'first-run-select-model-method';
      readonly method: ModelConnectionMethod;
    }
  | { readonly type: 'first-run-submit'; readonly step: FirstRunStep }
  | { readonly type: 'first-run-complete' }
  | { readonly type: 'first-run-succeed'; readonly step: FirstRunStep; readonly previewOnly?: boolean }
  | {
      readonly type: 'first-run-fail';
      readonly step: FirstRunStep;
      readonly message: string;
    }
  | { readonly type: 'first-run-retry' }
  | { readonly type: 'first-run-back' }
  | { readonly type: 'navigate'; readonly page: GuiPage }
  | { readonly type: 'select-target'; readonly targetId: string }
  | { readonly type: 'open-chat'; readonly targetId: string }
  | { readonly type: 'open-agent-chat'; readonly targetId: string }
  | { readonly type: 'agent-deleted'; readonly targetId: string }
  | { readonly type: 'clear-chat-intent' }
  | { readonly type: 'open-chat-draft'; readonly targetId: string; readonly draft: string }
  | { readonly type: 'open-agent'; readonly agentId: string }
  | { readonly type: 'open-agent-detail'; readonly agentId: string }
  | { readonly type: 'open-agent-create' }
  | { readonly type: 'close-agent-panel' }
  | {
      readonly type: 'set-draft';
      readonly value: string;
      readonly targetId?: string;
    }
  | {
      readonly type: 'chat-send';
      readonly targetId: string;
      readonly text: string;
    }
  | {
      readonly type: 'chat-resolve';
      readonly targetId: string;
      readonly messageId: string;
      readonly outcome: 'sent' | 'failed';
    }
  | {
      readonly type: 'chat-retry';
      readonly targetId: string;
      readonly messageId: string;
    }
  | {
      readonly type: 'select-message';
      readonly targetId: string;
      readonly messageId: string;
    }
  | {
      readonly type: 'set-focus-intent';
      readonly targetId: string;
      readonly intent: ChatFocusIntent;
    }
  | { readonly type: 'clear-focus-intent'; readonly targetId: string }
  | { readonly type: 'set-info-collapsed'; readonly collapsed: boolean }
  | { readonly type: 'set-agent-query'; readonly value: string }
  | { readonly type: 'set-agent-filter'; readonly filter: AgentFilter }
  | { readonly type: 'clear-agent-filters' }
  | { readonly type: 'toggle-account-menu'; readonly open: boolean }
  | { readonly type: 'request-logout' }
  | { readonly type: 'cancel-logout' }
  | { readonly type: 'confirm-logout' }
  | { readonly type: 'set-notice'; readonly text: string }
  | { readonly type: 'clear-notice' }
  | {
      readonly type: 'push-notice';
      readonly tone: NoticeTone;
      readonly text: string;
      /** Origin shown in the notification center; omit when unknown. */
      readonly source?: string;
      readonly autoDismiss?: boolean;
    }
  | { readonly type: 'dismiss-notice'; readonly id: number }
  | { readonly type: 'toggle-notification-center'; readonly open: boolean }
  | { readonly type: 'clear-center-notifications' }
  | { readonly type: 'session-identity'; readonly identity: string | null };

export function isChatTarget(target: SessionTarget): boolean {
  return target.kind === 'agent' && target.availability === 'available';
}
