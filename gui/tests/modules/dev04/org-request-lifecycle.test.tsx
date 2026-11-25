import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  DomainError,
  TransportError,
  type DispatchState,
  type RequestDiagnostics,
} from '../../../src/client';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { ORG_COPY } from '../../../src/features/org/impl/domain/copy';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import type {
  InviteResult,
  OrgDomainSnapshot,
} from '../../../src/features/org/impl/domain/types';
import { orgIdFromName } from '../../../src/features/org/impl/domain/validation';
import {
  ORG_RESULT_INVALID,
  OrgResultShapeError,
} from '../../../src/features/org/impl/api/org-request-api';
import {
  createOrgLiveSourceForPort,
  createOrgRequestApiForPort,
} from '../../support/org-backend';
import type { OrgStore } from '../../../src/features/org/impl/domain/context';

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

async function caught(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => {
      throw new Error('expected rejection');
    },
    (error: unknown) => error,
  );
}

const CREATE_RESULT = {
  org: 'group-design',
  spaceId: 'space-design',
  aclSpaceId: 'space-design-acl',
  space: {
    spaceId: 'space-design',
    name: 'group-design',
    owner: 'user:me',
    createdAt: '2026-11-02T09:30:00Z',
  },
  aclSpace: {
    spaceId: 'space-design-acl',
    name: 'group-design-acl',
    owner: 'user:me',
    createdAt: '2026-11-02T09:30:00Z',
  },
  existed: false,
  aclExisted: false,
  casdoorGroup: 'not-configured',
  tailnetInitialized: true,
  published: true,
  devicePublished: true,
};

const LIST_AFTER_CREATE = {
  orgs: [
    {
      org: 'group-design',
      spaceId: 'space-design',
      role: 'owner',
      devices: 1,
      state: 'active',
    },
  ],
  leftOrgs: [],
};

const INVITE_RESULT = {
  org: 'group-hyprial-dev',
  spaceId: 'space-team',
  aclSpaceId: 'space-team-acl',
  user: 'user:bob@example',
  link: 'https://hyprial.ai/join#v2.example-only',
  deepLink: 'hyprial://join#v2.example-only',
  command: "hyprial org join 'https://hyprial.ai/join#v2.example-only'",
  invite: 'hyprial-invite:v2:example-only',
  expiresAt: '2026-11-02T09:30:00Z',
  member: { spaceId: 'space-team', user: 'user:bob@example', mode: 'rw' },
  aclMember: { spaceId: 'space-team-acl', user: 'user:bob@example', mode: 'ro' },
};

const JOIN_RESULT = {
  org: 'group-partner-net',
  spaceId: 'space-partner',
  steps: [
    { kind: 'connect-inviter', ok: true, detail: 'tailnet redeemed' },
    { kind: 'join-org', ok: true, detail: 'joined both spaces' },
    { kind: 'register-device', ok: true, detail: 'registered this device' },
    { kind: 'connect-members', ok: true, detail: 'mapped 2 member device(s)' },
  ],
};

const LIST_JOINED = {
  orgs: [
    {
      org: 'group-partner-net',
      spaceId: 'space-partner',
      role: 'member',
      devices: 2,
      state: 'active',
    },
  ],
  leftOrgs: [],
};

const LIST_OWNER = {
  orgs: [
    {
      org: 'group-hyprial-dev',
      spaceId: 'space-team',
      role: 'owner',
      devices: 3,
      state: 'active',
    },
  ],
  leftOrgs: [],
};

const LIST_EMPTY = { orgs: [], leftOrgs: [] };

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

function localClock(iso: string): string {
  const when = new Date(iso);
  return `${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}`;
}

function renderLive(port: ReturnType<typeof fakePort>) {
  const source = createOrgLiveSourceForPort(port);
  const user = userEvent.setup();
  render(
    <OrgDomainProvider source={source}>
      <FixtureApp dataMode="fixture" firstRunMode="complete" />
    </OrgDomainProvider>,
  );
  return { user, source };
}

function callsFor(port: ReturnType<typeof fakePort>, method: string) {
  return port.calls.filter((call) => call.method === method);
}

