/** Injected controller port; authority and transport belong to the composition root. */
export interface LivePort {
  call(operation: string, params?: Record<string, unknown>): Promise<unknown>;
}

export class LiveError extends Error {
  constructor(readonly code: string, message: string, readonly definitive = false, readonly canRetry = false) {
    super(message);
    this.name = 'LiveError';
  }
}
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new LiveError('invalid-response', '服务响应无法确认。');
  return value as Record<string, unknown>;
}
export function string(value: unknown): string {
  if (typeof value !== 'string' || !value) throw new LiveError('invalid-response', '服务响应无法确认。');
  return value;
}
export function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new LiveError('invalid-response', '服务响应无法确认。');
  return value;
}
