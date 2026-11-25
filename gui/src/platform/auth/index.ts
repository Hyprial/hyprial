import { publicErrorCode } from '../shared/public-error-code';

/** GUI-host authentication capability. No OIDC tokens enter this interface. */
export interface AuthState {
  readonly authenticated: boolean;
  readonly owner: string | null;
  readonly deviceReady: boolean;
  readonly sidecarError: string | null;
  readonly ready: boolean;
}
export interface AuthProgress {
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
export interface AuthResult {
  readonly ok: boolean;
  readonly state?: AuthState;
  readonly code?: string;
  readonly device?: { readonly ready: boolean; readonly error?: { readonly code: string } };
}
/** Cancellation is a two-part acknowledgement, never an inferred success. */
export interface AuthCancelResult {
  readonly ok: boolean;
  readonly cancelled: boolean;
  readonly refusal?: { readonly code: string; readonly source?: string };
}
/** Native-only acknowledgement; no URL or opener detail crosses this shape. */
export interface AuthOpenResult {
  readonly ok: boolean;
  readonly opened: boolean;
  readonly code?: string;
  readonly retryable?: boolean;
}
export interface AuthLogoutResult {
  readonly ok: boolean;
  readonly status?: 'logged-out' | 'not-logged-in';
  readonly localOnly?: true;
  readonly code?: string;
}
export interface AuthClient {
  status(): Promise<AuthState>;
  login(onProgress?: (progress: AuthProgress) => void): Promise<AuthResult>;
  device(onProgress?: (progress: AuthProgress) => void): Promise<AuthResult>;
  cancel(): Promise<AuthCancelResult>;
  open?(): Promise<AuthOpenResult>;
  logout?(): Promise<AuthLogoutResult>;
}
export class AuthError extends Error {
  constructor(readonly code: string) { super(code); }
}

function logoutReceipt(value: unknown): AuthLogoutResult {
  if (!value || typeof value !== 'object') throw new AuthError('AUTH_LOGOUT_UNKNOWN');
  const receipt = value as Partial<AuthLogoutResult>;
  if (receipt.ok !== true || receipt.localOnly !== true
    || (receipt.status !== 'logged-out' && receipt.status !== 'not-logged-in')) {
    throw new AuthError('AUTH_LOGOUT_UNKNOWN');
  }
  return { ok: true, status: receipt.status, localOnly: true };
}

function failedCancel(): AuthCancelResult {
  return { ok: false, cancelled: false };
}

function failedOpen(code?: string, retryable?: boolean): AuthOpenResult {
  return {
    ok: false,
    opened: false,
    ...(code ? { code } : {}),
    ...(typeof retryable === 'boolean' ? { retryable } : {}),
  };
}

function normalizeOpenAnswer(value: unknown): AuthOpenResult {
  if (!value || typeof value !== 'object') return failedOpen();
  const { ok, opened, code, retryable } =
    value as { readonly ok?: unknown; readonly opened?: unknown;
      readonly code?: unknown; readonly retryable?: unknown };
  if (ok === true && opened === true) return { ok: true, opened: true };
  if (ok !== false || opened !== false || typeof retryable !== 'boolean') {
    return failedOpen();
  }
  const safe = publicErrorCode(code);
  // A retryable failure must name a safe host code; otherwise the UI gets a
  // non-retryable failure rather than trusting a partial native answer.
  if (retryable && !safe) return failedOpen();
  return failedOpen(safe, retryable);
}

function normalizeOpenError(error: unknown): AuthOpenResult {
  if (!error || typeof error !== 'object') return failedOpen();
  const { code, retryable } = error as { readonly code?: unknown; readonly retryable?: unknown };
  const safe = publicErrorCode(code);
  if (typeof retryable !== 'boolean' || (retryable && !safe)) return failedOpen();
  return failedOpen(safe, retryable);
}

/**
 * Native hosts may inject this same narrow capability; absence is not success.
 * A missing, malformed or thrown cancel/open result cannot confirm an action,
 * and no raw opener message, URL or credential crosses this adapter.
 */
export function createNativeAuthClient(capability: AuthClient): AuthClient {
  const logout = capability.logout;
  const client: AuthClient = {
    ...(typeof logout === 'function' ? {
      async logout() { return logoutReceipt(await logout.call(capability)); },
    } : {}),
    status: () => capability.status(),
    login: listener => capability.login(listener),
    device: listener => capability.device(listener),
    async cancel() {
      try {
        const result: unknown = await capability.cancel();
        if (!result || typeof result !== 'object') return failedCancel();
        const { ok, cancelled } =
          result as { readonly ok?: unknown; readonly cancelled?: unknown };
        // `ok:false` always fails; carry only a genuine success and whether the
        // native host actually cancelled.
        return ok === true && typeof cancelled === 'boolean'
          ? { ok: true, cancelled }
          : failedCancel();
      } catch { return failedCancel(); }
    },
  };
  const open = capability.open;
  if (typeof open !== 'function') return client;
  return {
    ...client,
    async open() {
      try { return normalizeOpenAnswer(await open.call(capability)); }
      catch (error) { return normalizeOpenError(error); }
    },
  };
}
