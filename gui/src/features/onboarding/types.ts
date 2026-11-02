import type { AccountCredentialValues } from '../../components/forms';
import type { AuthCancelResult } from '../../platform/auth';
import type {
  ModelValidationRequest,
  ModelValidationResult,
} from '../model';

export type FirstRunStepId =
  | 'account'
  | 'model'
  | 'squire'
  | 'conversation';

/** `live` is the product path; `demo` exists only for isolated UI previews. */
export type FirstRunScenario = 'live' | 'demo';

export type AccountEntryMode = 'login' | 'register';

export type ChatStatus = 'unavailable' | 'ready' | 'completed';

export type StepViewState = 'active' | 'completed' | 'locked';

export type AttemptState<T> =
  | { readonly kind: 'idle' }
  | { readonly kind: 'running' }
  | { readonly kind: 'settled'; readonly result: T };

export type AccountActionResult =
  | { readonly status: 'unauthenticated'; readonly message?: string }
  | { readonly status: 'authenticated'; readonly owner?: string; readonly message?: string }
  | {
      readonly status: 'failed';
      readonly message: string;
      readonly retryable?: boolean;
    }
  | { readonly status: 'unknown'; readonly message: string }
  | { readonly status: 'unavailable'; readonly message: string };

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
  | { readonly status: 'unavailable'; readonly message: string };

export type SquireActionResult =
  | { readonly status: 'created'; readonly message?: string }
  | { readonly status: 'ready'; readonly message?: string }
  | {
      readonly status: 'failed';
      readonly message: string;
      readonly retryable?: boolean;
    }
  | { readonly status: 'unknown'; readonly message: string }
  | { readonly status: 'unavailable'; readonly message: string };

/**
 * UI callback contract only. It names no HTTP endpoint, daemon command or
 * storage key; integration supplies adapters and must report unavailable or
 * unknown instead of manufacturing success.
 */
export interface AccountProgress {
  readonly event:
    | 'device'
    | 'binding-device'
    | 'authenticated'
    | 'binding-proof-failed'
    | 'committed'
    | 'unknown';
  readonly verificationUri?: string;
  readonly userCode?: string;
  readonly expiresIn?: number;
  readonly interval?: number;
  readonly restarted?: boolean;
  readonly code?: string;
  readonly owner?: string;
}

export interface AccountSnapshot {
  readonly account: AccountActionResult;
  readonly device: DeviceActionResult;
}

export interface ModelKeySaveResult {
  readonly status: 'saved' | 'failed' | 'unavailable';
  readonly message?: string;
}
export interface SquirePreparationState {
  readonly grantRecorded?: boolean;
  readonly created: boolean;
  readonly granted: boolean;
  readonly started: boolean;
}
export interface SquirePreparationProgress { readonly stage: 'created' | 'granted' | 'started'; }
export interface SquirePreparationResult extends SquirePreparationState {
  readonly status: 'started' | 'failed' | 'unavailable' | 'unknown';
  readonly message?: string;
}

export interface SetupSnapshot extends SquirePreparationState {
  readonly grantRecorded: boolean;
  readonly keySaved: boolean | null;
  readonly granted: boolean;
  readonly compatible: boolean;
  readonly resumeAvailable: boolean;
  readonly pendingAction: 'save' | 'create' | 'configure' | 'grant' | 'start' | null;
}

export interface FirstRunActions {
  readonly getSetupState?: () => Promise<SetupSnapshot>;
  readonly saveModelKey?: (key: string) => Promise<ModelKeySaveResult>;
  readonly prepareSquire?: (onProgress?: (event: SquirePreparationProgress) => void) => Promise<SquirePreparationResult>;
  readonly cancelPreparation?: () => Promise<void>;

  /** Public state only; never returns access/refresh tokens. */
  readonly getAccountState?: () => Promise<AccountSnapshot>;
  readonly subscribeAccountProgress?: (listener: (progress: AccountProgress) => void) => () => void;
  readonly cancelAccount?: () => Promise<AuthCancelResult>;

  readonly authenticateAccount: (
    values: AccountCredentialValues,
    mode: AccountEntryMode,
  ) => Promise<AccountActionResult>;
  readonly initializeDevice: () => Promise<DeviceActionResult>;
  readonly validateModel: (
    request: ModelValidationRequest,
  ) => Promise<ModelValidationResult>;
  readonly createSquire: () => Promise<SquireActionResult>;
  readonly startSquire: () => Promise<SquireActionResult>;
}

export interface FirstRunOnboardingProps {
  /** Defaults to the no-backend `live` path. `demo` is visibly isolated. */
  readonly scenario?: FirstRunScenario;
  /** Required for a real path; omitted means local unavailable/unknown results. */
  readonly actions?: FirstRunActions;
  /** Supplied by the shared chat integration after a real reply is verified. */
  readonly chatStatus?: ChatStatus;
  /** Explicit UI simulation only; never sends or completes first-run. */
  readonly conversationPreview?: { readonly onConfirm: (text: string) => void };
  readonly onOpenChat?: () => void;
  readonly onComplete?: () => void;
  readonly onExit?: () => void;
  readonly className?: string;
}