describe('org lifecycle request adapter', () => {
  it('derives the group- id like the CLI, stripping the prefix only once', () => {
    expect(orgIdFromName('design')).toBe('group-design');
    expect(orgIdFromName('group-design')).toBe('group-design');
    expect(orgIdFromName('group-group-x')).toBe('group-group-x');
  });

  it('create maps both spaces and the publish flags; role and lifecycle stay unconfirmed', async () => {
    const port = fakePort(() => CREATE_RESULT);
    const outcome = await createOrgRequestApiForPort(port).createOrg('group-design');
    expect(port.calls).toEqual([
      { method: 'org.create', params: { org: 'group-design' } },
    ]);
    expect(outcome).toEqual({
      org: {
        id: 'group-design',
        name: 'design',
        kind: 'work',
        role: null,
        lifecycle: 'unknown',
        ownerLabel: null,
        authorityDevice: null,
        deviceCount: null,
      },
      published: true,
      devicePublished: true,
      tailnetInitialized: true,
      existed: false,
    });
  });

  it('create rejects a result without both publish flags', async () => {
    const { published: _published, ...withoutPublished } = CREATE_RESULT;
    const port = fakePort(() => withoutPublished);
    await expect(createOrgRequestApiForPort(port).createOrg('group-design')).rejects.toThrow(
      /格式不正确/,
    );
  });

  it('create preserves the explicit isolated publication state', async () => {
    const port = fakePort(() => ({
      ...CREATE_RESULT,
      tailnetInitialized: false,
      published: null,
      devicePublished: null,
    }));
    const outcome = await createOrgRequestApiForPort(port).createOrg('group-design');
    expect(outcome).toMatchObject({
      tailnetInitialized: false,
      published: null,
      devicePublished: null,
      existed: false,
    });
  });

  it.each([undefined, null, 'false'])('create rejects malformed Tailnet state %s', async (tailnetInitialized) => {
    const port = fakePort(() => ({ ...CREATE_RESULT, tailnetInitialized }));
    await expect(createOrgRequestApiForPort(port).createOrg('group-design')).rejects.toThrow(/格式不正确/);
  });

  it('invite maps the link, command and local expiry with no invented short link', async () => {
    const port = fakePort(() => INVITE_RESULT);
    const result = await createOrgRequestApiForPort(port).invite(
      'group-hyprial-dev',
      'user:bob@example',
    );
    expect(port.calls).toEqual([
      {
        method: 'org.invite',
        params: { org: 'group-hyprial-dev', user: 'user:bob@example' },
      },
    ]);
    expect(result).toEqual({
      link: INVITE_RESULT.link,
      joinCommand: INVITE_RESULT.command,
      expiresAt: localClock(INVITE_RESULT.expiresAt),
      shortLink: null,
    });
  });

  it('invite rejects a result with an invalid member mode or expiry', async () => {
    await expect(
      createOrgRequestApiForPort(
        fakePort(() => ({
          ...INVITE_RESULT,
          member: { ...INVITE_RESULT.member, mode: 'admin' },
        })),
      ).invite('group-hyprial-dev', 'user:bob@example'),
    ).rejects.toThrow(/格式不正确/);
    await expect(
      createOrgRequestApiForPort(
        fakePort(() => ({ ...INVITE_RESULT, expiresAt: 'not-a-time' })),
      ).invite('group-hyprial-dev', 'user:bob@example'),
    ).rejects.toThrow(/格式不正确/);
  });

  it('join maps the four declared steps in execution order', async () => {
    const port = fakePort(() => JOIN_RESULT);
    const outcome = await createOrgRequestApiForPort(port).join('https://hyprial.ai/join#v2.example');
    expect(port.calls).toEqual([
      { method: 'org.join', params: { link: 'https://hyprial.ai/join#v2.example' } },
    ]);
    expect(outcome.steps).toEqual(['connect', 'join', 'register', 'peers']);
    expect(outcome.org).toMatchObject({
      id: 'group-partner-net',
      name: 'partner-net',
      kind: 'work',
      role: null,
      lifecycle: 'unknown',
      deviceCount: null,
    });
  });

  it('join rejects an unknown step or a step that is not ok', async () => {
    await expect(
      createOrgRequestApiForPort(
        fakePort(() => ({
          ...JOIN_RESULT,
          steps: [{ kind: 'delete-org', ok: true, detail: 'x' }],
        })),
      ).join('https://hyprial.ai/join#v2.example'),
    ).rejects.toThrow(/格式不正确/);
    await expect(
      createOrgRequestApiForPort(
        fakePort(() => ({
          ...JOIN_RESULT,
          steps: [{ kind: 'join-org', ok: false, detail: 'x' }],
        })),
      ).join('https://hyprial.ai/join#v2.example'),
    ).rejects.toThrow(/格式不正确/);
  });

  it('marks create/invite/join result-shape failures as unknown with a stable code', async () => {
    const { published: _published, ...withoutPublished } = CREATE_RESULT;
    const cases: Promise<unknown>[] = [
      createOrgRequestApiForPort(fakePort(() => withoutPublished)).createOrg('group-design'),
      createOrgRequestApiForPort(
        fakePort(() => ({
          ...INVITE_RESULT,
          member: { ...INVITE_RESULT.member, mode: 'admin' },
        })),
      ).invite('group-hyprial-dev', 'user:bob@example'),
      createOrgRequestApiForPort(
        fakePort(() => ({
          ...JOIN_RESULT,
          steps: [{ kind: 'not-a-step', ok: true, detail: 'x' }],
        })),
      ).join('https://hyprial.ai/join#v2.example'),
    ];
    for (const promise of cases) {
      const error = await caught(promise);
      expect(error).toBeInstanceOf(OrgResultShapeError);
      expect(error).toMatchObject({
        code: ORG_RESULT_INVALID,
        dispatch: 'unknown',
      });
    }
  });
});

