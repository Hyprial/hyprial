import {
  createInitialSession,
} from '../../src/dev/session/initial-session';
import {
  sessionReducer,
} from '../../src/dev/session/session-reducer';
import {
  FEISHU_CHANNEL_ID,
  selectMessageConnection,
  selectAgentSeed,
  selectChatSeed,
  selectConnections,
  selectFirstRunState,
  selectMachineModel,
  selectOrgModel,
  selectTopologyModel,
} from '../../src/dev/session/session-selectors';
import {
  EXTERNAL_INVITE_CODE,
  INVALID_CODE,
  OWNER_INVITE_CODE,
  type LinkedSession,
} from '../../src/dev/session/session-types';

function runLinkedFirstRun(session: LinkedSession): LinkedSession {
  let state = sessionReducer(session, {
    type: 'first-run-select-provider',
    provider: 'service-a',
  });
  state = sessionReducer(state, {
    type: 'first-run-select-model-method',
    method: 'account',
  });
  state = sessionReducer(state, { type: 'first-run-submit', step: 'account' });
  state = sessionReducer(state, { type: 'first-run-succeed', step: 'account' });
  state = sessionReducer(state, { type: 'first-run-submit', step: 'model' });
  state = sessionReducer(state, { type: 'first-run-succeed', step: 'model' });
  state = sessionReducer(state, {
    type: 'first-run-submit',
    step: 'conversation',
  });
  state = sessionReducer(state, {
    type: 'first-run-succeed',
    step: 'conversation',
  });
  return state;
}

describe('linked session: first run and its products', () => {
  it('A-01..A-04 completes directly from model to conversation and produces model, messages and this computer', () => {
    const state = runLinkedFirstRun(createInitialSession());

    expect(state.firstRun.status).toBe('completed');
    expect(state.firstRun.step).toBe(3);
    expect(state.firstRun.accountLabel).toBe('演示账号');
    expect(state.modelServices).toHaveLength(1);
    expect(state.modelServices[0].state).toBe('connected');
    expect(state.agents).toHaveLength(1);
    expect(state.agents[0].name).toBe('Squire');
    expect(state.messages).toHaveLength(2);
    expect(state.messages.filter((message) => message.author === 'me')).toHaveLength(1);
    expect(state.machines).toHaveLength(1);
    expect(state.machines[0].isThisComputer).toBe(true);
    expect(state.machines[0].label).toBe('本机');
  });

  it('projects the first-run state for the product page', () => {
    const fresh = selectFirstRunState(createInitialSession());
    expect(fresh.step).toBe('account');
    expect(fresh.status).toBe('idle');

    const done = selectFirstRunState(runLinkedFirstRun(createInitialSession()));
    expect(done.status).toBe('complete');
  });

  it('keeps the first run deterministic and idempotent', () => {
    const first = runLinkedFirstRun(createInitialSession());
    const again = sessionReducer(first, {
      type: 'first-run-succeed',
      step: 'conversation',
    });
    expect(again.machines).toHaveLength(1);
    expect(again.messages).toHaveLength(2);
  });
});

describe('linked session: agents', () => {
  it('A-05 adds one Agent with an unconnected channel and no duplicate ids', () => {
    let state = runLinkedFirstRun(createInitialSession());
    state = sessionReducer(state, {
      type: 'agent-create',
      draft: {
        displayName: '新助手',
        role: '整理',
        modelServiceId: state.modelServices[0].id,
        organizationId: null,
      },
    });

    expect(state.agents).toHaveLength(2);
    expect(selectAgentSeed(state).agents[1].channelServiceId).not.toBe(FEISHU_CHANNEL_ID);
    expect(selectMessageConnection(state)).toEqual({
      state: 'not-connected',
      agentId: null,
      authorizeOutcome: 'success',
    });
    expect(new Set(state.agents.map((agent) => agent.id)).size).toBe(2);
    expect(selectAgentSeed(state).agents).toHaveLength(2);
  });

  it('A-06 exposes model and channel states through the seed', () => {
    const state = runLinkedFirstRun(createInitialSession());
    const seed = selectAgentSeed(state);
    const squire = seed.agents[0];
    expect(squire.modelLabel).toBeTruthy();
    expect(seed.connections.some((connection) => connection.status === 'connected')).toBe(
      true,
    );
  });

  it('feeds the chat journey from the same Squire conversation', () => {
    const state = runLinkedFirstRun(createInitialSession());
    const chat = selectChatSeed(state);
    expect(chat.availableTargetId).toBe('agent-squire');
    expect(chat.threads?.['agent-squire']).toHaveLength(2);
  });
});

