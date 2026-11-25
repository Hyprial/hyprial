import { createInitialSession } from '../../../src/dev/session/initial-session';
import { sessionReducer } from '../../../src/dev/session/session-reducer';
import {
  FEISHU_CHANNEL_ID,
  selectAgentSeed,
  selectConnections,
} from '../../../src/dev/session/session-selectors';

describe('J4 Feishu session projections', () => {
  it('J4 session: authorization only connects when the Agent exists and failure keeps it unconnected', () => {
    let state = sessionReducer(createInitialSession(), {
      type: 'first-run-select-provider',
      provider: 'service-a',
    });
    expect(state.messageConnection).toBe('not-connected');

    state = sessionReducer(state, {
      type: 'feishu-authorize',
      agentId: 'agent-missing',
    });
    expect(state.messageConnection).toBe('not-connected');

    state = sessionReducer(state, {
      type: 'first-run-select-model-method',
      method: 'account',
    });
    for (const step of ['account', 'model', 'conversation'] as const) {
      state = sessionReducer(state, { type: 'first-run-submit', step });
      state = sessionReducer(state, { type: 'first-run-succeed', step });
    }
    const squire = state.agents.find((agent) => agent.id === 'agent-squire');
    expect(squire).toBeTruthy();

    const authorized = sessionReducer(state, {
      type: 'feishu-authorize',
      agentId: 'agent-squire',
    });
    expect(authorized.messageConnection).toBe('connected');
    expect(selectConnections(authorized).some((row) => row.id === FEISHU_CHANNEL_ID)).toBe(true);

    const failed = sessionReducer(
      { ...state, flowScenarios: { ...state.flowScenarios, feishu: 'authorize-failure' } },
      { type: 'feishu-authorize', agentId: 'agent-squire' },
    );
    expect(failed.messageConnection).toBe('not-connected');

    const switched = sessionReducer(state, { type: 'flow-feishu-set', value: 'unavailable' });
    expect(switched.messageConnection).toBe('not-connected');
  });
});

describe('J4 channel projection (J2 regression)', () => {
  it('projects the Feishu channel onto the bound Agent only', () => {
    let state = createInitialSession();
    for (const step of ['account', 'model', 'conversation'] as const) {
      state = sessionReducer(state, { type: 'first-run-submit', step });
      state = sessionReducer(state, { type: 'first-run-succeed', step });
    }
    state = sessionReducer(state, {
      type: 'agent-create',
      draft: {
        displayName: '整理助手',
        role: '整理',
        modelServiceId: 'model-1',
        organizationId: null,
      },
    });
    const before = selectAgentSeed(state).agents;
    expect(before.every((agent) => agent.channelServiceId !== FEISHU_CHANNEL_ID)).toBe(true);

    state = sessionReducer(state, {
      type: 'feishu-authorize',
      agentId: 'agent-squire',
    });
    const after = selectAgentSeed(state).agents;
    const squire = after.find((agent) => agent.id === 'agent-squire');
    const created = after.find((agent) => agent.id !== 'agent-squire');
    expect(squire?.channelServiceId).toBe(FEISHU_CHANNEL_ID);
    expect(created?.channelServiceId).not.toBe(FEISHU_CHANNEL_ID);

    const cleared = sessionReducer(state, { type: 'flow-feishu-set', value: 'unavailable' });
    expect(
      selectAgentSeed(cleared).agents.every(
        (agent) => agent.channelServiceId !== FEISHU_CHANNEL_ID,
      ),
    ).toBe(true);
  });
});
