import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  DomainError,
  EnvelopeError,
  HttpError,
  TransportError,
  type DispatchState,
  type RequestDiagnostics,
} from '../../../src/client';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { createBackendConnection } from '../../../src/app/backend';
import type { OrgStore } from '../../../src/features/org/impl/domain/context';
import { OrgDomainProvider, UNWIRED_SOURCE } from '../../../src/features/org/impl/domain/context';
import { createOrgLiveSource } from '../../../src/features/org/impl/domain/live-source';
import { ORG_COPY } from '../../../src/features/org/impl/domain/copy';
import type { OrgDomainSnapshot } from '../../../src/features/org/impl/domain/types';
import {
  createOrgLiveSourceForPort,
  createOrgRequestApiForPort,
} from '../../support/org-backend';

const DIAGNOSTICS: RequestDiagnostics = {
  method: 'org.fetch',
  requestId: null,
  status: null,
  dispatch: 'unknown',
  catalogDigest: null,
  attempts: 1,
  startedAt: 0,
  durationMs: 1,
};

function diagnostics(dispatch: DispatchState, method = 'org.fetch'): RequestDiagnostics {
  return { ...DIAGNOSTICS, method, dispatch };
}

function memoryStore(initial: OrgDomainSnapshot): OrgStore {
  let current = initial;
  return {
    get: () => current,
    update: (next) => {
      current = next(current);
    },
  };
}

function fakePort(result: (method: string, params: Record<string, unknown>) => unknown) {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  return {
    calls,
    async request(method: string, params: Record<string, unknown>) {
      calls.push({ method, params });
      return result(method, params);
    },
  };
}

const DETAIL_METHODS = [
  'targets',
  'org.network',
  'hosts',
  'orgfs.spaces',
  'orgfs.members',
  'orgfs.status',
  'orgfs.ls',
  'orgfs.trash',
];

const LIST_RESULT = {
  orgs: [
    { org: 'group-hyprial-dev', spaceId: 'space-team', role: 'owner', devices: 3, state: 'active' },
    { org: 'group-acme-lab', spaceId: null, role: null, devices: 0, state: 'legacy-active' },
  ],
  leftOrgs: ['group-old'],
};

describe('org request adapter', () => {
  it('accepts null spaceId/role and keeps the row unchanged', async () => {
    const port = fakePort(() => LIST_RESULT);
    const api = createOrgRequestApiForPort(port);
    await expect(api.listOrgs()).resolves.toEqual(LIST_RESULT);
    expect(port.calls).toEqual([{ method: 'org.list', params: {} }]);
  });

  it('rejects a list row with an invalid shape', async () => {
    const port = fakePort(() => ({ orgs: [{ org: 'group-x', spaceId: 1 }], leftOrgs: [] }));
    await expect(createOrgRequestApiForPort(port).listOrgs()).rejects.toThrow(/格式不正确/);
  });

  it('sends the declared fetch defaults and returns counts with candidates', async () => {
    const result = {
      requestedSources: ['node-2'],
      responseCount: 2,
      receivedCount: 1,
      rejectedCount: 1,
      candidates: [
        {
          path: '/tmp/example/pending.json',
          version: 7,
          publisher: 'user:lin',
          receivedAt: '2026-11-02T09:30:00Z',
          source: 'node-2',
        },
      ],
    };
    const port = fakePort(() => result);
    const value = await createOrgRequestApiForPort(port).fetchContext();
    expect(port.calls).toEqual([
      { method: 'org.fetch', params: { timeoutSeconds: 2, requestMode: 'neighbors' } },
    ]);
    expect(value).toEqual(result);
  });

  it('passes an explicit from selector only when supplied', async () => {
    const port = fakePort(() => ({
      requestedSources: [],
      responseCount: 0,
      receivedCount: 0,
      rejectedCount: 0,
      candidates: [],
    }));
    await createOrgRequestApiForPort(port).fetchContext({ from: 'node-2', timeoutSeconds: 5 });
    expect(port.calls[0].params).toEqual({
      timeoutSeconds: 5,
      requestMode: 'neighbors',
      from: 'node-2',
    });
  });
});

