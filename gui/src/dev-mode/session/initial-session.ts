import type { LinkedSession } from './session-types';

/** Deterministic seed for one development session. */
export function createInitialSession(): LinkedSession {
  return {
    revision: 0,
    activeViewer: 'owner',
    firstRun: {
      step: 1,
      status: 'in_progress',
      pending: false,
      accountLabel: null,
      modelServiceId: null,
      squireReady: false,
      provider: null,
      modelMethod: null,
      mode: 'simulate',
      progress: 0,
      error: null,
    },
    modelServices: [],
    modelAddMode: 'ready',
    modelServiceSeed: 'default',
    messageConnection: 'not-connected',
    messageConnectionAgentId: null,
    flowScenarios: { feishu: 'ready' },
    chatChannel: 'in-app',
    agents: [],
    messages: [],
    org: null,
    members: [],
    invites: [],
    devices: { local: { id: 'dev-local', host: '本机', os: 'macOS' } },
    machines: [],
    communication: 'unavailable',
    seq: { agent: 0, message: 0, machine: 0 },
    lastResult: null,
  };
}
