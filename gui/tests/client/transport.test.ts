import { describe, expect, it } from 'vitest';

import {
  CatalogDriftError,
  DomainError,
  EnvelopeError,
  HttpError,
  TransportError,
} from '@/client/errors';
import { HttpTransport, type HttpTransportConfig } from '@/client/transport';

interface FetchRecord {
  readonly url: string;
  readonly init: RequestInit;
}

function jsonResponse(
  status: number,
  payload: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function success(method: string, result: unknown, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schemaVersion: 1,
    ok: true,
    method,
    requestId: 'req-1',
    dispatch: 'dispatched',
    result,
    ...extra,
  };
}

function failure(
  method: string | null,
  code: string,
  dispatch: 'not-dispatched' | 'dispatched' | 'unknown',
  data?: Record<string, unknown>,
): Record<string, unknown> {
  return {
    schemaVersion: 1,
    ok: false,
    requestId: 'req-err',
    code,
    message: `${code} from the server`,
    dispatch,
    ...(method === null ? {} : { method }),
    ...(data === undefined ? {} : { data }),
  };
}

function makeTransport(
  fetchImpl: (url: string, init: RequestInit) => Promise<Response>,
  overrides: Partial<HttpTransportConfig> = {},
): { transport: HttpTransport; calls: FetchRecord[] } {
  const calls: FetchRecord[] = [];
  const transport = new HttpTransport({
    baseUrl: 'http://127.0.0.1:47831/',
    token: 'test-token-not-a-secret',
    fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const record = { url: String(input), init: init ?? {} };
      calls.push(record);
      return fetchImpl(record.url, record.init);
    }) as typeof fetch,
    requestBudgetMs: 1000,
    ...overrides,
  });
  return { transport, calls };
}

