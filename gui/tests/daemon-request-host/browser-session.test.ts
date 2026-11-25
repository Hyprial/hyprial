import { describe, expect, it, vi } from 'vitest';
import { BrowserBootstrapError, connectBrowserBackend, requestSessionStatus, watchBrowserSession } from '../../src/app/backend';

const ORIGIN = 'http://127.0.0.1:4173';
const VIEW = 'view-proof';
const GRANT = 'launch-grant-value';
const DIGEST = 'sha256:catalog';
const METHODS = [
  { name: 'agent.list', callable: true, state: 'available', mutates: false },
  { name: 'agent.create', callable: true, state: 'available', mutates: true },
] as const;

it('retires the old connection and refreshes in place without replaying a business request', async () => {
  vi.useFakeTimers();
  let stop = () => {};
  try {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({ driver: 'daemon' }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, current: true })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, current: false })))
      .mockResolvedValueOnce(sessionResponse({ generation: 'generation-2', csrf: 'csrf-2' }));
    const configured = options({ hash: `#grant=${GRANT}`, fetch: fetcher });
    const initial = await connectBrowserBackend(configured);
    configured.location.hash = '';
    const changed = vi.fn();
    const connected = vi.fn();
    const failed = vi.fn();
    stop = watchBrowserSession(initial, { changing: changed, connected, failed }, configured);
    initial.connection.beginTransition?.('logout');
    expect(changed).toHaveBeenCalledOnce();
    expect(changed).toHaveBeenCalledWith('logout');
    await vi.advanceTimersByTimeAsync(1000);
    expect(connected).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(changed).toHaveBeenCalledOnce();
    expect(connected).toHaveBeenCalledOnce();
    expect(connected.mock.calls[0][0].session.generation).toBe('generation-2');
    expect(failed).not.toHaveBeenCalled();
    await expect(initial.connection.client.agent.create({ name: 'no-replay' } as never)).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RETIRED' });
    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      `${ORIGIN}/daemon/session`, `${ORIGIN}/daemon/session/status`, `${ORIGIN}/daemon/session/status`, `${ORIGIN}/daemon/session`,
    ]);
    expect(JSON.parse(String(fetcher.mock.calls[3][1]?.body))).toEqual({});
    stop();
    await expect(connected.mock.calls[0][0].connection.client.agent.list({})).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RETIRED' });
  } finally { stop(); vi.useRealTimers(); }
});

class MemoryStorage {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

it.each(['503', 'network', 'rotation'])('ends recovery after 30 seconds of %s without replaying writes', async mode => {
  vi.useFakeTimers();
  let stop = () => {};
  try {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(sessionResponse({ driver: 'daemon' }))
      .mockImplementation(async () => {
        if (mode === 'network') throw new TypeError('offline');
        return mode === 'rotation'
          ? new Response(JSON.stringify({ ok: true, current: true }))
          : new Response('{}', { status: 503 });
      });
    const configured = options({ hash: `#grant=${GRANT}`, fetch: fetcher });
    const initial = await connectBrowserBackend(configured);
    configured.location.hash = '';
    const changing = vi.fn(), connected = vi.fn(), failed = vi.fn();
    stop = watchBrowserSession(initial, { changing, connected, failed }, configured);
    if (mode === 'rotation') initial.connection.beginTransition?.('initialize');
    else await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(failed).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(failed).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ code: 'SESSION_RECOVERY_TIMEOUT' }));
    expect(changing).toHaveBeenCalledOnce();
    expect(connected).not.toHaveBeenCalled();
    const calls = fetcher.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(calls);
    expect(fetcher.mock.calls.every(([url]) => !String(url).includes('/daemon/request/'))).toBe(true);
    await expect(initial.connection.client.agent.create({ name: 'no-replay' } as never)).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RETIRED' });
  } finally { stop(); vi.useRealTimers(); }
});

it('ignores a handshake arriving after the recovery deadline', async () => {
  vi.useFakeTimers();
  let stop = () => {};
  try {
    let resolve!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({ driver: 'daemon' }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, current: false })))
      .mockImplementation(() => new Promise(done => { resolve = done; }));
    const configured = options({ hash: `#grant=${GRANT}`, fetch: fetcher });
    const initial = await connectBrowserBackend(configured);
    configured.location.hash = '';
    const connected = vi.fn(), failed = vi.fn();
    stop = watchBrowserSession(initial, { changing: vi.fn(), connected, failed }, configured);
    initial.connection.beginTransition?.('logout');
    await vi.advanceTimersByTimeAsync(30_000);
    expect(failed).toHaveBeenCalledOnce();
    resolve(sessionResponse({ generation: 'late-generation' }));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(connected).not.toHaveBeenCalled();
    expect(failed).toHaveBeenCalledOnce();
    expect(fetcher).toHaveBeenCalledTimes(3);
  } finally { stop(); vi.useRealTimers(); }
});

