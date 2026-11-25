import { describe, expect, it, vi } from 'vitest';
import { createBackendConnection, type BackendDescriptor } from '@/app/backend';
import { HttpTransport, TransportError, type RequestDiagnostics } from '@/client';

const descriptor: BackendDescriptor = {
  interfaceVersion: 1,
  driver: 'daemon',
  phase: 'ready',
  generation: 'test-generation-1',
  catalogDigest: 'test-catalog',
};
const diagnostics: RequestDiagnostics = {
  method: 'org.create', requestId: null, status: null, dispatch: 'unknown',
  catalogDigest: 'test-catalog', attempts: 1, startedAt: 1, durationMs: 10,
};

describe('shared backend connection', () => {
  it.each(['daemon', 'contract-mock'] as const)('uses the same generated method path for %s', async driver => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
      schemaVersion: 1, ok: true, method: 'hosts', requestId: 'r-1',
      dispatch: 'dispatched', result: { hosts: [] },
    }), { headers: { 'content-type': 'application/json', 'x-hyprial-catalog-digest': 'test-catalog' } }));
    // The test supplies a direct transport. No product token or host is used.
    const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:47831', token: 'synthetic', fetch: fetcher });
    const connection = createBackendConnection(transport, { ...descriptor, driver });
    const orgAdapterClient = connection.client;
    const settingsAdapterClient = connection.client;
    expect(orgAdapterClient).toBe(settingsAdapterClient);
    await orgAdapterClient.hosts({});
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]?.[0]).toBe('http://127.0.0.1:47831/daemon/request/hosts');
  });

  it('pins the connection catalog even when a caller supplies another digest', async () => {
    const invoke = vi.fn().mockResolvedValue({ hosts: [] });
    const connection = createBackendConnection({ invoke }, descriptor);
    await connection.client.hosts({}, { catalogDigest: 'caller-digest' });
    expect(invoke).toHaveBeenCalledExactlyOnceWith('hosts', {}, {
      retry: 'safe', catalogDigest: 'test-catalog',
    });
  });

  it('admits every cataloged callable method, including writes, and rejects absent or non-callable methods', async () => {
    const invoke = vi.fn().mockImplementation(async (method: string) => {
      if (method === 'orgfs.read_at') return { contentB64: 'AA==' };
      if (method === 'orgfs.purge_plan') return { planId: 'plan-1' };
      if (method === 'agent.start') return { accepted: true, running: true, warnings: [] };
      throw new Error(`unexpected dispatch ${method}`);
    });
    const connection = createBackendConnection({ invoke }, {
      ...descriptor,
      methods: [
        { name: 'orgfs.read_at', callable: true, state: 'available', mutates: false },
        { name: 'orgfs.purge_plan', callable: true, state: 'available', mutates: true },
        { name: 'agent.start', callable: true, state: 'available', mutates: true },
        { name: 'agent.stop', callable: false, state: 'retired', mutates: true },
      ],
    });
    await expect(connection.client.orgfs.readAt({ spaceId: 'space', node: 'id:node', version: '1' }))
      .resolves.toEqual({ contentB64: 'AA==' });
    expect(invoke).toHaveBeenCalledExactlyOnceWith('orgfs.read_at', {
      spaceId: 'space', node: 'id:node', version: '1',
    }, expect.anything());
    await expect(connection.client.orgfs.purgePlan({
      spaceId: 'space', targets: [{ kind: 'blob', sha: 'abc' }],
    })).resolves.toEqual({ planId: 'plan-1' });
    expect(connection.methodAccess('orgfs.purge_plan')).toEqual({ available: true });
    await expect(connection.client.agent.start({ name: 'worker' })).resolves.toEqual({
      accepted: true, running: true, warnings: [],
    });
    expect(connection.methodAccess('agent.stop')).toMatchObject({
      available: false, code: 'METHOD_NOT_CALLABLE',
    });
    await expect(connection.client.agent.stop({ name: 'worker' })).rejects.toMatchObject({
      code: 'METHOD_NOT_CALLABLE', dispatch: 'not-dispatched',
    });
    expect(connection.methodAccess('agent.create')).toMatchObject({
      available: false, code: 'METHOD_NOT_FOUND',
    });
    await expect(connection.client.agent.create({
      name: 'missing', provider: null, model: null, preferredHarness: null,
    })).rejects.toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched' });
    expect(invoke.mock.calls.map(([method]) => method)).toEqual([
      'orgfs.read_at', 'orgfs.purge_plan', 'agent.start',
    ]);
  });

  it('passes an uncertain write error unchanged without retry or rollback', async () => {
    const failure = new TransportError('network', 'NETWORK_ERROR', 'response lost', diagnostics);
    const invoke = vi.fn().mockRejectedValue(failure);
    const connection = createBackendConnection({ invoke }, descriptor);
    await expect(connection.client.org.create({ org: 'test-org' })).rejects.toBe(failure);
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke.mock.calls[0]?.[2]).toMatchObject({ retry: 'never' });
    expect(failure.dispatch).toBe('unknown');
  });

  it('admits only mutating methods present in the verified write gate', async () => {
    const invoke = vi.fn().mockResolvedValue({});
    const connection = createBackendConnection({ invoke }, {
      ...descriptor,
      methods: [
        { name: 'org.list', callable: true, state: 'available', mutates: false },
        { name: 'org.create', callable: true, state: 'available', mutates: true },
        { name: 'org.delete', callable: true, state: 'available', mutates: true },
      ],
      verifiedWrites: ['org.create'],
    });
    expect(connection.methodAccess('org.create')).toEqual({ available: true });
    expect(connection.methodAccess('org.delete')).toMatchObject({
      available: false, code: 'WRITE_METHOD_UNVERIFIED',
    });
    await expect(connection.client.org.delete({ org: 'group-test' })).rejects.toMatchObject({
      code: 'WRITE_METHOD_UNVERIFIED', dispatch: 'not-dispatched',
    });
    expect(invoke).not.toHaveBeenCalled();
  });

  it('rejects a retained adapter before dispatch after retirement; a new generation works', async () => {
    const invoke = vi.fn().mockResolvedValue({ hosts: [] });
    const old = createBackendConnection({ invoke }, descriptor);
    const retained = old.client;
    old.retire();
    old.retire();
    await expect(retained.hosts({})).rejects.toMatchObject({
      code: 'BACKEND_CONNECTION_RETIRED', dispatch: 'not-dispatched',
      diagnostics: { attempts: 0 },
    });
    expect(invoke).not.toHaveBeenCalled();
    const next = createBackendConnection({ invoke }, { ...descriptor, generation: 'test-generation-2' });
    await next.client.hosts({});
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(next.client).not.toBe(retained);
  });

  it('does not replay or relabel a write already in flight when retiring', async () => {
    let reject!: (error: unknown) => void;
    const invoke = vi.fn(() => new Promise<never>((_resolve, rejectRequest) => { reject = rejectRequest; }));
    const connection = createBackendConnection({ invoke }, descriptor);
    const pending = connection.client.org.create({ org: 'test-org' });
    connection.retire();
    const failure = new TransportError('timeout', 'REQUEST_TIMEOUT', 'unknown outcome', diagnostics);
    const result = expect(pending).rejects.toBe(failure);
    reject(failure);
    await result;
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it('copies and freezes metadata rather than accepting mutable mode/generation changes', () => {
    const source = { ...descriptor };
    const connection = createBackendConnection({ invoke: vi.fn() }, source);
    source.generation = 'changed';
    expect(connection.descriptor.generation).toBe('test-generation-1');
    expect(Object.isFrozen(connection.descriptor)).toBe(true);
  });

  it.each([
    { driver: 'fixture' }, { phase: 'invented' }, { generation: '' },
    { catalogDigest: '' }, { interfaceVersion: 2 },
  ])('refuses malformed connection metadata: %j', invalid => {
    expect(() => createBackendConnection({ invoke: vi.fn() }, {
      ...descriptor, ...invalid,
    } as BackendDescriptor)).toThrow(TypeError);
  });
});