describe('HttpTransport', () => {
  it.each([undefined, {}, { field: 'summary' }])(
    'preserves optional error data %j without manufacturing an empty object', async (data) => {
      const { transport, calls } = makeTransport(async () =>
        jsonResponse(400, failure('probe.error', 'INVALID_ARGUMENT', 'dispatched', data)),
      );
      const error = await transport.invoke('probe.error', {}).catch((value: unknown) => value);
      expect(error).toBeInstanceOf(DomainError);
      expect((error as DomainError).data).toEqual(data);
      expect((error as DomainError).diagnostics.dispatch).toBe('dispatched');
      expect(calls).toHaveLength(1);
    },
  );

  it('posts the canonical method path with Bearer injection from the configured provider', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(200, success('agent.list', { agents: [] })),
    );

    const detailed = await transport.invokeDetailed<{ agents: unknown[] }>('agent.list', {});

    expect(detailed.result).toEqual({ agents: [] });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://127.0.0.1:47831/daemon/request/agent.list');
    const headers = new Headers(calls[0].init.headers);
    expect(headers.get('authorization')).toBe('Bearer test-token-not-a-secret');
    expect(headers.get('content-type')).toBe('application/json');
    expect(calls[0].url).not.toContain('test-token-not-a-secret');
    expect(detailed.diagnostics).toMatchObject({
      method: 'agent.list',
      status: 200,
      dispatch: 'dispatched',
      requestId: 'req-1',
      attempts: 1,
    });
  });

  it('resolves an async token provider lazily for every invocation', async () => {
    let minted = 0;
    const { transport, calls } = makeTransport(
      async () => jsonResponse(200, success('demo.ping', { pong: true })),
      { token: async () => `token-${(minted += 1)}` },
    );

    await transport.invoke('demo.ping', {});
    await transport.invoke('demo.ping', {});

    expect(calls.map((call) => new Headers(call.init.headers).get('authorization'))).toEqual([
      'Bearer token-1',
      'Bearer token-2',
    ]);
  });

  it('maps a pre-dispatch domain rejection to DomainError without changing dispatch', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(400, failure('agent.secret.list', 'TARGET_NOT_AUTHORIZED', 'not-dispatched')),
    );

    const error = await transport
      .invoke('agent.secret.list', { actor: 'agent:other' })
      .catch((value: unknown) => value);

    expect(error).toBeInstanceOf(DomainError);
    const domain = error as DomainError;
    expect(domain.code).toBe('TARGET_NOT_AUTHORIZED');
    expect(domain.diagnostics.dispatch).toBe('not-dispatched');
    expect(domain.diagnostics.status).toBe(400);
    expect(domain.diagnostics.attempts).toBe(1);
  });

  it('maps 401 to an HttpError and never retries it', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(401, failure('agent.list', 'HTTP_UNAUTHORIZED', 'not-dispatched')),
    );

    const error = await transport.invoke('agent.list', {}).catch((value: unknown) => value);

    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).status).toBe(401);
    expect((error as HttpError).diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(1);
  });

  it('retains dispatch=dispatched for a domain failure after the handler ran', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(500, failure('agent.provider.login', 'PROVIDER_FAILED', 'dispatched', { hint: 'check-credentials' })),
    );

    const error = (await transport
      .invoke('agent.provider.login', { actor: 'agent:a', provider: 'claude', files: [] })
      .catch((value: unknown) => value)) as DomainError;

    expect(error).toBeInstanceOf(DomainError);
    expect(error.diagnostics.dispatch).toBe('dispatched');
    expect(error.data).toEqual({ hint: 'check-credentials' });
  });

  it('marks a network failure as dispatch=unknown and never retries a write', async () => {
    const { transport, calls } = makeTransport(async () => {
      throw new TypeError('connection reset');
    });

    const error = (await transport
      .invoke('agent.provider.logout', { actor: 'agent:a', provider: 'claude' }, { retry: 'never' })
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('network');
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(calls).toHaveLength(1);
  });

  it('enforces the request budget and does not claim a rollback', async () => {
    const { transport, calls } = makeTransport(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          const signal = init.signal;
          signal?.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
          });
        }),
      { requestBudgetMs: 25 },
    );

    const error = (await transport
      .invoke('agent.provider.logout', { actor: 'agent:a', provider: 'claude' })
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('timeout');
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(error.diagnostics.durationMs).toBeGreaterThanOrEqual(20);
    expect(error).not.toHaveProperty('rolledBack');
    expect(error).not.toHaveProperty('definitelyNotSent');
    expect(calls).toHaveLength(1);
  });

  it('treats a caller abort after send as dispatch=unknown and stops at one attempt', async () => {
    const { transport, calls } = makeTransport(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
          });
        }),
      { requestBudgetMs: 1000 },
    );
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 10);

    const error = (await transport
      .invoke('agent.provider.logout', { actor: 'agent:a', provider: 'claude' }, { signal: controller.signal })
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('aborted');
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(calls).toHaveLength(1);
  });

  it('does not retry a safe read either; retry metadata is not an automatic policy', async () => {
    const { transport, calls } = makeTransport(async () => {
      throw new TypeError('connection reset');
    });

    const error = (await transport
      .invoke('agent.list', {}, { retry: 'safe' })
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.diagnostics.attempts).toBe(1);
    expect(calls).toHaveLength(1);
  });

  it('treats a caller abort before dispatch as not-dispatched and sends nothing', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(200, success('agent.list', { agents: [] })),
    );
    const controller = new AbortController();
    controller.abort();

    const error = (await transport
      .invoke('agent.list', {}, { signal: controller.signal })
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('aborted');
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(0);
  });

  it('applies the budget before dispatch and never sends after a late token', async () => {
    let release: (value: string) => void = () => {};
    const pending = new Promise<string>((resolve) => {
      release = resolve;
    });
    const { transport, calls } = makeTransport(
      async () => jsonResponse(200, success('agent.provider.logout', { revoked: true })),
      { token: () => pending, requestBudgetMs: 10 },
    );

    const started = performance.now();
    const error = (await transport
      .invoke('agent.provider.logout', { actor: 'agent:a', provider: 'claude' })
      .catch((value: unknown) => value)) as TransportError;
    const elapsed = performance.now() - started;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('timeout');
    expect(error.diagnostics.attempts).toBe(0);
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(0);
    expect(elapsed).toBeLessThan(50);

    release('late-token');
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(calls).toHaveLength(0);
  });

  it('applies the budget to catalog token preparation and does not fetch after a late token', async () => {
    let release: (value: string) => void = () => {};
    const pending = new Promise<string>((resolve) => {
      release = resolve;
    });
    const { transport, calls } = makeTransport(
      async () => jsonResponse(200, { schemaVersion: 1, ok: true }),
      { token: () => pending, requestBudgetMs: 10 },
    );

    const error = (await transport.catalog().catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('timeout');
    expect(error.diagnostics.attempts).toBe(0);
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(0);

    release('late-token');
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(calls).toHaveLength(0);
  });

  it('rejects an error envelope whose explicit method belongs to another request', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(400, failure('other.write', 'INVALID_ARGUMENT', 'not-dispatched')),
    );

    const error = (await transport.invoke('demo.write', {}).catch((value: unknown) => value)) as EnvelopeError;

    expect(error).toBeInstanceOf(EnvelopeError);
    expect(error.code).toBe('ENVELOPE_METHOD_MISMATCH');
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(error.diagnostics.status).toBe(400);
  });

  it('accepts a legal pre-dispatch error without an explicit method', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(400, failure(null, 'INVALID_ARGUMENT', 'not-dispatched')),
    );

    const error = (await transport
      .invoke('agent.secret.list', {})
      .catch((value: unknown) => value)) as DomainError;

    expect(error).toBeInstanceOf(DomainError);
    expect(error.code).toBe('INVALID_ARGUMENT');
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
  });

  it('rejects a success envelope whose dispatch is not dispatched', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(200, success('agent.list', { agents: [] }, { dispatch: 'unknown' })),
    );

    const error = (await transport.invoke('agent.list', {}).catch((value: unknown) => value)) as EnvelopeError;

    expect(error).toBeInstanceOf(EnvelopeError);
    expect(error.code).toBe('ENVELOPE_DISPATCH_MISMATCH');
    expect(error.diagnostics.dispatch).toBe('unknown');
  });

  it('rejects a success envelope without a result field', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(200, {
        schemaVersion: 1,
        ok: true,
        method: 'agent.list',
        requestId: 'req-1',
        dispatch: 'dispatched',
      }),
    );

    const error = (await transport.invoke('agent.list', {}).catch((value: unknown) => value)) as EnvelopeError;

    expect(error).toBeInstanceOf(EnvelopeError);
    expect(error.code).toBe('ENVELOPE_MISSING_RESULT');
  });

  it('does not retry a write on a 503 whose dispatch is unknown', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(503, failure('agent.provider.logout', 'DAEMON_UNAVAILABLE', 'unknown')),
    );

    const error = (await transport
      .invoke('agent.provider.logout', { actor: 'agent:a', provider: 'claude' })
      .catch((value: unknown) => value)) as DomainError;

    expect(error).toBeInstanceOf(DomainError);
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(error.diagnostics.attempts).toBe(1);
    expect(calls).toHaveLength(1);
  });

  it('maps an invalid success envelope to EnvelopeError with dispatch=unknown', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(200, { schemaVersion: 1, ok: true, requestId: 'req-1', dispatch: 'dispatched', result: {} }),
    );

    const error = (await transport.invoke('agent.list', {}).catch((value: unknown) => value)) as EnvelopeError;

    expect(error).toBeInstanceOf(EnvelopeError);
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(error.code).toBe('ENVELOPE_METHOD_MISMATCH');
  });

  it('rejects a response whose method does not match the requested method', async () => {
    const { transport } = makeTransport(async () =>
      jsonResponse(200, success('demo.ping', { pong: true })),
    );

    const error = (await transport.invoke('agent.list', {}).catch((value: unknown) => value)) as EnvelopeError;

    expect(error).toBeInstanceOf(EnvelopeError);
    expect(error.code).toBe('ENVELOPE_METHOD_MISMATCH');
  });

  it('rejects a non-JSON body instead of returning a business result', async () => {
    const { transport } = makeTransport(
      async () => new Response('<html>proxy error</html>', { status: 502 }),
    );

    const error = (await transport.invoke('agent.list', {}).catch((value: unknown) => value)) as EnvelopeError;

    expect(error).toBeInstanceOf(EnvelopeError);
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(error.diagnostics.status).toBe(502);
  });

  it('detects catalog digest drift from the response header', async () => {
    const { transport } = makeTransport(
      async () =>
        jsonResponse(
          200,
          success('agent.list', { agents: [] }),
          { 'x-hyprial-catalog-digest': 'sha256:new-catalog' },
        ),
      { expectedCatalogDigest: 'sha256:old-catalog' },
    );

    const error = (await transport
      .invoke('agent.list', {})
      .catch((value: unknown) => value)) as CatalogDriftError;

    expect(error).toBeInstanceOf(CatalogDriftError);
    expect(error.expected).toBe('sha256:old-catalog');
    expect(error.actual).toBe('sha256:new-catalog');
    expect(error.diagnostics.dispatch).toBe('dispatched');
  });

  it('sends the pinned digest header and maps an explicit catalog mismatch without a digest header', async () => {
    const { transport, calls } = makeTransport(
      async () =>
        jsonResponse(409, failure('agent.list', 'CATALOG_MISMATCH', 'not-dispatched')),
      { expectedCatalogDigest: 'sha256:pinned' },
    );

    const error = (await transport
      .invoke('agent.list', {}, { headers: { 'x-hyprial-catalog-digest': 'sha256:caller' } })
      .catch((value: unknown) => value)) as CatalogDriftError;

    expect(error).toBeInstanceOf(CatalogDriftError);
    expect(new Headers(calls[0].init.headers).get('x-hyprial-catalog-digest')).toBe('sha256:pinned');
  });

  it('keeps a 409 scenario refusal as its declared domain error', async () => {
    const { transport } = makeTransport(
      async () => jsonResponse(409, failure('agent.list', 'MOCK_SCENARIO_MISSING', 'not-dispatched')),
      { expectedCatalogDigest: 'sha256:pinned' },
    );

    const error = (await transport
      .invoke('agent.list', {})
      .catch((value: unknown) => value)) as DomainError;

    expect(error).toBeInstanceOf(DomainError);
    expect(error.code).toBe('MOCK_SCENARIO_MISSING');
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
  });

  it('refuses a non-positive budget before dispatch', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(200, success('agent.list', { agents: [] })),
    );

    const error = (await transport
      .invoke('agent.list', {}, { budgetMs: 0 })
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('configuration');
    expect(error.code).toBe('INVALID_BUDGET');
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(0);
  });

  it('does not fetch the catalog when the caller signal is already aborted', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(200, { schemaVersion: 1, ok: true }),
    );
    const controller = new AbortController();
    controller.abort();

    const error = (await transport.catalog({ signal: controller.signal }).catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('aborted');
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(0);
  });

  it('refuses a non-canonical method name before dispatch', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(200, success('agent.list', { agents: [] })),
    );

    const error = (await transport
      .invoke('agents/list', {})
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.kind).toBe('configuration');
    expect(error.diagnostics.dispatch).toBe('not-dispatched');
    expect(calls).toHaveLength(0);
  });

  it('fetches the served catalog snapshot through the same auth and budget boundary', async () => {
    const { transport, calls } = makeTransport(async () =>
      jsonResponse(200, {
        schemaVersion: 1,
        ok: true,
        httpContractVersion: 1,
        catalogVersion: '0.5.5.2',
        catalogDigest: 'sha256:catalog',
        declarationDigest: 'sha256:decl',
        generatedFrom: 'daemon.catalog',
        backend: { kind: 'daemon', socket: '/tmp/x.sock', version: '0.5.5.2', nodeId: 'n', owner: 'o', epoch: 'e' },
        principal: { mode: 'local-operator' },
        methods: [{ name: 'agent.list', path: '/daemon/request/agent.list', state: 'callable', callable: true }],
      }),
    );

    const catalog = await transport.catalog();

    expect(calls[0].url).toBe('http://127.0.0.1:47831/daemon/request');
    expect(calls[0].init.method).toBe('GET');
    expect(catalog.methods[0].name).toBe('agent.list');
    expect(catalog.catalogDigest).toBe('sha256:catalog');
  });
});