describe('org live source', () => {
  it('dispatches a cataloged org.fetch once while retaining org.list reads', async () => {
    const fetched = {
      requestedSources: [],
      responseCount: 0,
      receivedCount: 0,
      rejectedCount: 0,
      candidates: [],
    };
    const port = fakePort((method) => method === 'org.fetch' ? fetched : LIST_RESULT);
    const connection = createBackendConnection({
      async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
        return await port.request(method, params as Record<string, unknown>) as TResult;
      },
    }, {
      interfaceVersion: 1,
      driver: 'daemon',
      phase: 'ready',
      generation: 'org-fetch-gate',
      catalogDigest: 'sha256:org-fetch-gate',
      methods: [
        { name: 'org.list', callable: true, state: 'available', mutates: false },
        { name: 'org.fetch', callable: true, state: 'available', mutates: true },
        { name: 'org.network', callable: true, state: 'available', mutates: false },
        { name: 'hosts', callable: true, state: 'available', mutates: false },
        { name: 'orgfs.spaces', callable: true, state: 'available', mutates: false },
        { name: 'orgfs.status', callable: true, state: 'available', mutates: false },
        { name: 'orgfs.ls', callable: true, state: 'available', mutates: false },
        { name: 'orgfs.trash', callable: true, state: 'available', mutates: false },
      ],
    });
    const source = createOrgLiveSource(connection.client);
    const store = memoryStore(source.initial);
    const gateway = source.createGateway(store);
    await expect(gateway.syncContext()).resolves.toMatchObject({
      kind: 'ok',
      value: fetched,
    });
    expect(port.calls.filter((call) => call.method === 'org.fetch')).toEqual([{
      method: 'org.fetch', params: { timeoutSeconds: 2, requestMode: 'neighbors' },
    }]);
    await expect(gateway.refresh(null)).resolves.toMatchObject({ kind: 'ok' });
    expect(port.calls.filter((call) => call.method === 'org.list')).toHaveLength(1);
  });

  it('does not call org.fetch on mount; refresh is the only automatic read', async () => {
    const port = fakePort(() => LIST_RESULT);
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const gateway = source.createGateway(store);
    await Promise.resolve();
    expect(port.calls).toEqual([]);
    const result = await gateway.refresh(null);
    expect(result.kind).toBe('ok');
    expect(port.calls).toEqual([
      { method: 'org.list', params: {} },
      { method: 'targets', params: { kind: 'agent' } },
      { method: 'org.network', params: { org: 'group-hyprial-dev' } },
      { method: 'hosts', params: {} },
      { method: 'orgfs.spaces', params: {} },
      { method: 'orgfs.members', params: { spaceId: 'space-team' } },
      { method: 'orgfs.status', params: { spaceId: 'space-team' } },
      { method: 'orgfs.ls', params: { spaceId: 'space-team', recursive: true } },
      { method: 'orgfs.trash', params: { spaceId: 'space-team' } },
    ]);
  });

  it('maps orgfs.members into people without inventing roles or agent links', async () => {
    const network = {
      orgs: ['group-hyprial-dev'],
      unavailableOrgs: [],
      dataProblems: [],
      directory: {
        'group-hyprial-dev': [
          {
            deviceId: 'dev-a',
            owner: 'user:alice',
            presence: 'online',
            updatedAt: '2026-11-02T09:30:00Z',
          },
          {
            deviceId: 'dev-b',
            owner: 'user:bob',
            presence: 'offline',
            updatedAt: '2026-11-02T09:30:00Z',
          },
        ],
      },
      self: { deviceId: 'dev-a', owner: 'user:alice' },
    };
    const port = fakePort((method) => {
      switch (method) {
        case 'org.list':
          return LIST_RESULT;
        case 'org.network':
          return network;
        case 'hosts':
          return { hosts: [] };
        case 'orgfs.spaces':
          return {
            spaces: [
              {
                space_id: 'space-team',
                name: 'team',
                owner: 'user:alice',
                created_at: '2026-11-02T09:30:00Z',
              },
            ],
          };
        case 'orgfs.members':
          return {
            members: [
              {
                space_id: 'space-team',
                user: 'user:alice',
                mode: 'rw',
                added_by: 'user:alice',
                added_at: '2026-11-02T09:30:00Z',
              },
              {
                space_id: 'space-team',
                user: 'user:bob',
                mode: 'ro',
                added_by: 'user:alice',
                added_at: '2026-11-02T09:31:00Z',
              },
            ],
          };
        case 'orgfs.status':
          return { space_id: 'space-team' };
        case 'orgfs.ls':
        case 'orgfs.trash':
          return { nodes: [] };
        default:
          throw new Error(`unexpected ${method}`);
      }
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    await expect(source.createGateway(store).refresh(null)).resolves.toMatchObject({ kind: 'ok' });

    const detail = store.get().details['group-hyprial-dev'];
    expect(detail?.people).toEqual([
      expect.objectContaining({
        id: 'user:alice',
        account: 'alice',
        displayName: 'alice',
        role: 'owner',
        source: 'orgfs',
        sync: 'synced',
        isMe: true,
        machineIds: ['dev-a'],
        agentIds: [],
      }),
      expect.objectContaining({
        id: 'user:bob',
        account: 'bob',
        displayName: 'bob',
        role: 'member',
        source: 'orgfs',
        sync: 'synced',
        isMe: false,
        machineIds: ['dev-b'],
        agentIds: [],
      }),
    ]);
    expect(port.calls.filter((call) => call.method === 'orgfs.members')).toEqual([
      { method: 'orgfs.members', params: { spaceId: 'space-team' } },
    ]);

    const user = userEvent.setup();
    render(
      <OrgDomainProvider source={{ ...source, initial: store.get(), autoload: false }}>
        <FixtureApp dataMode="fixture" firstRunMode="complete" />
      </OrgDomainProvider>,
    );
    await user.click(screen.getByRole('button', { name: '组织' }));
    await user.click(screen.getByRole('tab', { name: '成员' }));
    await user.click(screen.getByRole('button', { name: /查看成员.*bob/ }));
    const association = screen.getByText('在本组织的 Agent').parentElement;
    expect(association).toHaveTextContent(ORG_COPY.unknown);
    expect(association).not.toHaveTextContent('没有 Agent');
  });

  it('keeps people unknown when orgfs.members fails', async () => {
    const port = fakePort((method) => {
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'org.network') {
        return {
          orgs: ['group-hyprial-dev'],
          unavailableOrgs: [],
          dataProblems: [],
          directory: {},
          self: {},
        };
      }
      if (method === 'hosts') return { hosts: [] };
      if (method === 'orgfs.spaces') {
        return {
          spaces: [
            {
              space_id: 'space-team',
              name: 'team',
              owner: 'user:alice',
              created_at: '2026-11-02T09:30:00Z',
            },
          ],
        };
      }
      if (method === 'orgfs.members') {
        throw new DomainError('ORG_NETWORK_ERROR', 'members failed', null, DIAGNOSTICS);
      }
      if (method === 'orgfs.status') return { space_id: 'space-team' };
      return { nodes: [] };
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    await expect(source.createGateway(store).refresh(null)).resolves.toMatchObject({ kind: 'ok' });

    expect(store.get().details['group-hyprial-dev']?.people).toBeNull();
  });

  it('keeps people unknown when org.network fails even if members succeed', async () => {
    const port = fakePort((method) => {
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'org.network') {
        throw new DomainError('ORG_NETWORK_ERROR', 'network failed', null, DIAGNOSTICS);
      }
      if (method === 'hosts') return { hosts: [] };
      if (method === 'orgfs.spaces') {
        return {
          spaces: [
            {
              space_id: 'space-team',
              name: 'team',
              owner: 'user:alice',
              created_at: '2026-11-02T09:30:00Z',
            },
          ],
        };
      }
      if (method === 'orgfs.members') {
        return {
          members: [
            {
              space_id: 'space-team',
              user: 'user:alice',
              mode: 'rw',
              added_by: 'user:alice',
              added_at: '2026-11-02T09:30:00Z',
            },
          ],
        };
      }
      if (method === 'orgfs.status') return { space_id: 'space-team' };
      return { nodes: [] };
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    await expect(source.createGateway(store).refresh(null)).resolves.toMatchObject({ kind: 'ok' });

    expect(store.get().details['group-hyprial-dev']?.people).toBeNull();
  });

  it('keeps people unknown when network.self.owner is missing', async () => {
    const port = fakePort((method) => {
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'org.network') {
        return {
          orgs: ['group-hyprial-dev'],
          unavailableOrgs: [],
          dataProblems: [],
          directory: {},
          self: {},
        };
      }
      if (method === 'hosts') return { hosts: [] };
      if (method === 'orgfs.spaces') {
        return {
          spaces: [
            {
              space_id: 'space-team',
              name: 'team',
              owner: 'user:alice',
              created_at: '2026-11-02T09:30:00Z',
            },
          ],
        };
      }
      if (method === 'orgfs.members') {
        return {
          members: [
            {
              space_id: 'space-team',
              user: 'user:alice',
              mode: 'rw',
              added_by: 'user:alice',
              added_at: '2026-11-02T09:30:00Z',
            },
          ],
        };
      }
      if (method === 'orgfs.status') return { space_id: 'space-team' };
      return { nodes: [] };
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    await expect(source.createGateway(store).refresh(null)).resolves.toMatchObject({ kind: 'ok' });

    expect(store.get().details['group-hyprial-dev']?.people).toBeNull();
  });

  it('maps org.list rows without inventing owner, authority or personal kind', async () => {
    const source = createOrgLiveSourceForPort(fakePort(() => LIST_RESULT));
    const store = memoryStore(source.initial);
    const gateway = source.createGateway(store);
    await gateway.refresh(null);
    const snapshot = store.get();
    expect(snapshot.status).toBe('ready');
    expect(snapshot.currentOrgId).toBe('group-hyprial-dev');
    expect(snapshot.orgs?.[0]).toMatchObject({
      id: 'group-hyprial-dev',
      name: 'hyprial-dev',
      kind: 'work',
      role: 'owner',
      lifecycle: 'active',
      ownerLabel: null,
      authorityDevice: null,
      deviceCount: 3,
    });
    expect(snapshot.orgs?.[1]).toMatchObject({
      role: null,
      lifecycle: 'active',
      deviceCount: 0,
    });
  });

  it('accepts an empty org.list as a normal ready state', async () => {
    const source = createOrgLiveSourceForPort(fakePort(() => ({ orgs: [], leftOrgs: [], notices: [] })));
    const store = memoryStore(source.initial);
    await expect(source.createGateway(store).refresh(null)).resolves.toMatchObject({ kind: 'ok' });
    expect(store.get().status).toBe('ready');
    expect(store.get().orgs).toEqual([]);
    expect(store.get().currentOrgId).toBeNull();
  });

  it('rejects retired kind="personal" rows from org.list', async () => {
    const rows = {
      orgs: [
        { org: 'personal-max', spaceId: 'space-max', role: 'owner', devices: 1, state: 'active', kind: 'personal', current: true },
      ],
      leftOrgs: [],
    };
    const source = createOrgLiveSourceForPort(fakePort(() => rows));
    const store = memoryStore(source.initial);
    const result = await source.createGateway(store).refresh(null);
    expect(result.kind).toBe('failed');
    expect(store.get().status).toBe('error');
    expect(store.get().orgs).toBeNull();
  });

  it('keeps the previous current org while it is still listed', async () => {
    const source = createOrgLiveSourceForPort(fakePort(() => LIST_RESULT));
    const store = memoryStore({ ...source.initial, currentOrgId: 'group-acme-lab' });
    await source.createGateway(store).refresh(null);
    expect(store.get().currentOrgId).toBe('group-acme-lab');
  });

  it('uses daemon current only for the initial browser selection', async () => {
    const rows = {
      orgs: [
        { org: 'group-team', spaceId: 'space-team', role: 'member', devices: 2, state: 'active', kind: 'work', current: false },
        { org: 'group-acme', spaceId: 'space-acme', role: 'owner', devices: 1, state: 'active', kind: 'work', current: true },
      ],
      leftOrgs: [],
    };
    const source = createOrgLiveSourceForPort(fakePort(() => rows));
    const store = memoryStore(source.initial);
    await source.createGateway(store).refresh(null);
    expect(store.get().orgs?.map((org) => org.kind)).toEqual(['work', 'work']);
    expect(store.get().currentOrgId).toBe('group-acme');

    store.update((snapshot) => ({ ...snapshot, currentOrgId: 'group-team' }));
    await source.createGateway(store).refresh(null);
    expect(store.get().currentOrgId).toBe('group-team');
  });

  it('loads shared Agent organization visibility from targets', async () => {
    const port = fakePort((method) => {
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'targets') return { targets: [{
        targetUri: 'agent:alice:node:a.' + 'a'.repeat(26),
        agentId: 'a.' + 'a'.repeat(26), displayName: 'a', orgs: ['group-hyprial-dev'],
      }] };
      return {};
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    await source.createGateway(store).refresh(null);
    expect(store.get().details['group-hyprial-dev']?.agents?.[0]).toMatchObject({
      id: 'a.' + 'a'.repeat(26), ownerLabel: 'user:alice', orgIds: ['group-hyprial-dev'],
    });
    expect(store.get().agentOrgs?.['a.' + 'a'.repeat(26)]).toEqual(['group-hyprial-dev']);
    expect(store.get().orgAgentDirectory?.['group-hyprial-dev']).toMatchObject([{
      id: 'a.' + 'a'.repeat(26), ownerLabel: 'user:alice',
    }]);
    expect(store.get().orgAgentDirectory?.['group-acme-lab']).toEqual([]);
    expect(port.calls.filter((call) => call.method === 'targets')).toHaveLength(1);
  });

  it('shows Agents owned by all members without individual Agent org memberships', async () => {
    const ids = ['a.' + 'a'.repeat(26), 'a.' + 'b'.repeat(26), 'a.' + 'c'.repeat(26), 'a.' + 'd'.repeat(26)];
    const port = fakePort((method) => {
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'targets') return {
        targets: ids.map((id, index) => ({
          agentId: id,
          targetUri: `agent:${index === 0 ? 'a' : 'b'}@example:node-${index === 0 ? 'a' : 'b'}:${id}`,
          displayName: index === 0 ? 'test-1' : `test-b-${index}`,
          orgs: ['group-hyprial-dev'],
        })),
      };
      return {};
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);

    await source.createGateway(store).refresh(null);

    expect(store.get().orgAgentDirectory?.['group-hyprial-dev']).toHaveLength(4);
    expect(store.get().orgAgentDirectory?.['group-hyprial-dev']?.map((row) => row.name)).toEqual([
      'test-1', 'test-b-1', 'test-b-2', 'test-b-3',
    ]);
    expect(port.calls.filter((call) => call.method === 'targets')).toHaveLength(1);
  });

  it('distinguishes a known Agent without org membership from a failed membership read', async () => {
    const id = 'a.' + 'b'.repeat(26);
    const port = fakePort((method) => {
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'targets') return { targets: [{
        targetKind: 'agent', targetUri: 'agent:bob:node:' + id, actor: 'agent:bob:node:' + id,
        status: 'offline', deliverable: true, hosted: false, hostedBy: null, agentId: id, displayName: 'b',
      }] };
      return {};
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    await source.createGateway(store).refresh(null);
    expect(store.get().details['group-hyprial-dev']?.agents).toEqual([]);
    expect(store.get().agentOrgs?.[id]).toEqual([]);
    expect(port.calls.filter((call) => call.method === 'agent.list')).toEqual([]);
  });

  it('does not offer explicit Agent organization writes in the live gateway', async () => {
    const source = createOrgLiveSourceForPort(fakePort(() => LIST_RESULT));
    const gateway = source.createGateway(memoryStore(source.initial));
    expect(gateway.joinAgentOrg).toBeUndefined();
    expect(gateway.leaveAgentOrg).toBeUndefined();
  });

  it('reports a failed org.list without claiming success', async () => {
    const port = fakePort(() => {
      throw new DomainError('ORG_NETWORK_ERROR', 'boom', null, DIAGNOSTICS);
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const result = await source.createGateway(store).refresh(null);
    expect(result).toMatchObject({ kind: 'failed', code: 'ORG_NETWORK_ERROR' });
    expect(store.get().status).toBe('error');
    expect(store.get().readError).toBe('组织网络请求失败。');
    expect(store.get().orgs).toBeNull();
  });

  it('marks loading before org.list and keeps the read error after a retry recovers', async () => {
    let attempt = 0;
    const port = fakePort(() => {
      attempt += 1;
      if (attempt === 1) {
        throw new DomainError('ORG_NETWORK_ERROR', 'boom', null, diagnostics('dispatched', 'org.list'));
      }
      return LIST_RESULT;
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const gateway = source.createGateway(store);

    const pending = gateway.refresh(null);
    expect(store.get().status).toBe('loading');
    expect(store.get().readError).toBeNull();
    await expect(pending).resolves.toMatchObject({ kind: 'failed', code: 'ORG_NETWORK_ERROR' });
    expect(store.get()).toMatchObject({
      status: 'error',
      readError: '组织网络请求失败。',
      orgs: null,
    });

    await expect(gateway.refresh(null)).resolves.toMatchObject({ kind: 'ok' });
    expect(store.get()).toMatchObject({ status: 'ready', readError: null });
    expect(store.get().orgs?.[0].id).toBe('group-hyprial-dev');
    expect(port.calls.map((call) => call.method)).toEqual([
      'org.list',
      'org.list',
      ...DETAIL_METHODS,
    ]);
    expect(port.calls.some((call) => call.method === 'org.fetch')).toBe(false);
  });

  it('keeps an unreadable read response as a readable error with its code', async () => {
    const port = fakePort(() => {
      throw new EnvelopeError(
        'ENVELOPE_NOT_JSON',
        'response body is not JSON',
        diagnostics('unknown', 'org.list'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const result = await source.createGateway(store).refresh(null);
    expect(result).toMatchObject({ kind: 'failed', code: 'ENVELOPE_NOT_JSON' });
    expect(store.get()).toMatchObject({
      status: 'error',
      readError: ORG_COPY.responseUnreadable,
    });
  });

  it('syncContext calls org.fetch once and returns counts and candidates', async () => {
    const port = fakePort(() => ({
      requestedSources: ['node-2', 'node-3'],
      responseCount: 2,
      receivedCount: 1,
      rejectedCount: 1,
      candidates: [
        {
          path: '/tmp/example/pending.json',
          version: 7,
          publisher: 'user:lin',
          receivedAt: '2026-11-02T09:30:00Z',
          source: 'node-2',
        },
      ],
    }));
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).syncContext();
    expect(result).toMatchObject({
      kind: 'ok',
      value: { responseCount: 2, receivedCount: 1, rejectedCount: 1 },
    });
    expect(port.calls).toHaveLength(1);
    expect(port.calls[0].method).toBe('org.fetch');
  });

  it('shows an unknown outcome on timeout and never retries', async () => {
    const port = fakePort(() => {
      throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timeout', DIAGNOSTICS);
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).syncContext();
    expect(result).toEqual({
      kind: 'failed',
      code: 'REQUEST_TIMEOUT',
      message: ORG_COPY.syncConfigUnknown,
      unknown: true,
    });
    expect(port.calls).toHaveLength(1);
  });

  it('retries org.fetch only on an explicit user call, never automatically', async () => {
    let calls = 0;
    const port = fakePort(() => {
      calls += 1;
      if (calls === 1) {
        throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timeout', DIAGNOSTICS);
      }
      return {
        requestedSources: ['node-2'],
        responseCount: 1,
        receivedCount: 1,
        rejectedCount: 0,
        candidates: [],
      };
    });
    const source = createOrgLiveSourceForPort(port);
    const gateway = source.createGateway(memoryStore(source.initial));
    await expect(gateway.syncContext()).resolves.toMatchObject({
      kind: 'failed',
      code: 'REQUEST_TIMEOUT',
    });
    expect(port.calls).toHaveLength(1);
    await expect(gateway.syncContext()).resolves.toMatchObject({ kind: 'ok' });
    expect(port.calls).toHaveLength(2);
  });

  it('reports an unknown result with ENVELOPE_NOT_JSON after a dispatched fetch', async () => {
    const port = fakePort(() => {
      throw new EnvelopeError(
        'ENVELOPE_NOT_JSON',
        'response body is not JSON; dispatch cannot be determined',
        diagnostics('unknown'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).syncContext();
    expect(result).toEqual({
      kind: 'failed',
      code: 'ENVELOPE_NOT_JSON',
      message: ORG_COPY.syncConfigUnknown,
      unknown: true,
    });
    expect(port.calls).toHaveLength(1);
  });

  it('reports an unknown result with the HTTP code after a dispatched fetch', async () => {
    const port = fakePort(() => {
      throw new HttpError(
        'HTTP_UNAUTHORIZED',
        'the request entry refused the bearer credential',
        401,
        diagnostics('unknown'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).syncContext();
    expect(result).toEqual({
      kind: 'failed',
      code: 'HTTP_UNAUTHORIZED',
      message: ORG_COPY.syncConfigUnknown,
      unknown: true,
    });
    expect(port.calls).toHaveLength(1);
  });

  it('keeps a not-dispatched failure as a plain failure with its code', async () => {
    const port = fakePort(() => {
      throw new DomainError(
        'ORG_SOURCE_UNREACHABLE',
        'not online',
        { availableSources: ['node-2'] },
        diagnostics('not-dispatched'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).syncContext();
    expect(result).toEqual({
      kind: 'failed',
      code: 'ORG_SOURCE_UNREACHABLE',
      message: ORG_COPY.syncConfigFailed('选定的来源不可达。'),
    });
    expect(result).not.toMatchObject({ message: ORG_COPY.syncConfigUnknown });
    expect(port.calls).toHaveLength(1);
  });

  it('preserves the declared domain code on a not-dispatched fetch rejection', async () => {
    const port = fakePort(() => {
      throw new DomainError(
        'ORG_SOURCE_UNREACHABLE',
        'not online',
        { availableSources: ['node-2'] },
        diagnostics('not-dispatched'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).syncContext();
    expect(result).toMatchObject({
      kind: 'failed',
      code: 'ORG_SOURCE_UNREACHABLE',
      message: ORG_COPY.syncConfigFailed('选定的来源不可达。'),
    });
  });
});

describe('org page first read failure', () => {
  it('shows the reason, refreshes from the page and never calls org.fetch', async () => {
    let attempt = 0;
    const port = fakePort(() => {
      attempt += 1;
      if (attempt === 1) {
        throw new DomainError('ORG_NETWORK_ERROR', 'boom', null, diagnostics('dispatched', 'org.list'));
      }
      return LIST_RESULT;
    });
    const source = createOrgLiveSourceForPort(port);
    const user = userEvent.setup();
    render(
      <OrgDomainProvider source={source}>
        <FixtureApp dataMode="fixture" firstRunMode="complete" />
      </OrgDomainProvider>,
    );

    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.readFailedTitle)).toBeTruthy();
    expect(screen.getByText('组织网络请求失败。')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: ORG_COPY.refresh }));
    expect(
      await screen.findByRole('heading', { name: 'hyprial-dev' }),
    ).toBeTruthy();
    expect(port.calls.map((call) => call.method)).toEqual([
      'org.list',
      'org.list',
      ...DETAIL_METHODS,
    ]);
    expect(port.calls.some((call) => call.method === 'org.fetch')).toBe(false);
  });

  it('clears the refresh failure notice after a later successful refresh', async () => {
    let attempt = 0;
    const port = fakePort(() => {
      attempt += 1;
      if (attempt <= 2) {
        throw new DomainError('ORG_NETWORK_ERROR', 'boom', null, diagnostics('dispatched', 'org.list'));
      }
      return LIST_RESULT;
    });
    const source = createOrgLiveSourceForPort(port);
    const user = userEvent.setup();
    render(
      <OrgDomainProvider source={source}>
        <FixtureApp dataMode="fixture" firstRunMode="complete" />
      </OrgDomainProvider>,
    );

    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.readFailedTitle)).toBeTruthy();

    const refreshNotice = ORG_COPY.refreshFailed('组织网络请求失败。');
    await user.click(screen.getByRole('button', { name: ORG_COPY.refresh }));
    expect(await screen.findByText(refreshNotice)).toBeTruthy();

    await user.click(screen.getByRole('button', { name: ORG_COPY.refresh }));
    expect(await screen.findByRole('heading', { name: 'hyprial-dev' })).toBeTruthy();
    expect(screen.queryByText(refreshNotice)).toBeNull();
  });
});

describe('org page without a capability', () => {
  it('has no organization entry and never issues a request', async () => {
    render(<FixtureApp dataMode="fixture" firstRunMode="complete" />);
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
    expect(document.querySelector('[data-org-mode]')).toBeNull();
    await expect(
      UNWIRED_SOURCE.createGateway({ get: () => UNWIRED_SOURCE.initial, update: () => undefined }).syncContext(),
    ).resolves.toEqual({ kind: 'unavailable' });
  });
});
