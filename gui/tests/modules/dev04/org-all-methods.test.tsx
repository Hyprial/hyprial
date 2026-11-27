import { describe, expect, it } from 'vitest';
import type { OrgDomainSnapshot } from '../../../src/features/org/impl/domain/types';
import { createOrgLiveSourceForPort } from '../../support/org-backend';
import type { OrgStore } from '../../../src/features/org/impl/domain/context';
import type { OrgRequestPort } from '../../../src/features/org/impl/api/org-request-api';

const ORG = 'group-x';
const SPACE = 'space-x';
// Opaque base64 version as the real daemon emits it; parseInt would collapse
// it to 0, so this catches accidental numeric comparison in the adapter.
const V3 = 'dGVzdC12ZXJzaW9uLTM=';
const V4 = 'dGVzdC12ZXJzaW9uLTQ=';

const node = (version: string) => ({
  node_id: 'n1',
  path: 'a.md',
  name: 'a.md',
  kind: 'doc',
  modified_by: 'user:me',
  modified_via: null,
  blob_hash: null,
  contentState: 'arrived' as const,
  deleted: false,
  doc_id: 'doc-1',
  name_conflict: false,
  size: 3,
  space_id: SPACE,
  uri: 'orgfs:owner:space-x:n1',
  version,
});

const trashNode = (
  version: string,
  nodeId: string,
  path: string,
  kind: 'dir' | 'doc',
) => ({
  ...node(version),
  node_id: nodeId,
  path,
  name: path.split('/').at(-1) ?? path,
  kind,
  deleted: true,
  doc_id: kind === 'doc' ? `doc-${nodeId}` : null,
  uri: `orgfs:owner:space-x:${nodeId}`,
});

const REMOVAL = (confirmed: boolean) => ({
  org: ORG,
  spaceId: SPACE,
  operationId: 'op-1',
  deviceIds: ['dev-1'],
  expectedRevision: 4,
  tailnetId: 'tailnet-1',
  authorityDeviceId: 'dev-1',
  phase: confirmed ? 'complete' : 'pending-confirmation',
  confirmed,
});

