/** Same-origin transport. The server establishes identity; browsers never supply it. */
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

export class HttpLiveClient implements LivePort {
  private token: string | null = null;
  constructor(private readonly fetcher: typeof fetch = fetch, private readonly timeoutMs = 30_000) {}

  async bootstrap(): Promise<boolean> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetcher.call(globalThis, '/api/gui/v1/live/bootstrap', {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new LiveError('unavailable', '真实服务尚未连接。');
      const body = object(await response.json());
      if (body.enabled === false) return false;
      if (body.enabled !== true) throw new LiveError('invalid-response', '无法确认服务连接方式。');
      this.token = string(body.csrfToken);
      return true;
    } catch (error) {
      if (error instanceof LiveError) throw error;
      throw new LiveError('unavailable', '真实服务尚未连接。');
    } finally { clearTimeout(timer); }
  }

  async call(operation: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (!this.token) throw new LiveError('unavailable', '真实服务尚未连接。');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetcher.call(globalThis, '/api/gui/v1/live', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { 'Content-Type': 'application/json', 'X-Hyprial-Gui-Token': this.token },
        body: JSON.stringify({ operation, params }),
      });
      const body = object(await response.json());
      if (response.ok && body.ok === true && 'result' in body) return body.result;
      if (body.ok === false) {
        const error = object(body.error);
        throw new LiveError(string(error.code), string(error.message), error.definitive === true,
          error.definitive === true && error.canRetry === true);
      }
      throw new LiveError('invalid-response', '操作结果尚未确认。');
    } catch (error) {
      if (error instanceof LiveError) throw error;
      throw new LiveError('transport', '未收到可确认的响应，请查询原操作结果。');
    } finally { clearTimeout(timer); }
  }
}
