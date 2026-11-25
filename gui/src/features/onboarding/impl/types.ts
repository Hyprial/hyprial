import type { MethodRefusal } from '../../../client';
import type { AccountCredentialValues } from '../../../components/forms';
import type { ConversationMessage } from '../../../state/gui-view-model';
import type { AuthCancelResult, AuthOpenResult } from '../../../platform/auth';
import type { AgentDraft } from '../../agents';
import type { ProviderLoginFile } from '../../agent-provider';
import type { SharedCredentialsApi } from '../../connections';
import type { ChatIntent } from '../../agent-chat';

export type FirstRunStepId =
  | 'account'
  | 'device'
  | 'model'
  | 'conversation';

/** `live` is the product path; `isolated` exists only for isolated UI previews. */
export type FirstRunScenario = 'live' | 'isolated';

export type AccountEntryMode = 'login' | 'register';

export type ChatStatus = 'unavailable' | 'ready' | 'completed';

export type StepViewState = 'active' | 'completed' | 'locked';

export type AttemptState<T> =
  | { readonly kind: 'idle' }
  | { readonly kind: 'running' }
  | { readonly kind: 'settled'; readonly result: T };

export type AccountActionResult =
  | { readonly status: 'unauthenticated'; readonly message?: string }
  | {
      readonly status: 'authenticated';
      readonly owner?: string;
      readonly username?: string;
      readonly message?: string;
    }
  | {
      readonly status: 'failed';
      readonly message: string;
      readonly retryable?: boolean;
    }
  | { readonly status: 'unknown'; readonly message: string }
  | { readonly status: 'unavailable'; readonly message?: string; readonly refusal?: MethodRefusal };

export type DeviceActionResult =
  | { readonly status: 'not-ready'; readonly message?: string }
  | {
      readonly status: 'ready';
      readonly message?: string;
    }
  | {
      readonly status: 'failed';
      readonly message: string;
      readonly retryable?: boolean;
    }
  | { readonly status: 'unknown'; readonly message: string }
  | { readonly status: 'unavailable'; readonly message?: string; readonly refusal?: MethodRefusal };

/**
 * UI callback contract only. It names no HTTP endpoint, daemon command or
 * storage key; integration supplies adapters and must report unavailable or
 * unknown instead of manufacturing success.
 */
export interface AccountProgress {
  readonly event:
    | 'device'
    | 'authenticated'
    | 'committed'
    | 'unknown';
  readonly verificationUri?: string;
  readonly userCode?: string;
  readonly expiresIn?: number;
  readonly interval?: number;
  readonly restarted?: boolean;
  readonly code?: string;
  readonly owner?: string;
  readonly openAvailable?: boolean;
  readonly openOpened?: boolean;
  readonly openError?: string;
  readonly openRetryable?: boolean;
}

export type FirstRunReceiptState =
  | 'none'
  | 'recorded'
  | 'corrupt'
  | 'unsupported-version'
  | 'unavailable';

export interface FirstRunReceiptView {
  readonly state: FirstRunReceiptState;
  readonly revision: number;
  readonly confirmed: readonly ('configuration' | 'reply')[];
  readonly updatedAt: string | null;
  readonly skipped?: boolean;
}

export interface FirstRunSkipResult {
  readonly status: 'skipped' | 'failed' | 'unavailable' | 'unknown';
  readonly firstRun?: FirstRunReceiptView;
  readonly message?: string;
  readonly refusal?: MethodRefusal;
}

export type FirstRunAttemptState =
  | 'pending'
  | 'bound'
  | 'confirmed'
  | 'cancelled'
  | 'superseded';

/** The server's non-secret P3 attempt projection; it carries no message body. */
export interface FirstRunAttemptView {
  readonly attemptId: string;
  readonly revision: number;
  readonly state: FirstRunAttemptState;
  readonly operationKey: string;
  readonly sendKey: string;
  readonly receiptRevision: number;
  readonly loginEpoch: number;
  readonly agentUri: string;
  readonly agentCreatedAtMs: number;
  readonly provider: string | null;
  readonly model: string | null;
  readonly harness: string;
  readonly configRevision: string | null;
  readonly credentialKind: 'key' | 'native';
  readonly requestMessageId: string | null;
  readonly conversationId: string | null;
  readonly replyMessageId: string | null;
}