it('clears a successful recovery deadline and gives a later transition its own budget', async () => {
  vi.useFakeTimers();
  let stop = () => {};
  try {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({ driver: 'daemon' }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, current: false })))
      .mockResolvedValueOnce(sessionResponse({ driver: 'daemon', generation: 'generation-2' }))
      .mockImplementation(async () => new Response(JSON.stringify({ ok: true, current: true })));
    const configured = options({ hash: `#grant=${GRANT}`, fetch: fetcher });
    const initial = await connectBrowserBackend(configured);
    configured.location.hash = '';
    const connected = vi.fn(), failed = vi.fn();
    stop = watchBrowserSession(initial, { changing: vi.fn(), connected, failed }, configured);
    initial.connection.beginTransition?.('initialize');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(connected).toHaveBeenCalledOnce();
    expect(failed).not.toHaveBeenCalled();
    connected.mock.calls[0][0].connection.beginTransition('logout');
    await vi.advanceTimersByTimeAsync(29_999);
    expect(failed).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(failed).toHaveBeenCalledOnce();
  } finally { stop(); vi.useRealTimers(); }
});

function sessionResponse(
  overrides: Record<string, unknown> = {},
  catalogOverrides: Record<string, unknown> = {},
): Response {
  return new Response(JSON.stringify({
    schemaVersion: 1,
    ok: true,
    requestId: 'session-request',
    driver: 'contract-mock',
    phase: 'ready',
    view: VIEW,
    generation: 'generation-1',
    csrf: 'csrf-1',
    expiresAt: Date.now() + 60_000,
    principal: { owner: 'owner', node: 'node', actor: 'user:owner' },
    catalog: {
      digest: DIGEST,
      declarationDigest: 'sha256:declaration',
      backendEpoch: 'epoch',
      httpContractVersion: 1,
      catalogVersion: '1',
      methodCount: METHODS.length,
      methods: METHODS,
      ...catalogOverrides,
    },
    ...overrides,
  }), { status: 200, headers: { 'content-type': 'application/json' } });
}

function dispatch(method: string, result: unknown = {}): Response {
  return new Response(JSON.stringify({
    schemaVersion: 1,
    ok: true,
    method,
    requestId: `request-${method}`,
    dispatch: 'dispatched',
    result,
  }), { status: 200, headers: { 'content-type': 'application/json', 'x-hyprial-catalog-digest': DIGEST } });
}

function options(input: {
  readonly hash?: string;
  readonly storage?: MemoryStorage;
  readonly fetch: typeof fetch;
}) {
  const storage = input.storage ?? new MemoryStorage();
  const replaceState = vi.fn();
  return {
    fetch: input.fetch,
    storage,
    history: { replaceState },
    location: { origin: ORIGIN, hash: input.hash ?? '', pathname: '/', search: '' },
    replaceState,
  };
}

