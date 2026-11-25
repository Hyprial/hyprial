/*
 * Decision record (unavailable copy to notifications, PR 1f/F0b, 2026-11-14):
 * - One classifier for "this method was refused before dispatch": the pinned
 *   catalog or the composition did not admit it (METHOD_NOT_FOUND,
 *   METHOD_NOT_CALLABLE, WRITE_METHOD_UNVERIFIED, METHOD_NOT_AVAILABLE) or the
 *   composition has no handler (GUI_CAPABILITY_UNAVAILABLE, M1_WRITE_UNAVAILABLE).
 *   Adapters throw MethodRefusedError instead of building their own sentence;
 *   click handlers ask accessRefusal; catch blocks ask methodRefusal.
 * - The result is machine tokens only (`code`, `source` = method), which is
 *   structurally what useNotifyUnavailable takes: the notification center
 *   shows `method · CODE`. Human text is never produced here.
 * - MethodRefusedError.message is machine text. The catalog `reason` sentence
 *   is carried only as `reason` so that call sites that still present it keep
 *   their current output until their feature-area PR drops it; it is never
 *   part of a MethodRefusal.
 * - Classification is duck-typed (ClientError, AgentRequestError and plain
 *   carriers) and requires dispatch === 'not-dispatched', so a dispatched
 *   DomainError, an HTTP 500 or a network failure is never a refusal.
 */
import type { MethodAccess } from './types';

export const METHOD_REFUSAL_CODES: ReadonlySet<string> = new Set([
  'METHOD_NOT_FOUND',
  'METHOD_NOT_CALLABLE',
  'WRITE_METHOD_UNVERIFIED',
  'METHOD_NOT_AVAILABLE',
  'GUI_CAPABILITY_UNAVAILABLE',
  'M1_WRITE_UNAVAILABLE',
]);

export class MethodRefusedError extends Error {
  readonly dispatch = 'not-dispatched' as const;
  readonly method: string;
  readonly code: string;
  readonly reason: string | undefined;

  constructor(method: string, code: string, reason?: string) {
    super(`${code}: ${method}`);
    this.name = 'MethodRefusedError';
    this.method = method;
    this.code = code;
    this.reason = reason;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export interface MethodRefusal {
  readonly code: string;
  readonly source?: string;
}

export function assertMethodAvailable(
  access: MethodAccess | undefined,
  method: string,
  params?: unknown,
): void {
  const availability = access?.(method, params);
  if (availability && !availability.available) {
    throw new MethodRefusedError(method, availability.code ?? 'METHOD_NOT_AVAILABLE', availability.reason);
  }
}

function sourceOf(value: object): string | undefined {
  const direct = (value as { readonly method?: unknown }).method;
  if (typeof direct === 'string' && direct) return direct;
  for (const key of ['diagnostics', 'data'] as const) {
    const nested = (value as Readonly<Record<string, unknown>>)[key];
    if (nested !== null && typeof nested === 'object') {
      const method = (nested as { readonly method?: unknown }).method;
      if (typeof method === 'string' && method) return method;
    }
  }
  return undefined;
}

export function methodRefusal(value: unknown): MethodRefusal | null {
  if (value === null || typeof value !== 'object') return null;
  const { code, dispatch } = value as { readonly code?: unknown; readonly dispatch?: unknown };
  if (dispatch !== 'not-dispatched' || typeof code !== 'string' || !METHOD_REFUSAL_CODES.has(code)) return null;
  const source = sourceOf(value);
  return source === undefined ? { code } : { code, source };
}

export function accessRefusal(
  access: MethodAccess | undefined,
  method: string,
  params?: unknown,
): MethodRefusal | null {
  const availability = access?.(method, params);
  if (!availability || availability.available) return null;
  return { code: availability.code ?? 'METHOD_NOT_AVAILABLE', source: method };
}