function fakePort(
  overrides: Record<string, unknown | ((params: Record<string, unknown>) => unknown)>,
) {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const base: Record<string, unknown> = {
    'org.list': {
      orgs: [{ org: ORG, spaceId: SPACE, role: 'owner', devices: 1, state: 'active' }],
      leftOrgs: [],
    },
    'org.network': {
      orgs: [ORG],
      unavailableOrgs: [],
      dataProblems: [],
      directory: {
        [ORG]: [
          {
            deviceId: 'dev-1',
            owner: 'user:me',
            presence: 'online',
            updatedAt: '2026-11-02T10:00:00Z',
          },
        ],
      },
      self: { deviceId: 'dev-1', owner: 'user:me' },
    },
    hosts: {
      hosts: [
        {
          nodeId: 'dev-1',
          status: 'online',
          observedAtMs: null,
          ageMs: null,
          stale: false,
          lastError: null,
        },
      ],
    },
    'orgfs.spaces': {
      spaces: [
        {
          space_id: SPACE,
          name: 'team',
          owner: 'user:me',
          created_at: '2026-11-02T10:00:00Z',
        },
      ],
    },
    'orgfs.status': {
      space_id: SPACE,
      synced: true,
      holders_online: ['dev-1'],
      content_pending: 0,
      rejected_updates: 0,
    },
    'orgfs.ls': { nodes: [node(V3)] },
    'orgfs.read': { text: 'hello', node: node(V3), version: V3 },
    'orgfs.trash': { nodes: [] },
  };
  return {
    calls,
    async request(method: string, params: Record<string, unknown>) {
      calls.push({ method, params });
      const value = overrides[method] ?? base[method];
      if (typeof value === 'function') return value(params);
      if (value === undefined) throw new Error(`unexpected method ${method}`);
      return value;
    },
  };
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

function gatewayFor(port: OrgRequestPort) {
  const source = createOrgLiveSourceForPort(port);
  return source.createGateway(memoryStore(source.initial));
}

function storeAndGateway(port: OrgRequestPort) {
  const source = createOrgLiveSourceForPort(port);
  const store = memoryStore(source.initial);
  return { gateway: source.createGateway(store), store };
}

describe('org all-method gateway', () => {
  it('previews leave and confirms with the exact operation/device set', async () => {
    const port = fakePort({
      'org.leave': (params: Record<string, unknown>) => REMOVAL(params.confirmed === true),
    });
    const gateway = gatewayFor(port);
    const preview = await gateway.previewLeave(ORG);
    expect(preview).toMatchObject({
      kind: 'ok',
      value: { revision: 4, devices: ['dev-1'], tailnet: 'tailnet-1' },
    });
    if (preview.kind !== 'ok') throw new Error('preview failed');
    const result = await gateway.leaveOrg(ORG, preview.value.revision);
    expect(result).toEqual({ kind: 'ok', value: undefined });
    expect(port.calls.filter((call) => call.method === 'org.leave')).toEqual([
      { method: 'org.leave', params: { org: ORG } },
      {
        method: 'org.leave',
        params: {
          org: ORG,
          confirmed: true,
          operationId: 'op-1',
          deviceIds: ['dev-1'],
        },
      },
    ]);
  });

  it('refuses a stale removal revision without calling the daemon', async () => {
    const port = fakePort({ 'org.remove': () => REMOVAL(false) });
    const gateway = gatewayFor(port);
    await gateway.previewRemoveMember(ORG, 'user:bob');
    const result = await gateway.removeMember(ORG, 'user:bob', 99);
    expect(result).toMatchObject({ kind: 'failed', code: 'stale' });
    expect(port.calls.filter((call) => call.method === 'org.remove')).toHaveLength(1);
  });

  it('requires a fresh preview after a daemon revision conflict without retrying confirmation', async () => {
    let revision = 3;
    let confirmations = 0;
    const port = fakePort({
      'org.leave': (params: Record<string, unknown>) => {
        if (params.confirmed !== true) return { ...REMOVAL(false), expectedRevision: revision };
        confirmations += 1;
        if (confirmations === 1) {
          revision = 4;
          throw Object.assign(new Error('the daemon refused the request'), {
            code: 'REVISION_CONFLICT',
          });
        }
        return { ...REMOVAL(true), expectedRevision: revision };
      },
    });
    const gateway = gatewayFor(port);
    const first = await gateway.previewLeave(ORG);
    if (first.kind !== 'ok') throw new Error('first preview failed');
    await expect(gateway.leaveOrg(ORG, first.value.revision)).resolves.toMatchObject({
      kind: 'failed',
      code: 'stale',
    });
    expect(confirmations).toBe(1);
    const second = await gateway.previewLeave(ORG);
    if (second.kind !== 'ok') throw new Error('second preview failed');
    expect(second.value.revision).toBe(4);
    await expect(gateway.leaveOrg(ORG, second.value.revision)).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(confirmations).toBe(2);
  });

  it('maps repair preview and apply to org.repair', async () => {
    const port = fakePort({
      'org.repair': (params: Record<string, unknown>) =>
        params.apply === true
          ? { apply: true, candidates: [], repaired: [] }
          : {
              apply: false,
              candidates: [
                { org: ORG, reason: 'missing-space', repairable: true, spaceId: SPACE },
              ],
              repaired: [],
            },
    });
    const gateway = gatewayFor(port);
    await expect(gateway.previewRepair(ORG)).resolves.toEqual({
      kind: 'ok',
      value: [{ org: ORG, reason: 'missing-space', repairable: true }],
    });
    // The daemon repaired nothing (repaired=[]); the gateway must preserve
    // that result instead of reporting a blanket success.
    await expect(gateway.repairOrg(ORG)).resolves.toEqual({ kind: 'ok', value: [] });
    expect(port.calls.filter((call) => call.method === 'org.repair')).toEqual([
      { method: 'org.repair', params: { apply: false, org: ORG } },
      { method: 'org.repair', params: { apply: true, org: ORG } },
    ]);
  });

  it('publishes local records for retryMachine and refreshes', async () => {
    const port = fakePort({
      'org.publish': { published: true, orgfsPublished: true },
    });
    const gateway = gatewayFor(port);
    await expect(gateway.retryMachine(ORG, 'dev-1')).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(port.calls.map((call) => call.method)[0]).toBe('org.publish');
    expect(port.calls.some((call) => call.method === 'org.list')).toBe(true);
  });

  it('loads text content with the file listing and binds the opaque version', async () => {
    const port = fakePort({});
    const { gateway, store } = storeAndGateway(port);
    await expect(gateway.refresh(ORG)).resolves.toMatchObject({ kind: 'ok' });
    expect(store.get().details[ORG]?.files[SPACE]?.[0]).toMatchObject({
      path: 'a.md',
      content: 'hello',
      version: 0,
      versionKey: V3,
    });
  });

  it('saves with the exact opaque version bound to the loaded content', async () => {
    const port = fakePort({
      'orgfs.write': { ...node(V4), deleted: false },
    });
    const gateway = gatewayFor(port);
    await gateway.refresh(ORG);
    await expect(gateway.saveFile(ORG, SPACE, 'a.md', 'hello', 0, V3)).resolves.toEqual({
      kind: 'ok',
      value: { version: 0, versionKey: V4 },
    });
    expect(port.calls).toContainEqual({
      method: 'orgfs.write',
      params: { spaceId: SPACE, path: 'a.md', text: 'hello', baseVersion: V3 },
    });
  });

  it('refuses a save whose opaque version no longer matches the content', async () => {
    const port = fakePort({});
    const gateway = gatewayFor(port);
    await gateway.refresh(ORG);
    await expect(gateway.saveFile(ORG, SPACE, 'a.md', 'hello', 0, V4)).resolves.toMatchObject({
      kind: 'failed',
      code: 'ORG_VERSION_STALE',
    });
    expect(port.calls.some((call) => call.method === 'orgfs.write')).toBe(false);
  });

  it('creates a new file with createOnly so an existing body is never cleared', async () => {
    const port = fakePort({
      'orgfs.write': (params: Record<string, unknown>) => {
        expect(params).toMatchObject({ createOnly: true, text: '' });
        return { ...node(V4), deleted: false };
      },
    });
    const gateway = gatewayFor(port);
    await expect(gateway.createEntry(ORG, SPACE, 'new.md', 'file')).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(port.calls).toContainEqual({
      method: 'orgfs.write',
      params: { spaceId: SPACE, path: 'new.md', text: '', createOnly: true },
    });
  });

  it('restores a trash file by node only, without stat or a version', async () => {
    const version = 'dGVzdC12ZXJzaW9uLTc=';
    const port = fakePort({
      'orgfs.trash': { nodes: [{ ...node(version), deleted: true }] },
      'orgfs.restore': node('dGVzdC12ZXJzaW9uLTg='),
    });
    const { gateway } = storeAndGateway(port);
    await gateway.refresh(ORG);
    await expect(gateway.restoreEntry(ORG, SPACE, 'n1')).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(port.calls.some((call) => call.method === 'orgfs.stat')).toBe(false);
    expect(port.calls.filter((call) => call.method === 'orgfs.restore')).toEqual([
      { method: 'orgfs.restore', params: { spaceId: SPACE, node: 'id:n1' } },
    ]);
  });

  it('restores a folder with one orgfs.restore of the folder node; the daemon picks the children', async () => {
    const post = 'post-delete';
    const folder = trashNode(post, 'd1', 'dir', 'dir');
    const child = trashNode(post, 'c1', 'dir/keep.md', 'doc');
    const earlier = trashNode('earlier-delete', 'e1', 'dir/early.md', 'doc');
    const port = fakePort({
      'orgfs.trash': { nodes: [folder, child, earlier] },
      'orgfs.restore': { ...folder, deleted: false, version: 'after-restore' },
    });
    const { gateway } = storeAndGateway(port);
    await gateway.refresh(ORG);
    await expect(gateway.restoreEntry(ORG, SPACE, 'd1')).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(port.calls.some((call) => call.method === 'orgfs.stat')).toBe(false);
    expect(port.calls.filter((call) => call.method === 'orgfs.restore')).toEqual([
      { method: 'orgfs.restore', params: { spaceId: SPACE, node: 'id:d1' } },
    ]);
  });

  it('does not call orgfs.restore for a trash id it has not listed', async () => {
    const port = fakePort({});
    const { gateway } = storeAndGateway(port);
    await gateway.refresh(ORG);
    await expect(gateway.restoreEntry(ORG, SPACE, 'gone')).resolves.toMatchObject({
      kind: 'failed',
      code: 'ORG_TRASH_UNKNOWN',
    });
    expect(port.calls.some((call) => call.method === 'orgfs.restore')).toBe(false);
  });

  it('does not turn a failed file listing into an empty space', async () => {
    const port = fakePort({
      'orgfs.ls': () => {
        throw new Error('boom');
      },
    });
    const { gateway, store } = storeAndGateway(port);
    await gateway.refresh(ORG);
    expect(store.get().details[ORG]?.filesStatus).toBe('error');
    expect(store.get().details[ORG]?.spaces?.[0].health).toBe('unknown');
  });

  it('loads a switched org detail without changing the current org', async () => {
    const BETA = 'group-beta';
    const port = fakePort({
      'org.list': {
        orgs: [
          { org: ORG, spaceId: SPACE, role: 'owner', devices: 1, state: 'active' },
          { org: BETA, spaceId: 'space-beta', role: 'member', devices: 2, state: 'active' },
        ],
        leftOrgs: [],
      },
      'org.network': (params: Record<string, unknown>) => ({
        orgs: [params.org],
        unavailableOrgs: [],
        dataProblems: [],
        directory: {
          [String(params.org)]: [
            {
              deviceId: `dev-${String(params.org)}`,
              owner: 'user:me',
              presence: 'online',
              updatedAt: '2026-11-02T10:00:00Z',
            },
          ],
        },
        self: { deviceId: `dev-${String(params.org)}`, owner: 'user:me' },
      }),
    });
    const { gateway, store } = storeAndGateway(port);
    await gateway.refresh(ORG);
    await expect(gateway.loadOrgDetail(BETA)).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(store.get().currentOrgId).toBe(ORG);
    expect(store.get().details[ORG]).toBeDefined();
    expect(store.get().details[BETA]?.machines?.[0].id).toBe(`dev-${BETA}`);
  });

  it('drops a late switched-org detail read after a newer refresh', async () => {
    const BETA = 'group-beta';
    let releaseOld!: (value: unknown) => void;
    const oldRead = new Promise((resolve) => {
      releaseOld = resolve;
    });
    let betaMode: 'old' | 'new' = 'new';
    const networkFor = (org: string, deviceId: string) => ({
      orgs: [org],
      unavailableOrgs: [],
      dataProblems: [],
      directory: {
        [org]: [
          { deviceId, owner: 'user:me', presence: 'online', updatedAt: '2026-11-02T10:00:00Z' },
        ],
      },
      self: { deviceId, owner: 'user:me' },
    });
    const port = fakePort({
      'org.list': {
        orgs: [
          { org: ORG, spaceId: SPACE, role: 'owner', devices: 1, state: 'active' },
          { org: BETA, spaceId: 'space-beta', role: 'member', devices: 1, state: 'active' },
        ],
        leftOrgs: [],
      },
      'org.network': (params: Record<string, unknown>) => {
        const org = String(params.org);
        if (org === BETA) {
          return betaMode === 'old' ? oldRead : networkFor(BETA, 'dev-beta-new');
        }
        return networkFor(ORG, 'dev-1');
      },
    });
    const { gateway, store } = storeAndGateway(port);
    await gateway.refresh(ORG);
    betaMode = 'old';
    const stale = gateway.loadOrgDetail(BETA);
    betaMode = 'new';
    await expect(gateway.refresh(BETA)).resolves.toMatchObject({ kind: 'ok' });
    expect(store.get().details[BETA]?.machines?.[0].id).toBe('dev-beta-new');
    releaseOld(networkFor(BETA, 'dev-beta-old'));
    await stale;
    expect(store.get().details[BETA]?.machines?.[0].id).toBe('dev-beta-new');
  });

  it('rejects a repaired entry without publish fields', async () => {
    const port = fakePort({
      'org.repair': { apply: true, candidates: [], repaired: [{ org: ORG }] },
    });
    const gateway = gatewayFor(port);
    await expect(gateway.repairOrg(ORG)).resolves.toMatchObject({ kind: 'failed' });
  });

  it('keeps the machine list unknown when org.network fails', async () => {
    const port = fakePort({
      'org.network': () => {
        throw new Error('network read failed');
      },
    });
    const { gateway, store } = storeAndGateway(port);
    await gateway.refresh(ORG);
    expect(store.get().details[ORG]?.machines).toBeNull();
  });

  it('does not expose addLink or revokeLink on the live gateway', () => {
    const port = fakePort({});
    const gateway = gatewayFor(port);
    expect('addLink' in gateway).toBe(false);
    expect('revokeLink' in gateway).toBe(false);
    expect(port.calls).toEqual([]);
  });
});