describe('org lifecycle live source', () => {
  it('creates with the CLI group- id and returns the refreshed row', async () => {
    const port = fakePort((method) =>
      method === 'org.create' ? CREATE_RESULT : LIST_AFTER_CREATE,
    );
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const gateway = source.createGateway(store);

    const result = await gateway.createOrg('design');

    expect(port.calls.map((call) => call.method)).toEqual([
      'org.create',
      'org.list',
      ...DETAIL_METHODS,
    ]);
    expect(port.calls[0].params).toEqual({ org: 'group-design' });
    expect(result).toMatchObject({
      kind: 'ok',
      value: {
        id: 'group-design',
        name: 'design',
        role: 'owner',
        lifecycle: 'active',
        deviceCount: 1,
      },
    });
    expect(store.get().orgs?.[0].id).toBe('group-design');
  });

  it('never claims success when the device row was not published', async () => {
    const port = fakePort((method) =>
      method === 'org.create'
        ? { ...CREATE_RESULT, published: false, devicePublished: false }
        : LIST_AFTER_CREATE,
    );
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const result = await source.createGateway(store).createOrg('design');

    expect(result).toEqual({ kind: 'failed', message: ORG_COPY.createNotPublished });
    expect(result).not.toMatchObject({ kind: 'ok' });
    expect(port.calls.map((call) => call.method)).toEqual([
      'org.create',
      'org.list',
      ...DETAIL_METHODS,
    ]);
  });

  it('accepts an isolated local create without claiming publication or resending', async () => {
    const port = fakePort((method) => method === 'org.create'
      ? { ...CREATE_RESULT, tailnetInitialized: false, published: null, devicePublished: null }
      : LIST_AFTER_CREATE);
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);
    const result = await source.createGateway(store).createOrg('design');
    expect(result).toMatchObject({ kind: 'ok', value: { id: 'group-design', role: 'owner' } });
    expect(port.calls.filter((call) => call.method === 'org.create')).toHaveLength(1);
    expect(store.get().orgs?.[0].id).toBe('group-design');
  });

  it.each([
    { tailnetInitialized: true, published: null, devicePublished: null },
    { tailnetInitialized: false, published: true, devicePublished: null },
    { tailnetInitialized: false, published: null, devicePublished: false },
  ])('does not treat an unconfirmed partial publication as isolated success: %s', async (publication) => {
    const port = fakePort((method) => method === 'org.create'
      ? { ...CREATE_RESULT, ...publication }
      : LIST_AFTER_CREATE);
    const source = createOrgLiveSourceForPort(port);
    const result = await source.createGateway(memoryStore(source.initial)).createOrg('design');
    expect(result).toEqual({ kind: 'failed', message: ORG_COPY.createNotPublished });
    expect(port.calls.filter((call) => call.method === 'org.create')).toHaveLength(1);
  });

  it('treats a timed-out create as unknown and never retries', async () => {
    const port = fakePort(() => {
      throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timeout', DIAGNOSTICS);
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source
      .createGateway(memoryStore(source.initial))
      .createOrg('design');
    expect(result).toEqual({
      kind: 'failed',
      code: 'REQUEST_TIMEOUT',
      message: ORG_COPY.createUnknown,
      unknown: true,
    });
    expect(port.calls).toHaveLength(1);
  });

  it('maps a refused create to its code and reason', async () => {
    const port = fakePort(() => {
      throw new DomainError(
        'ORG_ACTION_NOT_AUTHORIZED',
        'not allowed',
        null,
        diagnostics('not-dispatched', 'org.create'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source
      .createGateway(memoryStore(source.initial))
      .createOrg('design');
    expect(result).toEqual({
      kind: 'failed',
      code: 'ORG_ACTION_NOT_AUTHORIZED',
      message: '当前身份没有该组织的操作权限。',
    });
    expect(port.calls).toHaveLength(1);
  });

  it('invites once and keeps the link out of storage and the URL', async () => {
    const port = fakePort(() => INVITE_RESULT);
    const source = createOrgLiveSourceForPort(port);
    const href = window.location.href;
    const result = await source
      .createGateway(memoryStore(source.initial))
      .createInvite('group-hyprial-dev', 'user:bob@example');
    expect(port.calls).toEqual([
      {
        method: 'org.invite',
        params: { org: 'group-hyprial-dev', user: 'user:bob@example' },
      },
    ]);
    expect(result.kind).toBe('ok');
    expect((result as { value: InviteResult }).value).toEqual({
      link: INVITE_RESULT.link,
      joinCommand: INVITE_RESULT.command,
      expiresAt: localClock(INVITE_RESULT.expiresAt),
      shortLink: null,
    });
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).toBe(href);
  });

  it('maps a refused invite to its code and reason', async () => {
    const port = fakePort(() => {
      throw new DomainError(
        'CASDOOR_USER_NOT_FOUND',
        'no such user',
        null,
        diagnostics('dispatched', 'org.invite'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const result = await source
      .createGateway(memoryStore(source.initial))
      .createInvite('group-hyprial-dev', 'user:nobody');
    expect(result).toEqual({
      kind: 'failed',
      code: 'CASDOOR_USER_NOT_FOUND',
      message: '身份服务里没有这个账号。',
    });
  });

  it('joins, replays the real steps in order and refreshes the member row', async () => {
    const port = fakePort((method) => (method === 'org.join' ? JOIN_RESULT : LIST_JOINED));
    const source = createOrgLiveSourceForPort(port);
    const steps: string[] = [];
    const store = memoryStore(source.initial);
    const result = await source
      .createGateway(store)
      .joinByLink('https://hyprial.ai/join#v2.example', (step) => steps.push(step));
    expect(port.calls.map((call) => call.method)).toEqual([
      'org.join',
      'org.list',
      ...DETAIL_METHODS,
    ]);
    expect(steps).toEqual(['connect', 'join', 'register', 'peers']);
    expect(result).toMatchObject({
      kind: 'ok',
      value: { id: 'group-partner-net', role: 'member', deviceCount: 2 },
    });
  });

  it('does not replay steps when the join is refused and never retries', async () => {
    const port = fakePort(() => {
      throw new DomainError(
        'INVITE_EXPIRED',
        'expired',
        null,
        diagnostics('dispatched', 'org.join'),
      );
    });
    const source = createOrgLiveSourceForPort(port);
    const steps: string[] = [];
    const result = await source
      .createGateway(memoryStore(source.initial))
      .joinByLink('https://hyprial.ai/join#v2.example', (step) => steps.push(step));
    expect(steps).toEqual([]);
    expect(result).toMatchObject({ kind: 'failed', code: 'INVITE_EXPIRED' });
    expect(port.calls).toHaveLength(1);
  });

  it('treats a timed-out join as unknown without retrying or faking steps', async () => {
    const port = fakePort(() => {
      throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timeout', DIAGNOSTICS);
    });
    const source = createOrgLiveSourceForPort(port);
    const steps: string[] = [];
    const result = await source
      .createGateway(memoryStore(source.initial))
      .joinByLink('https://hyprial.ai/join#v2.example', (step) => steps.push(step));
    expect(result).toEqual({
      kind: 'failed',
      code: 'REQUEST_TIMEOUT',
      message: ORG_COPY.joinUnknown,
      unknown: true,
    });
    expect(steps).toEqual([]);
    expect(port.calls).toHaveLength(1);
  });

  it('keeps a malformed create result unknown and skips refresh/retry', async () => {
    const { published: _published, ...withoutPublished } = CREATE_RESULT;
    const port = fakePort(() => withoutPublished);
    const source = createOrgLiveSourceForPort(port);
    const result = await source
      .createGateway(memoryStore(source.initial))
      .createOrg('design');

    expect(result).toEqual({
      kind: 'failed',
      code: ORG_RESULT_INVALID,
      message: ORG_COPY.createUnknown,
      unknown: true,
    });
    expect(port.calls.map((call) => call.method)).toEqual(['org.create']);
  });

  it('keeps a malformed invite result unknown and never invents a link', async () => {
    const port = fakePort(() => ({
      ...INVITE_RESULT,
      member: { ...INVITE_RESULT.member, mode: 'admin' },
    }));
    const source = createOrgLiveSourceForPort(port);
    const result = await source
      .createGateway(memoryStore(source.initial))
      .createInvite('group-hyprial-dev', 'user:bob@example');

    expect(result).toEqual({
      kind: 'failed',
      code: ORG_RESULT_INVALID,
      message: ORG_COPY.inviteUnknown,
      unknown: true,
    });
    expect(result).not.toMatchObject({ kind: 'ok' });
    expect(port.calls).toHaveLength(1);
  });

  it('keeps a malformed join result unknown without faking steps or retrying', async () => {
    const port = fakePort(() => ({
      ...JOIN_RESULT,
      steps: [{ kind: 'not-a-step', ok: true, detail: 'x' }],
    }));
    const source = createOrgLiveSourceForPort(port);
    const steps: string[] = [];
    const result = await source
      .createGateway(memoryStore(source.initial))
      .joinByLink('https://hyprial.ai/join#v2.example', (step) => steps.push(step));

    expect(result).toEqual({
      kind: 'failed',
      code: ORG_RESULT_INVALID,
      message: ORG_COPY.joinUnknown,
      unknown: true,
    });
    expect(steps).toEqual([]);
    expect(port.calls).toHaveLength(1);
  });

  it('does not define the 14 actions the live source never implements', () => {
    const port = fakePort(() => ({}));
    const source = createOrgLiveSourceForPort(port);
    const gateway = source.createGateway(memoryStore(source.initial));
    for (const name of ["claimInvite","renameOrg","listTemplates","previewImport","validateConfig","publishCandidate","adoptCandidate","decideMigration","checkMigration","planMigration","submitMigration","startMigration","cancelMigration","rollbackMigration"]) {
      expect(gateway[name as keyof typeof gateway], name).toBeUndefined();
    }
    expect(port.calls).toEqual([]);
  });

  it('changes the current organization locally without a backend write', async () => {
    const port = fakePort(() => ({}));
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore({
      ...source.initial,
      status: 'ready',
      currentOrgId: 'org-one',
      orgs: [
        { id: 'org-one', name: 'one', kind: 'work', role: 'owner', lifecycle: 'active', ownerLabel: null, authorityDevice: false, deviceCount: 1 },
        { id: 'org-two', name: 'two', kind: 'work', role: 'member', lifecycle: 'active', ownerLabel: null, authorityDevice: false, deviceCount: 1 },
      ],
    });
    const gateway = source.createGateway(store);
    await expect(gateway.setCurrentOrg?.('org-two')).resolves.toMatchObject({ kind: 'ok' });
    expect(store.get().currentOrgId).toBe('org-two');
    await expect(gateway.setCurrentOrg?.('missing')).resolves.toMatchObject({ kind: 'failed' });
    expect(store.get().currentOrgId).toBe('org-two');
    expect(port.calls).toEqual([]);
  });
});

describe('org lifecycle page wiring', () => {
  it('creates from the shared dialog and shows the refreshed owner row', async () => {
    let listCalls = 0;
    const port = fakePort((method) => {
      if (method === 'org.create') return CREATE_RESULT;
      listCalls += 1;
      return listCalls === 1 ? LIST_EMPTY : LIST_AFTER_CREATE;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.noOrgsTitle)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.createOrg }));
    const dialog = screen.getByRole('dialog', { name: ORG_COPY.createOrg });
    await user.type(within(dialog).getByLabelText(/组织名称/), 'design');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));

    expect(await screen.findByRole('dialog', { name: '组织已创建' })).toBeTruthy();
    expect(callsFor(port, 'org.create')).toEqual([
      { method: 'org.create', params: { org: 'group-design' } },
    ]);
    expect(callsFor(port, 'org.list')).toHaveLength(2);
    expect(window.localStorage.length).toBe(0);
  });

  it('keeps the dialog and shows the refusal without a success state', async () => {
    const port = fakePort((method) => {
      if (method === 'org.create') {
        throw new DomainError(
          'ORG_ACTION_NOT_AUTHORIZED',
          'not allowed',
          null,
          diagnostics('dispatched', 'org.create'),
        );
      }
      return LIST_EMPTY;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.noOrgsTitle)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.createOrg }));
    const dialog = screen.getByRole('dialog', { name: ORG_COPY.createOrg });
    const name = within(dialog).getByLabelText(/组织名称/);
    await user.type(name, 'design');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));

    expect(
      await within(dialog).findByText(/组织创建未完成：当前身份没有该组织的操作权限。/),
    ).toBeTruthy();
    expect(name).toHaveValue('design');
    expect(screen.queryByRole('dialog', { name: '组织已创建' })).toBeNull();
    expect(callsFor(port, 'org.create')).toHaveLength(1);
    expect(callsFor(port, 'org.list')).toHaveLength(1);
  });

  it('sends an invite from the shared section and shows the link only in the dialog', async () => {
    const port = fakePort((method) => {
      if (method === 'org.invite') return INVITE_RESULT;
      return LIST_OWNER;
    });
    const href = window.location.href;
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByRole('heading', { name: 'hyprial-dev' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.inviteMember }));
    await user.type(screen.getByLabelText(/成员账号/), 'user:bob@example');
    await user.click(screen.getByRole('button', { name: ORG_COPY.generateInvite }));

    const result = await screen.findByRole('dialog', { name: '邀请链接已生成' });
    expect(within(result).getByText(INVITE_RESULT.link)).toBeTruthy();
    expect(within(result).getByText(INVITE_RESULT.command)).toBeTruthy();
    expect(within(result).queryByText('短链接')).toBeNull();
    expect(callsFor(port, 'org.invite')).toEqual([
      {
        method: 'org.invite',
        params: { org: 'group-hyprial-dev', user: 'user:bob@example' },
      },
    ]);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).toBe(href);
  });

  it('shows an invite refusal without opening a result dialog', async () => {
    const port = fakePort((method) => {
      if (method === 'org.invite') {
        throw new DomainError(
          'CASDOOR_USER_NOT_FOUND',
          'no such user',
          null,
          diagnostics('dispatched', 'org.invite'),
        );
      }
      return LIST_OWNER;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByRole('heading', { name: 'hyprial-dev' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.inviteMember }));
    await user.type(screen.getByLabelText(/成员账号/), 'user:nobody');
    await user.click(screen.getByRole('button', { name: ORG_COPY.generateInvite }));

    expect(
      await screen.findByText('邀请链接未生成：身份服务里没有这个账号。'),
    ).toBeTruthy();
    expect(screen.queryByRole('dialog', { name: '邀请链接已生成' })).toBeNull();
    expect(callsFor(port, 'org.invite')).toHaveLength(1);
  });

  it('joins from the shared form with the real steps and refreshed member row', async () => {
    let listCalls = 0;
    const port = fakePort((method) => {
      if (method === 'org.join') return JOIN_RESULT;
      listCalls += 1;
      return listCalls === 1
        ? LIST_OWNER
        : { orgs: [...LIST_OWNER.orgs, ...LIST_JOINED.orgs], leftOrgs: [] };
    });
    const href = window.location.href;
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByRole('heading', { name: 'hyprial-dev' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.inviteMember }));
    await user.click(screen.getByRole('tab', { name: ORG_COPY.joinByLink }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    await user.type(link, 'https://hyprial.ai/join#v2.example-only');
    await user.click(screen.getByRole('button', { name: ORG_COPY.join }));

    expect(await screen.findByText(ORG_COPY.joined('partner-net'))).toBeTruthy();
    for (const label of ORG_COPY.joinSteps) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(screen.getByText(ORG_COPY.askSetCurrent('partner-net'))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: ORG_COPY.setCurrent })).toBeEnabled();
    expect(callsFor(port, 'org.join')).toEqual([
      {
        method: 'org.join',
        params: { link: 'https://hyprial.ai/join#v2.example-only' },
      },
    ]);
    expect(callsFor(port, 'org.list')).toHaveLength(2);
    expect(window.localStorage.length).toBe(0);
    expect(window.location.href).toBe(href);
  });

  it('keeps the first join result and steps after the empty list becomes an org', async () => {
    let listCalls = 0;
    const port = fakePort((method) => {
      if (method === 'org.join') return JOIN_RESULT;
      listCalls += 1;
      return listCalls === 1 ? LIST_EMPTY : LIST_JOINED;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.noOrgsTitle)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.joinByLink }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    await user.type(link, 'https://hyprial.ai/join#v2.first-join');
    await user.click(screen.getByRole('button', { name: ORG_COPY.join }));

    expect(await screen.findByText(ORG_COPY.joined('partner-net'))).toBeTruthy();
    expect(screen.getByLabelText('加入进度')).toBeTruthy();
    for (const label of ORG_COPY.joinSteps) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(callsFor(port, 'org.join')).toHaveLength(1);
    expect(callsFor(port, 'org.list')).toHaveLength(2);
  });

  it('disables the join form and sends one request while the response is pending', async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let listCalls = 0;
    const port = fakePort((method) => {
      if (method === 'org.join') return gate.then(() => JOIN_RESULT);
      listCalls += 1;
      return listCalls === 1 ? LIST_EMPTY : LIST_JOINED;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.noOrgsTitle)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.joinByLink }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    await user.type(link, 'https://hyprial.ai/join#v2.pending');
    await user.click(screen.getByRole('button', { name: ORG_COPY.join }));

    const pending = await screen.findByRole('button', { name: ORG_COPY.processing });
    expect(pending).toBeDisabled();
    expect(link).toBeDisabled();
    const form = link.closest('form');
    expect(form).not.toBeNull();
    if (form) {
      fireEvent.submit(form);
      fireEvent.submit(form);
    }
    expect(callsFor(port, 'org.join')).toHaveLength(1);

    release();
    expect(await screen.findByText(ORG_COPY.joined('partner-net'))).toBeTruthy();
    expect(callsFor(port, 'org.join')).toHaveLength(1);
  });

  it('shows a malformed join result as unknown without a failed step or retry', async () => {
    const port = fakePort((method) => {
      if (method === 'org.join') {
        return {
          ...JOIN_RESULT,
          steps: [{ kind: 'not-a-step', ok: true, detail: 'x' }],
        };
      }
      return LIST_EMPTY;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.noOrgsTitle)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.joinByLink }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    await user.type(link, 'https://hyprial.ai/join#v2.malformed');
    await user.click(screen.getByRole('button', { name: ORG_COPY.join }));

    expect(await screen.findByText(ORG_COPY.joinUnknown)).toBeTruthy();
    expect(screen.queryByLabelText('加入进度')).toBeNull();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
    expect(screen.queryByText(/^已加入/)).toBeNull();
    expect(callsFor(port, 'org.join')).toHaveLength(1);
    expect(callsFor(port, 'org.list')).toHaveLength(1);
  });

  it('shows the join refusal and next step without a joined state', async () => {
    const port = fakePort((method) => {
      if (method === 'org.join') {
        throw new DomainError(
          'INVITE_EXPIRED',
          'expired',
          null,
          diagnostics('dispatched', 'org.join'),
        );
      }
      return LIST_EMPTY;
    });
    const { user } = renderLive(port);
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(await screen.findByText(ORG_COPY.noOrgsTitle)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: ORG_COPY.joinByLink }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    await user.type(link, 'https://hyprial.ai/join#v2.expired');
    await user.click(screen.getByRole('button', { name: ORG_COPY.join }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('加入未完成：邀请已过期（1 小时）');
    expect(alert).toHaveTextContent('请邀请人重新生成邀请链接');
    expect(link).toHaveValue('');
    expect(screen.queryByText(/^已加入/)).toBeNull();
    expect(callsFor(port, 'org.join')).toHaveLength(1);
    expect(callsFor(port, 'org.list')).toHaveLength(1);
  });
});