describe('linked session: organization, members and identity', () => {
  it('A-07 creates the organization, the owner member and enables communication', () => {
    const state = sessionReducer(createInitialSession(), {
      type: 'org-create',
      name: '设计组',
    });

    expect(state.org?.name).toBe('设计组');
    expect(state.org?.inviteCode).toBe(OWNER_INVITE_CODE);
    expect(state.members).toHaveLength(1);
    expect(state.members[0].role).toBe('owner');
    expect(state.communication).toBe('available');
  });

  it('A-08 adds one pending invite and stays owner-only', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'org-create',
      name: '设计组',
    });
    state = sessionReducer(state, { type: 'org-invite' });
    state = sessionReducer(state, { type: 'org-invite' });

    expect(state.invites).toHaveLength(1);
    expect(state.invites[0]).toMatchObject({
      code: OWNER_INVITE_CODE,
      identity: 'invitee',
      state: 'pending',
    });

    const asInvitee = sessionReducer(state, {
      type: 'viewer-set',
      viewer: 'invitee',
    });
    const inviteAgain = sessionReducer(asInvitee, { type: 'org-invite' });
    expect(inviteAgain.invites).toHaveLength(1);
  });

  it('V-01a owner builds, invitee joins with the owner code and becomes a member', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'org-create',
      name: '设计组',
    });
    state = sessionReducer(state, { type: 'org-invite' });
    state = sessionReducer(state, { type: 'viewer-set', viewer: 'invitee' });
    state = sessionReducer(state, { type: 'org-join', code: OWNER_INVITE_CODE });

    expect(state.members).toHaveLength(2);
    expect(state.members[1]).toMatchObject({ identity: 'invitee', role: 'member' });
    expect(state.invites[0].state).toBe('accepted');

    state = sessionReducer(state, { type: 'viewer-set', viewer: 'owner' });
    const org = selectOrgModel(state);
    expect(org.members.map((member) => member.name)).toEqual([
      '创建者',
      '受邀成员',
    ]);
  });

  it('V-01b invitee joins the external organization and never sees invite or create', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'viewer-set',
      viewer: 'invitee',
    });
    const beforeJoin = selectOrgModel(state);
    expect(beforeJoin.createEnabled).toBe(false);
    expect(beforeJoin.inviteEnabled).toBe(false);
    expect(beforeJoin.joinEnabled).toBe(true);

    state = sessionReducer(state, { type: 'org-join', code: EXTERNAL_INVITE_CODE });
    expect(state.org?.name).toBe('外部组织');
    expect(state.members).toHaveLength(1);
    expect(state.activeViewer).toBe('invitee');

    const afterJoin = selectOrgModel(state);
    expect(afterJoin.createEnabled).toBe(false);
    expect(afterJoin.inviteEnabled).toBe(false);
    expect(afterJoin.joinEnabled).toBe(false);
  });

  it('V-01c an unjoined owner sees only the join entry', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'viewer-set',
      viewer: 'invitee',
    });
    state = sessionReducer(state, { type: 'org-join', code: EXTERNAL_INVITE_CODE });
    state = sessionReducer(state, { type: 'viewer-set', viewer: 'owner' });

    const org = selectOrgModel(state);
    expect(org.createEnabled).toBe(false);
    expect(org.inviteEnabled).toBe(false);
    expect(org.joinEnabled).toBe(true);
    expect(org.members).toHaveLength(1);
  });

  it('rejects an unknown code without changing the session', () => {
    const before = createInitialSession();
    const after = sessionReducer(before, { type: 'org-join', code: 'WRONG' });
    expect(after.org).toBeNull();
    expect(after.members).toHaveLength(0);
    expect(after.firstRun.error).toBe(INVALID_CODE);
  });

  it('is idempotent when the same identity joins twice', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'org-join',
      code: EXTERNAL_INVITE_CODE,
    });
    state = sessionReducer(state, { type: 'org-join', code: EXTERNAL_INVITE_CODE });
    expect(state.members).toHaveLength(1);
  });

  it('A-09b and A-09c switch identity without losing data', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'org-create',
      name: '设计组',
    });
    state = sessionReducer(state, { type: 'viewer-set', viewer: 'invitee' });
    expect(state.org?.name).toBe('设计组');
    expect(state.members).toHaveLength(1);
    state = sessionReducer(state, { type: 'viewer-set', viewer: 'owner' });
    expect(state.activeViewer).toBe('owner');
    expect(state.members).toHaveLength(1);
  });
});