export interface ReplyAttemptResult {
  readonly status: 'begun' | 'bound' | 'confirmed' | 'cancelled' | 'failed' | 'unavailable' | 'unknown';
  readonly attempt?: FirstRunAttemptView;
  readonly firstRun?: FirstRunReceiptView;
  readonly message?: string;
  readonly refusal?: MethodRefusal;
}

/** One dispatch ticket per attempt; every later call is lookup-only. */
export interface FirstRunSendOptions {
  readonly dispatch?: boolean;
}

/** One exact message from the attempt's own conversation history. */
export interface ReplyMessageView {
  readonly id: string;
  readonly author: 'user' | 'agent' | 'system';
  readonly text: string;
}

/** The P3 closed loop; every write is explicit and never auto-retried. */
export interface FirstRunReplyActions {
  readonly begin: (agentUri: string) => Promise<ReplyAttemptResult>;
  /** Send the confirmation message with the attempt's own send key, then bind. */
  readonly send: (
    attempt: FirstRunAttemptView,
    text: string,
    options?: FirstRunSendOptions,
  ) => Promise<ReplyAttemptResult>;
  /** Read the bound conversation's durable messages; never throws or writes. */
  readonly history: (attempt: FirstRunAttemptView) => Promise<readonly ReplyMessageView[]>;
  /** Complete only after the user says a real reply arrived. */
  readonly confirm: (attempt: FirstRunAttemptView, receiptRevision: number) => Promise<ReplyAttemptResult>;
  readonly cancel: (attempt: FirstRunAttemptView) => Promise<ReplyAttemptResult>;
}

export interface AccountSnapshot {
  readonly account: AccountActionResult;
  readonly device: DeviceActionResult;
  readonly firstRun?: FirstRunReceiptView;
  readonly firstRunAttempt?: FirstRunAttemptView;
  readonly pendingLogin?: { readonly operationId: string };
}

export interface ModelKeySaveResult {
  readonly status: 'saved' | 'failed' | 'unavailable' | 'unknown';
  readonly message?: string;
  readonly refusal?: MethodRefusal;
}
export type ModelKeyStatusCheck =
  | {
      readonly status: 'checked';
      readonly keySaved: boolean | null;
      readonly message: string;
    }
  | { readonly status: 'unavailable'; readonly message: string };

export interface SetupSnapshot {
  readonly keySaved: boolean | null;
  readonly pendingAction: 'save' | null;
  /** The selected first-run Agent; non-secret and read from the Agent directory. */
  readonly agentUri?: string;
}

export type RuntimeConfigurationStage = 'credential-key' | 'credential-auth';

export interface RuntimeConfigurationResult {
  readonly status: 'configured' | 'failed' | 'unavailable' | 'unknown';
  readonly message?: string;
  readonly refusal?: MethodRefusal;
  readonly actorUri?: string;
  readonly created?: boolean;
  /** Non-secret stage already reached; kept for reconciliation, never retried automatically. */
  readonly stage?: RuntimeConfigurationStage;
}

export interface AccountLogoutResult {
  readonly status: 'logged-out' | 'failed' | 'unknown';
  readonly message?: string;
  readonly refusal?: MethodRefusal;
  readonly snapshot?: AccountSnapshot;
  /** The server's typed failure code, kept verbatim (never remapped locally). */
  readonly code?: string;
  /** Login-owned artifacts the server really removed, when it reported them. */
  readonly removed?: readonly string[];
  readonly nextAction?: 'inspect' | 'login' | 'poll';
}

export interface FirstRunActions {
  readonly logoutAccount?: () => Promise<AccountLogoutResult>;
  readonly getSetupState?: () => Promise<SetupSnapshot>;
  readonly saveModelKey?: (key: string) => Promise<ModelKeySaveResult>;
  /** Connected product path: create the first-run Agent through the shared Agent API. */
  readonly configureAgent?: (draft: AgentDraft) => Promise<RuntimeConfigurationResult>;
  readonly runtimeCredentials?: SharedCredentialsApi;
  readonly pickRuntimeLoginFiles?: (provider: string) => Promise<readonly ProviderLoginFile[]>;

