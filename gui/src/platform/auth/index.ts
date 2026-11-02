import { createWebHostTransport, HostSessionError, type WebHostTransport } from '../transport';

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
}
export interface AuthClient {
  status(): Promise<AuthState>;
  login(onProgress?: (progress: AuthProgress) => void): Promise<AuthResult>;
  device(onProgress?: (progress: AuthProgress) => void): Promise<AuthResult>;
  cancel(): Promise<AuthCancelResult>;
}
export class AuthError extends Error {
  constructor(readonly code: string) { super(code); }
}

function failedCancel(): AuthCancelResult {
  return { ok: false, cancelled: false };
}

/**
 * Native hosts may inject this same narrow capability; absence is not success.
 * A missing, malformed or thrown cancel result cannot confirm an operation.
 */
export function createNativeAuthClient(capability: AuthClient): AuthClient {
  return {
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
}

export function createWebAuthClient(
  fetcher: typeof fetch = fetch,
  transport: WebHostTransport = createWebHostTransport(fetcher),
): AuthClient {
  let statusInFlight: Promise<AuthState> | null = null;
  async function request(action: string): Promise<Response> {
    try { return await transport.request(action); }
    catch (error) {
      if (error instanceof HostSessionError) throw new AuthError(error.code ?? 'AUTH_UNAVAILABLE');
      throw error;
    }
  }
  async function operation(action: string, onProgress?: (progress: AuthProgress) => void): Promise<AuthResult> {
    const response = await request(action);
    if (!response.ok) {
      const value = await response.json();
      throw new AuthError(value.code ?? 'AUTH_FAILED');
    }
    if (!response.body) throw new AuthError('AUTH_PROTOCOL_ERROR');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let bytes = 0;
    let result: AuthResult | undefined;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > 1_048_576) throw new AuthError('AUTH_PROTOCOL_ERROR');
        buffer += decoder.decode(value, { stream: true });
        let newline: number;
        while ((newline = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
          if (!line.trim()) continue;
          const frame = JSON.parse(line);
          if (frame.type === 'progress' && !result) onProgress?.(frame.data as AuthProgress);
          else if (frame.type === 'result' && !result) result = frame.data as AuthResult;
          else throw new AuthError('AUTH_PROTOCOL_ERROR');
        }
      }
      if (!result || buffer.trim()) throw new AuthError('AUTH_PROTOCOL_ERROR');
      return result;
    } catch (error) {
      await reader.cancel().catch(() => {});
      throw error;
    } finally { reader.releaseLock(); }
  }
  return {
    status() {
      statusInFlight ??= (async () => {
        const response = await request('status');
        const result = await response.json() as AuthResult;
        if (!response.ok || !result.ok || !result.state) throw new AuthError(result.code ?? 'AUTH_FAILED');
        return result.state;
      })().finally(() => { statusInFlight = null; });
      return statusInFlight;
    },
    login: listener => operation('login', listener),
    device: listener => operation('device', listener),
    async cancel(): Promise<AuthCancelResult> {
      const response = await request('cancel');
      if (!response.ok) throw new AuthError('AUTH_CANCEL_FAILED');
      // The host's success response is the web transport's acknowledgement;
      // callers still refresh authoritative status before retrying login.
      return { ok: true, cancelled: true };
    },
  };
}