describe('linked session: machines, topology and monitor', () => {
  it('V-07 adds a second computer only after the organization exists', () => {
    const withoutOrg = sessionReducer(createInitialSession(), {
      type: 'machine-add',
    });
    expect(withoutOrg.machines).toHaveLength(0);

    let state = runLinkedFirstRun(createInitialSession());
    state = sessionReducer(state, { type: 'org-create', name: '设计组' });
    state = sessionReducer(state, { type: 'machine-add' });
    expect(state.machines).toHaveLength(2);
    expect(state.machines[1].isThisComputer).toBe(false);
    expect(state.machines[1].status).toBe('online');

    const switched = sessionReducer(state, { type: 'viewer-set', viewer: 'invitee' });
    expect(switched.machines).toHaveLength(2);
  });

  it('V-08 V-09 exposes one edge, Inspect fields and the recent communication record', () => {
    let state = runLinkedFirstRun(createInitialSession());
    state = sessionReducer(state, { type: 'org-create', name: '设计组' });
    state = sessionReducer(state, { type: 'machine-add' });

    const topology = selectTopologyModel(state);
    expect(topology.dataMode).toBe('full');
    expect(topology.nodes).toHaveLength(state.members.length + state.machines.length);
    expect(topology.edges).toHaveLength(1);
    expect(topology.monitor.title).toBe('最近通信记录');
    expect(topology.monitor.sample?.observedAt).toBeTruthy();
    expect(topology.monitor.sample?.fromLabel).toBeTruthy();
  });

  it('A-11 marks communication unavailable for the regression view', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'org-create',
      name: '设计组',
    });
    state = sessionReducer(state, {
      type: 'communication-set',
      value: 'unavailable',
    });
    const topology = selectTopologyModel(state);
    expect(topology.dataMode).toBe('structure-only');
    expect(topology.edges).toHaveLength(0);
    expect(topology.monitor.mode).toBe('unavailable');
  });

  it('marks the machine section as the linked scene with this computer', () => {
    let state = runLinkedFirstRun(createInitialSession());
    state = sessionReducer(state, { type: 'org-create', name: '设计组' });
    const machines = selectMachineModel(state);
    expect(machines.scene).toBe('linked');
    expect(machines.machines).toHaveLength(1);
    expect(machines.machines[0].current).toBe(true);
  });

  it('rejects the external code once an organization already exists', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'org-create',
      name: '设计组',
    });
    state = sessionReducer(state, { type: 'viewer-set', viewer: 'invitee' });
    state = sessionReducer(state, { type: 'org-join', code: EXTERNAL_INVITE_CODE });

    expect(state.org?.name).toBe('设计组');
    expect(state.members).toHaveLength(1);
    expect(state.firstRun.error).toBe(INVALID_CODE);
  });

  it('projects the linked service into the connections list', () => {
    const state = runLinkedFirstRun(createInitialSession());
    const connections = selectConnections(state);
    const service = connections.find((entry) => entry.kind === 'model');
    expect(service?.label).toBe('模型服务 A');
    expect(service?.status).toBe('connected');
  });
});