  /** Public state only; never returns access/refresh tokens. */
  readonly getAccountState?: () => Promise<AccountSnapshot>;
  readonly readLoginOperation?: (operationId: string) => Promise<AccountActionResult>;
  readonly subscribeAccountProgress?: (listener: (progress: AccountProgress) => void) => () => void;
  readonly cancelAccount?: () => Promise<AuthCancelResult>;
  /** Synchronous: the refusal a login or cancellation would meet, known before any side effect starts. */
  readonly loginRefusal?: () => MethodRefusal | null;
  readonly cancelRefusal?: () => MethodRefusal | null;
  /** Present only when the native host owns a controlled system-browser opener. */
  readonly openAuthorization?: () => Promise<AuthOpenResult>;

  readonly authenticateAccount?: (
    values: AccountCredentialValues,
    mode: AccountEntryMode,
  ) => Promise<AccountActionResult>;
  readonly initializeDevice?: () => Promise<DeviceActionResult>;
  /** P3: server-backed reply confirmation; absent means the step is unavailable. */
  readonly replyConfirmation?: FirstRunReplyActions;
  /** Persist the user's explicit shortcut past the four-step flow. */
  readonly skipFirstRun?: (expectedRevision: number) => Promise<FirstRunSkipResult>;
}

/**
 * View projection of the shared chat contract runtime for the first message.
 * The host must inject the same controller the chat feature uses; `sendAccepted`
 * is true only after a send armed by this flow instance was accepted and
 * confirmed by the daemon contract. A success from any other send (ordinary
 * chat, an earlier session, another tab or a restored pending request) keeps
 * this false and never completes first-run.
 */
export interface FirstRunConversationRuntime {
  readonly status: 'loading' | 'ready' | 'error';
  readonly error?: string;
  /** Present only when the runtime labels an isolated contract fixture. */
  readonly sourceLabel?: string;
  readonly targetId: string | null;
  readonly targetName: string;
  readonly draft: string;
  readonly messages: readonly ConversationMessage[];
  readonly history: { readonly status: 'loading' | 'ready' | 'error'; readonly error?: string };
  readonly operation?: ChatIntent;
  readonly sendAccepted: boolean;
  readonly capability: 'ready' | 'unavailable' | 'unconfirmed';
  readonly onDraftChange: (value: string) => void;
  readonly onSend: (text: string) => void;
  readonly onRetry: () => void;
  readonly onCheckResult: () => void;
}

/**
 * Isolation metadata supplied by the composition that mounts an isolated
 * preview. Product compositions omit it; the copy never lives in product code
 * so the production bundle stays free of fixture markers while dev and
 * production render identical DOM for identical inputs.
 */
export interface FirstRunSourceNotice {
  readonly title: string;
  readonly body: string;
}

export interface FirstRunSourceMetadata {
  /** Rendered as an isolation Notice when the composition supplies one. */
  readonly notice?: FirstRunSourceNotice;
  /** Diagnostic aside label; falls back to the formal-entry text. */
  readonly label?: string;
  /** Note appended to the saved DeepSeek key status card value. */
  readonly savedKeyNote?: string;
}

export interface FirstRunOnboardingProps {
  readonly resumeFromStatus?: boolean;
  /**
   * Inert labeling only (rendered as `data-scenario`); it no longer selects
   * copy or actions. Isolated previews inject `source` metadata and explicit
   * demo actions instead; unspecified actions always resolve unavailable.
   */
  readonly scenario?: FirstRunScenario;
  /** Required for a real path; omitted means local unavailable/unknown results. */
  readonly actions?: FirstRunActions;
  /** Supplied by the shared chat integration after a real reply is verified. */
  readonly chatStatus?: ChatStatus;
  /** Shared chat contract runtime for the real first message; omitted means unavailable. */
  readonly conversation?: FirstRunConversationRuntime;
  /** Isolation metadata for explicit preview compositions. */
  readonly source?: FirstRunSourceMetadata;
  readonly onOpenChat?: () => void;
  /** Opens the chat on the P3 attempt's own Agent instead of a page-wide target. */
  readonly onOpenReplyChat?: (targetId: string) => void;
  readonly onComplete?: () => void;
  readonly onExit?: () => void;
  readonly className?: string;
}