describe('browser same-origin bootstrap', () => {
  it('posts the session status probe with the pinned request shape and returns the raw response', async () => {
    const raw = new Response('{}', { status: 503 });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(raw);
    const response = await requestSessionStatus(
      { view: 'view-1', generation: 'gen-1' },
      { fetch: fetcher, location: { origin: ORIGIN } as Location },
    );
    expect(response).toBe(raw);
    const [url, init] = fetcher.mock.calls[0];
    expect(String(url)).toBe(`${ORIGIN}/daemon/session/status`);
    expect(init).toMatchObject({
      method: 'POST',
      credentials: 'include',
      redirect: 'error',
      cache: 'no-store',
      headers: { 'content-type': 'application/json', 'x-hyprial-view': 'view-1', 'x-hyprial-generation': 'gen-1' },
      body: '{}',
    });
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it('scrubs the fragment grant immediately and carries no bearer to browser calls', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse())
      .mockResolvedValueOnce(dispatch('agent.list'))
      .mockResolvedValueOnce(dispatch('agent.create'));
    const configured = options({ hash: `#grant=${GRANT}`, fetch: fetcher });

    const bootstrap = await connectBrowserBackend(configured);
    expect(bootstrap.connection.descriptor.verifiedWrites).toEqual(['agent.create']);
    expect(bootstrap.connection.descriptor.principalActor).toBe('user:owner');
    expect(bootstrap.connection.descriptor).not.toHaveProperty('csrf');
    expect(bootstrap.connection.descriptor).not.toHaveProperty('owner');
    expect(configured.replaceState).toHaveBeenCalledWith(null, '', '/');
    expect(configured.storage.values.get('hyprial.gui.session-view')).toBe(VIEW);
    expect(JSON.stringify([...configured.storage.values])).not.toContain(GRANT);

    const firstInit = fetcher.mock.calls[0][1];
    expect(String(fetcher.mock.calls[0][0])).toBe(`${ORIGIN}/daemon/session`);
    expect(firstInit).toMatchObject({ method: 'POST', credentials: 'include', body: JSON.stringify({ grant: GRANT }) });
    const handshakeHeaders = new Headers(firstInit?.headers);
    expect(handshakeHeaders.get('x-hyprial-view')).toBeNull();
    expect(handshakeHeaders.get('authorization')).toBeNull();

    await bootstrap.connection.client.agent.list({});
    await bootstrap.connection.client.agent.create({ name: 'reader' } as never);

    const readInit = fetcher.mock.calls[1][1];
    const readHeaders = new Headers(readInit?.headers);
    expect(readInit).toMatchObject({ credentials: 'include' });
    expect(readHeaders.get('x-hyprial-view')).toBe(VIEW);
    expect(readHeaders.get('x-hyprial-generation')).toBe('generation-1');
    expect(readHeaders.get('x-hyprial-catalog-digest')).toBe(DIGEST);
    expect(readHeaders.get('x-hyprial-csrf')).toBeNull();
    expect(readHeaders.get('authorization')).toBeNull();

    const writeHeaders = new Headers(fetcher.mock.calls[2][1]?.headers);
    expect(writeHeaders.get('x-hyprial-csrf')).toBe('csrf-1');
    expect(writeHeaders.get('authorization')).toBeNull();
  });

  it('decides CSRF from the session method list, not from the build metadata', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({}, {
        methods: [
          { name: 'agent.list', callable: true, state: 'available', mutates: true },
          { name: 'agent.create', callable: true, state: 'available', mutates: false },
        ],
      }))
      .mockResolvedValueOnce(dispatch('agent.list'))
      .mockResolvedValueOnce(dispatch('agent.create'));
    const bootstrap = await connectBrowserBackend(options({ hash: `#grant=${GRANT}`, fetch: fetcher }));
    expect(bootstrap.connection.descriptor.verifiedWrites).toEqual(['agent.list']);

    await bootstrap.connection.client.agent.list({});
    await bootstrap.connection.client.agent.create({ name: 'reader' } as never);

    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('x-hyprial-csrf')).toBe('csrf-1');
    expect(new Headers(fetcher.mock.calls[2][1]?.headers).get('x-hyprial-csrf')).toBeNull();
  });

  it.each([
    ['a grant handshake', `#grant=${GRANT}`, false],
    ['a refresh handshake', '', true],
  ])('declares the session contract version on %s and accepts a response that carries sessionContract', async (_label, hash, stored) => {
    const storage = new MemoryStorage();
    if (stored) storage.setItem('hyprial.gui.session-view', VIEW);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({ sessionContract: { version: 1, min: 1, max: 1 } }));
    const bootstrap = await connectBrowserBackend(options({ hash, storage, fetch: fetcher }));
    expect(bootstrap.session.view).toBe(VIEW);
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('x-hyprial-session-contract')).toBe('1');
  });

  it('declares the version on the handshake only', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse())
      .mockResolvedValueOnce(dispatch('agent.list'));
    const bootstrap = await connectBrowserBackend(options({ hash: `#grant=${GRANT}`, fetch: fetcher }));
    await bootstrap.connection.client.agent.list({});
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('x-hyprial-session-contract')).toBeNull();
  });

  it('shows the host refusal of an unsupported contract version without guessing', async () => {
    const refusal = new Response(JSON.stringify({
      schemaVersion: 1, ok: false, requestId: 'gui-1', code: 'SESSION_CONTRACT_UNSUPPORTED',
      message: 'unsupported session contract', dispatch: 'not-dispatched', data: { min: 2, max: 3 },
    }), { status: 409, headers: { 'content-type': 'application/json' } });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(refusal);
    await expect(connectBrowserBackend(options({ hash: `#grant=${GRANT}`, fetch: fetcher })))
      .rejects.toMatchObject({ code: 'SESSION_CONTRACT_UNSUPPORTED', status: 409 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('resumes the same view after reload and adopts the rotated generation', async () => {
    const storage = new MemoryStorage();
    storage.setItem('hyprial.gui.session-view', VIEW);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({ generation: 'generation-2', csrf: 'csrf-2' }))
      .mockResolvedValueOnce(dispatch('agent.list'));

    const configured = options({ storage, fetch: fetcher });
    const bootstrap = await connectBrowserBackend(configured);
    expect(configured.replaceState).not.toHaveBeenCalled();
    expect(fetcher.mock.calls[0][1]).toMatchObject({ body: '{}', credentials: 'include' });
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('x-hyprial-view')).toBe(VIEW);

    await bootstrap.connection.client.agent.list({});
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('x-hyprial-generation')).toBe('generation-2');
    expect(storage.getItem('hyprial.gui.session-view')).toBe(VIEW);
  });

  it('fails closed without a launcher grant or retained view', async () => {
    const fetcher = vi.fn<typeof fetch>();
    const configured = options({ fetch: fetcher });
    await expect(connectBrowserBackend(configured)).rejects.toMatchObject({
      code: 'SESSION_REQUIRED',
    } satisfies Partial<BrowserBootstrapError>);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('rejects a refreshed view mismatch and removes the stale proof', async () => {
    const storage = new MemoryStorage();
    storage.setItem('hyprial.gui.session-view', 'old-view');
    const configured = options({
      storage,
      fetch: vi.fn<typeof fetch>().mockResolvedValueOnce(sessionResponse({ view: 'other-view' })),
    });
    await expect(connectBrowserBackend(configured)).rejects.toMatchObject({ code: 'SESSION_VIEW_MISMATCH' });
    expect(storage.getItem('hyprial.gui.session-view')).toBeNull();
  });

  it('rejects a session response with the wrong envelope schema', async () => {
    const configured = options({
      hash: `#grant=${GRANT}`,
      fetch: vi.fn<typeof fetch>().mockResolvedValueOnce(sessionResponse({ schemaVersion: 2 })),
    });
    await expect(connectBrowserBackend(configured)).rejects.toMatchObject({
      code: 'BOOTSTRAP_INVALID_RESPONSE',
    });
  });

  it('scrubs malformed launch fragments before refusing them', async () => {
    const configured = options({
      hash: '#grant=first&grant=second',
      fetch: vi.fn<typeof fetch>(),
    });
    await expect(connectBrowserBackend(configured)).rejects.toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
    expect(configured.replaceState).toHaveBeenCalledWith(null, '', '/');
    expect(configured.fetch).not.toHaveBeenCalled();
  });

  it('rejects a method that is absent from the pinned catalog before dispatch', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({}, {
        methods: [
          { name: 'agent.list', callable: true, state: 'available', mutates: false },
          { name: 'message.query', callable: true, state: 'available', mutates: false },
        ],
      }));
    const bootstrap = await connectBrowserBackend(options({ hash: `#grant=${GRANT}`, fetch: fetcher }));
    expect(bootstrap.connection.methodStatus('agent.create')).toMatchObject({
      available: false,
      code: 'METHOD_NOT_FOUND',
    });
    await expect(bootstrap.connection.client.agent.create({ name: 'blocked' }))
      .rejects.toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    fetcher.mockResolvedValueOnce(dispatch('agent.list', { agents: [], agentsWithoutRoutine: [] }));
    await expect(bootstrap.connection.client.agent.list({})).resolves.toMatchObject({ agents: [] });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('rejects a non-callable method while retaining a readable sibling', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse({}, {
        methods: [
          { name: 'agent.list', callable: true, state: 'available', mutates: false },
          { name: 'agent.create', callable: false, state: 'unavailable', mutates: true },
        ],
      }));
    const bootstrap = await connectBrowserBackend(options({ hash: `#grant=${GRANT}`, fetch: fetcher }));
    expect(bootstrap.connection.methodStatus('agent.create')).toMatchObject({
      available: false,
      code: 'METHOD_NOT_CALLABLE',
    });
    await expect(bootstrap.connection.client.agent.create({ name: 'blocked' }))
      .rejects.toMatchObject({ code: 'METHOD_NOT_CALLABLE', dispatch: 'not-dispatched' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    fetcher.mockResolvedValueOnce(dispatch('agent.list', { agents: [], agentsWithoutRoutine: [] }));
    await expect(bootstrap.connection.client.agent.list({})).resolves.toMatchObject({ agents: [] });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});

describe('developer tools capability in the session response', () => {
  async function sessionWith(overrides: Record<string, unknown>) {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(sessionResponse(overrides));
    return (await connectBrowserBackend(options({ hash: `#grant=${GRANT}`, fetch: fetcher }))).session;
  }

  it('reads devTools.apiDocs when the host reports it', async () => {
    expect((await sessionWith({ devTools: { apiDocs: true } })).devTools).toEqual({ apiDocs: true });
    expect((await sessionWith({ devTools: { apiDocs: false } })).devTools).toEqual({ apiDocs: false });
  });

  it('leaves devTools undefined when the host does not report it', async () => {
    expect((await sessionWith({})).devTools).toBeUndefined();
  });

  it.each([
    ['a string', 'x'],
    ['an array', [true]],
    ['null', null],
    ['a non-boolean apiDocs', { apiDocs: 'yes' }],
    ['a missing apiDocs', {}],
  ])('ignores %s without failing the session', async (_label, devTools) => {
    expect((await sessionWith({ devTools })).devTools).toBeUndefined();
  });
});
