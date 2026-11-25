import { publicErrorCode } from '../shared/public-error-code';

/** First-run host capability; no model key or grant value is returned. */
export interface SetupResult {
  readonly ok: boolean;
  readonly code?: string;
  readonly saved?: boolean;
  readonly keySaved?: boolean | null;
  readonly pendingAction?: 'save' | null;
  readonly modelVerified: false;
}
export interface SetupClient {
  status(): Promise<SetupResult>;
  saveModelKey(key: string): Promise<SetupResult>;
}
export class SetupError extends Error {
  constructor(readonly code: string) { super(code); }
}

const SAVE_NOT_DISPATCHED_CODES = new Set([
  'AUTH_NOT_OWNER',
  'AUTH_BUSY',
  'AUTH_REQUEST_REJECTED',
  'AUTH_CONFIGURATION_INVALID',
  'AUTH_SESSION_LIMIT',
  'AUTH_ORIGIN_REJECTED',
  'AUTH_UNAVAILABLE',
  'SETUP_BUSY',
  'SETUP_CLOSED',
  'SETUP_INVALID_ACTION',
  'SETUP_UNAVAILABLE',
  'SETUP_CONFIGURATION_INVALID',
  'MODEL_KEY_REQUIRED',
  'MODEL_KEY_ALREADY_GRANTED',
  'INVALID_MODEL_KEY',
  'SETUP_REVIEW_REQUIRED',
]);
export function isSaveNotDispatchedCode(code?: string): boolean {
  return code !== undefined && SAVE_NOT_DISPATCHED_CODES.has(code);
}
function explicitCode(error: unknown): string | undefined {
  if (error && typeof error === 'object' && 'code' in error) {
    return publicErrorCode(error.code);
  }
  return undefined;
}
function hostCode(error: unknown): string {
  return explicitCode(error) ?? 'SETUP_UNAVAILABLE';
}
function nativeResult(value: unknown): SetupResult {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new SetupError('SETUP_PROTOCOL_ERROR');
  }
  const record = value as Record<string, unknown>;
  if (record.ok !== true) throw new SetupError(publicErrorCode(record.code) ?? 'SETUP_FAILED');
  const pending = record.pendingAction;
  return {
    ok: true,
    ...(typeof record.saved === 'boolean' ? { saved: record.saved } : {}),
    ...(typeof record.keySaved === 'boolean' ? { keySaved: record.keySaved } : {}),
    ...(pending === null || pending === 'save'
      ? { pendingAction: pending }
      : {}),
    modelVerified: false,
  };
}
/**
 * The native host injects the same narrow setup capability; absence, a thrown
 * action or a malformed answer is not success and never confirms a saved key.
 */
export function createNativeSetupClient(capability: SetupClient): SetupClient {
  async function operation(run: () => Promise<unknown>): Promise<SetupResult> {
    try { return nativeResult(await run()); }
    catch (error) {
      if (error instanceof SetupError) throw error;
      throw new SetupError(hostCode(error));
    }
  }
  async function saveOperation(run: () => Promise<unknown>): Promise<SetupResult> {
    try { return nativeResult(await run()); }
    catch (error) {
      const code = error instanceof SetupError ? error.code : explicitCode(error);
      if (code && isSaveNotDispatchedCode(code)) throw new SetupError(code);
      throw new SetupError('SETUP_RESULT_UNKNOWN');
    }
  }
  return {
    status: () => operation(() => capability.status()),
    saveModelKey(key) {
      let input = key;
      try { return saveOperation(() => capability.saveModelKey(input)); }
      finally { input = ''; }
    },
  };
}
