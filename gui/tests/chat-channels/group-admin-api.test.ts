/**
 * Decision record (group chat admin API & controller tests, slice GC5-3, 2026-11-21):
 * - Verifies `createGroupAdminApiFromClient`, `GroupAdminController`, and
 *   `buildGroupMemberCandidates` for Slice 3: one-action-one-request budget,
 *   43-char idempotency keys, same-key retry after unknown/unavailable outcomes,
 *   D4 top-list + highlight state, refusal classification, and Contract Mock
 *   empirical behavior.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import { connectBrowserBackend } from '../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { DomainError, HttpError, MethodRefusedError, TransportError } from '../../src/client';
import { createGeneratedClient } from '../../src/client/generated';
import {
  GroupAdminController,
  GroupResponseMismatchError,
  buildGroupMemberCandidates,
  createGroupAdminApiFromClient,
  type GroupAdminApi,
  type GroupDetail,
} from '../../src/features/chat-channels';
import {
  MOCK_ORIGIN,
  createContractMock,
  createContractMockFetch,
  projectContract,
  withDigest,
} from '../../contract-mock/index.ts';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const REVIEWER_ID = 'a.revieweraaaaaaaaaaaaaaaaaa';

const CONTRACT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../contract/daemon-request');
const contractFile = (name: string) => JSON.parse(readFileSync(resolve(CONTRACT, name), 'utf8'));

function makeDetail(overrides: Partial<GroupDetail> = {}): GroupDetail {
  return {
    conversationId: GROUP_ID,
    title: 'release',
    createdBy: OWNER,
    createdAtMs: 1790000120000,
    members: [
      { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 1790000120000, addedBy: OWNER },
      { memberId: WORKER_ID, kind: 'agent', displayName: 'worker', manager: false, joinedAtMs: 1790000120001, addedBy: OWNER },
    ],
    self: OWNER,
    canManageMembers: true,
    ...overrides,
  };
}

describe('createGroupAdminApiFromClient', () => {
  it('validates create, get, add, remove responses and rejects malformed payloads', async () => {
    const client = createGeneratedClient({
      async invoke<T>(method: string): Promise<T> {
        if (method === 'conversations.create') {
          return {
            ok: true,
            kind: 'group',
            conversationId: GROUP_ID,
            title: 'release',
            createdBy: OWNER,
            createdAtMs: 1000,
            members: [
              { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 1000, addedBy: OWNER },
            ],
          } as T;
        }
        if (method === 'conversations.get') {
          return {
            ok: true,
            kind: 'group',
            conversationId: 'g.other',
            title: 'other',
            createdBy: OWNER,
            createdAtMs: 1000,
            members: [],
            self: OWNER,
            canManageMembers: true,
          } as T;
        }
        throw new Error(`unexpected ${method}`);
      },
    });
    const api = createGroupAdminApiFromClient(client);
    const created = await api.createGroup({ title: ' release ', members: [WORKER_ID], idempotencyKey: 'k'.repeat(43) });
    expect(created.conversationId).toBe(GROUP_ID);
    await expect(api.getGroup(GROUP_ID)).rejects.toBeInstanceOf(GroupResponseMismatchError);
  });

  it('throws MethodRefusedError before dispatch when catalog refuses a method', async () => {
    const invoke = vi.fn();
    const client = createGeneratedClient({ invoke });
    const api = createGroupAdminApiFromClient(client, (method) => ({
      available: method !== 'conversations.create',
      code: 'METHOD_NOT_CALLABLE',
    }));
    await expect(api.createGroup({ title: 't', members: [], idempotencyKey: 'k'.repeat(43) }))
      .rejects.toBeInstanceOf(MethodRefusedError);
    expect(invoke).not.toHaveBeenCalled();
  });
});

describe('GroupAdminController', () => {
  it('issues exactly one port call per action and updates members from mutation response without re-reading getGroup', async () => {
    const counts = { create: 0, get: 0, add: 0, remove: 0 };
    const memberCallbacks: Array<[string, number]> = [];
    const api: GroupAdminApi = {
      createGroup: async ({ title }) => {
        counts.create += 1;
        return makeDetail({ title });
      },
      getGroup: async () => {
        counts.get += 1;
        return makeDetail();
      },
      addMember: async () => {
        counts.add += 1;
        return {
          conversationId: GROUP_ID,
          entryId: 'e-add',
          members: [
            ...makeDetail().members,
            { memberId: REVIEWER_ID, kind: 'agent', displayName: 'reviewer', manager: false, joinedAtMs: 2000, addedBy: OWNER },
          ],
        };
      },
      removeMember: async () => {
        counts.remove += 1;
        return {
          conversationId: GROUP_ID,
          entryId: 'e-rem',
          members: [makeDetail().members[0]],
        };
      },
    };
    const controller = new GroupAdminController({
      api,
      onGroupMembersUpdated: (groupId, members) => memberCallbacks.push([groupId, members.length]),
    });

    const created = await controller.createGroup('release', [WORKER_ID]);
    expect(created?.conversationId).toBe(GROUP_ID);
    expect(counts).toEqual({ create: 1, get: 0, add: 0, remove: 0 });
    expect(controller.getSnapshot().createdGroups[0]).toMatchObject({
      conversationId: GROUP_ID,
      title: 'release',
      memberCount: 2,
    });
    expect(controller.getSnapshot().highlightedGroupId).toBe(GROUP_ID);

    await controller.loadMembers(GROUP_ID);
    expect(counts).toEqual({ create: 1, get: 1, add: 0, remove: 0 });

    await controller.addMember(GROUP_ID, REVIEWER_ID);
    expect(counts).toEqual({ create: 1, get: 1, add: 1, remove: 0 });
    expect(controller.getSnapshot().groups[GROUP_ID]).toMatchObject({
      status: 'ready',
      detail: { members: expect.arrayContaining([expect.objectContaining({ memberId: REVIEWER_ID })]) },
    });

    await controller.removeMember(GROUP_ID, WORKER_ID);
    expect(counts).toEqual({ create: 1, get: 1, add: 1, remove: 1 });
    expect(controller.getSnapshot().groups[GROUP_ID].detail?.members).toHaveLength(1);
    expect(memberCallbacks).toEqual([[GROUP_ID, 2], [GROUP_ID, 2], [GROUP_ID, 3], [GROUP_ID, 1]]);

    controller.clearHighlight(GROUP_ID);
    expect(controller.getSnapshot().highlightedGroupId).toBeNull();
  });

  it('reuses the exact same idempotencyKey on retry after unknown outcome for create, add, and remove', async () => {
    const createKeys: string[] = [];
    const addKeys: string[] = [];
    const removeKeys: string[] = [];
    let failCreate = true;
    let failAdd = true;
    let failRemove = true;
    let seed = 1;

    const controller = new GroupAdminController({
      randomBytes: (count) => new Uint8Array(count).fill(seed++),
      api: {
        createGroup: async ({ idempotencyKey }) => {
          createKeys.push(idempotencyKey);
          if (failCreate) {
            throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timed out', {
              method: 'conversations.create',
              requestId: null,
              status: null,
              dispatch: 'unknown',
              catalogDigest: null,
              attempts: 1,
              startedAt: 1,
              durationMs: 1,
            });
          }
          return makeDetail();
        },
        getGroup: async () => makeDetail(),
        addMember: async ({ idempotencyKey }) => {
          addKeys.push(idempotencyKey);
          if (failAdd) {
            throw new HttpError('DAEMON_UNAVAILABLE', 'unavailable', 503, {
              method: 'conversations.members.add',
              requestId: 'r-add',
              status: 503,
              dispatch: 'dispatched',
              catalogDigest: 'd',
              attempts: 1,
              startedAt: 1,
              durationMs: 1,
            });
          }
          return { conversationId: GROUP_ID, entryId: 'e-1', members: makeDetail().members };
        },
        removeMember: async ({ idempotencyKey }) => {
          removeKeys.push(idempotencyKey);
          if (failRemove) {
            throw new GroupResponseMismatchError('mismatch');
          }
          return { conversationId: GROUP_ID, entryId: 'e-2', members: [makeDetail().members[0]] };
        },
      },
    });

    await controller.createGroup('release', [WORKER_ID]);
    expect(controller.getSnapshot().create).toMatchObject({
      status: 'unknown',
      message: '结果未知。',
      idempotencyKey: createKeys[0],
    });
    expect(createKeys[0]).toMatch(/^[A-Za-z0-9_-]{43}$/);

    failCreate = false;
    await controller.retryCreateGroup();
    expect(createKeys).toHaveLength(2);
    expect(createKeys[1]).toBe(createKeys[0]);
    expect(controller.getSnapshot().create.status).toBe('idle');

    await controller.addMember(GROUP_ID, REVIEWER_ID);
    expect(controller.getSnapshot().addMember).toMatchObject({ status: 'unknown', message: '结果未知。' });
    failAdd = false;
    await controller.retryAddMember();
    expect(addKeys).toEqual([addKeys[0], addKeys[0]]);

    await controller.removeMember(GROUP_ID, WORKER_ID);
    expect(controller.getSnapshot().removeMember).toMatchObject({ status: 'unknown', message: '结果未知。' });
    failRemove = false;
    await controller.retryRemoveMember();
    expect(removeKeys).toEqual([removeKeys[0], removeKeys[0]]);

    await controller.createGroup('second', []);
    expect(createKeys).toHaveLength(3);
    expect(createKeys[2]).not.toBe(createKeys[0]);
  });

  it('classifies domain refusals and routes MethodRefusedError to reportUnavailable', async () => {
    const reports: unknown[] = [];
    let mode: 'agent-not-found' | 'unsupported' | 'refused' = 'agent-not-found';
    const controller = new GroupAdminController({
      reportUnavailable: (subject, detail) => reports.push([subject, detail]),
      api: {
        createGroup: async () => {
          if (mode === 'refused') throw new MethodRefusedError('conversations.create', 'METHOD_NOT_FOUND');
          throw new DomainError(
            mode === 'agent-not-found' ? 'AGENT_NOT_FOUND' : 'UNSUPPORTED_TARGET',
            'rejected',
            {},
            {
              method: 'conversations.create',
              requestId: 'req-1',
              status: 400,
              dispatch: 'dispatched',
              catalogDigest: 'd',
              attempts: 1,
              startedAt: 1,
              durationMs: 1,
            },
          );
        },
        getGroup: async () => makeDetail(),
        addMember: async () => ({ conversationId: GROUP_ID, entryId: null, members: [] }),
        removeMember: async () => ({ conversationId: GROUP_ID, entryId: null, members: [] }),
      },
    });

    await controller.createGroup('release', [WORKER_ID]);
    expect(controller.getSnapshot().create).toMatchObject({
      status: 'rejected',
      code: 'AGENT_NOT_FOUND',
    });

    mode = 'unsupported';
    await controller.createGroup('release', [WORKER_ID]);
    expect(controller.getSnapshot().create).toMatchObject({
      status: 'rejected',
      code: 'UNSUPPORTED_TARGET',
    });

    mode = 'refused';
    await controller.createGroup('release', [WORKER_ID]);
    expect(controller.getSnapshot().create.status).toBe('idle');
    expect(reports).toEqual([['新建群聊', { code: 'METHOD_NOT_FOUND', source: 'conversations.create' }]]);
  });

  it('computes candidate intersection of targets with agentId and local agent directory', () => {
    const candidates = buildGroupMemberCandidates(
      [
        { id: 'agent:alice:node:worker', kind: 'agent', agentId: WORKER_ID, displayName: 'worker' },
        { id: 'agent:alice:node:reviewer', kind: 'agent', agentId: REVIEWER_ID, displayName: 'reviewer' },
        { id: 'agent:alice:node:legacy', kind: 'agent', agentId: null, displayName: 'legacy' },
        { id: 'agent:bob:node:a.peer', kind: 'agent', agentId: 'a.peer', displayName: 'remote-peer' },
      ],
      [
        { uri: 'agent:alice:node:worker', agentId: WORKER_ID, displayName: 'worker' },
        { uri: 'agent:alice:node:reviewer', agentId: REVIEWER_ID, displayName: 'reviewer' },
        { uri: 'agent:alice:node:legacy', agentId: null, displayName: 'legacy' },
      ],
      [REVIEWER_ID],
    );
    expect(candidates).toEqual([
      { memberId: WORKER_ID, displayName: 'worker', uri: 'agent:alice:node:worker' },
    ]);
  });

  it('exercises group admin endpoints against Contract Mock', async () => {
    const table = withDigest(projectContract(contractFile('openapi.json'), contractFile('http-envelope.schema.json')));
    const mock = createContractMock(table, { preset: 'success' });
    const memory = new Map<string, string>();
    const bootstrap = await connectBrowserBackend({
      fetch: createContractMockFetch(mock),
      storage: {
        getItem: (key) => memory.get(key) ?? null,
        setItem: (key, value) => { memory.set(key, value); },
        removeItem: (key) => { memory.delete(key); },
      },
      location: { origin: MOCK_ORIGIN, hash: '#grant=contract-mock-launch', pathname: '/', search: '' },
      history: { replaceState() {} },
    });
    const capabilities = createConnectedGuiCapabilities(bootstrap.connection);
    expect(capabilities.groupAdmin).toBeDefined();

    const got = await capabilities.groupAdmin!.getGroup(GROUP_ID);
    expect(got.title).toBe('release');
    expect(got.canManageMembers).toBe(true);
    expect(got.members.map((m) => m.memberId)).toEqual([OWNER, WORKER_ID, REVIEWER_ID]);

    const added = await capabilities.groupAdmin!.addMember({
      conversationId: GROUP_ID,
      member: REVIEWER_ID,
      idempotencyKey: 'a'.repeat(43),
    });
    expect(added.members).toHaveLength(3);

    const removedReviewer = await capabilities.groupAdmin!.removeMember({
      conversationId: GROUP_ID,
      memberId: REVIEWER_ID,
      idempotencyKey: 'r'.repeat(43),
    });
    expect(removedReviewer.members.map((m) => m.memberId)).toEqual([OWNER, WORKER_ID]);

    const removedWorker = await capabilities.groupAdmin!.removeMember({
      conversationId: GROUP_ID,
      memberId: WORKER_ID,
      idempotencyKey: 'w'.repeat(43),
    });
    expect(removedWorker.members.map((m) => m.memberId)).toEqual([OWNER, REVIEWER_ID]);

    const created = await capabilities.groupAdmin!.createGroup({
      title: 'release',
      members: [WORKER_ID, REVIEWER_ID],
      idempotencyKey: 'c'.repeat(43),
    });
    expect(created.conversationId).toBe(GROUP_ID);

    await expect(capabilities.groupAdmin!.createGroup({
      title: 'custom-group',
      members: [WORKER_ID],
      idempotencyKey: 'c'.repeat(43),
    })).rejects.toMatchObject({ code: 'MOCK_EXAMPLE_MISSING' });
  });
});
