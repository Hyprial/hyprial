/**
 * The only business adapter in this batch.
 *
 * It targets one explicit placeholder path and deliberately defines no
 * per-operation business contract. Every request is expected to be rejected
 * by the local stub with HTTP 501 and an explicit `not implemented` message.
 */

export const STUB_BUSINESS_PATH = '/api/gui/v1/not-wired';
export const BACKEND_NOT_WIRED = 'not implemented: hyprial backend not wired';
export const BACKEND_NOT_WIRED_CODE = 'BACKEND_NOT_WIRED';

export interface StubResult {
  readonly ok: false;
  readonly status: number;
  readonly code: string;
  readonly message: string;
  readonly command: string;
  readonly params: Readonly<Record<string, unknown>>;
}

export interface StubOperations {
  call(
    command: string,
    params?: Readonly<Record<string, unknown>>,
  ): Promise<StubResult>;
}

type JsonRecord = Record<string, unknown>;

const SENSITIVE_KEY =
  /(token|secret|password|passwd|credential|authorization|cookie|api[-_]?key|private[-_]?key|access[-_]?key|key)$/i;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function redactValue(
  value: unknown,
  key: string,
  depth: number,
): unknown {
  if (SENSITIVE_KEY.test(key)) {
    return '<redacted>';
  }
  if (depth >= 4) {
    return '<truncated>';
  }
  if (Array.isArray(value)) {
    return value
      .slice(0, 20)
      .map((entry) => redactValue(entry, key, depth + 1));
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value)
        .slice(0, 50)
        .map(([childKey, childValue]) => [
          childKey,
          redactValue(childValue, childKey, depth + 1),
        ]),
    );
  }
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : '<non-finite>';
  }
  return '<unsupported>';
}

export function redactParams(
  params: Readonly<Record<string, unknown>> | undefined,
): Readonly<Record<string, unknown>> {
  if (!params) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(params)
      .slice(0, 50)
      .map(([key, value]) => [key, redactValue(value, key, 0)]),
  );
}

export function formatStubNotice(result: StubResult): string {
  return result.status === 0
    ? '操作失败：服务暂时无法连接。'
    : '功能暂不可用。';
}

export class HttpStubOperations implements StubOperations {
  constructor(private readonly path = STUB_BUSINESS_PATH) {}

  async call(
    command: string,
    params: Readonly<Record<string, unknown>> = {},
  ): Promise<StubResult> {
    try {
      const response = await fetch(this.path, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
        },
        credentials: 'same-origin',
        cache: 'no-store',
        body: JSON.stringify({ command, params }),
      });
      const body: unknown = await response.json().catch(() => null);
      if (
        isRecord(body) &&
        body.ok === false &&
        typeof body.code === 'string' &&
        typeof body.message === 'string' &&
        typeof body.command === 'string'
      ) {
        return {
          ok: false,
          status: response.status,
          code: body.code,
          message: body.message,
          command: body.command,
          params: isRecord(body.params) ? body.params : {},
        };
      }
      return {
        ok: false,
        status: response.status,
        code: 'STUB_UNEXPECTED_RESPONSE',
        message: '服务返回了无法处理的响应。',
        command,
        params: {},
      };
    } catch {
      return {
        ok: false,
        status: 0,
        code: 'STUB_UNREACHABLE',
        message: '服务暂时无法连接。',
        command,
        params: {},
      };
    }
  }
}
