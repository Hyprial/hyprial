/**
 * Deterministic seed for one development session.
 *
 * The seed carries the generic baseline Squire (`agent-squire`, bound to the
 * `model-1` id first run produces) so the chat page and the message-connection
 * journey have a target before any first-run interaction. Availability stays
 * `needs-connection` until a real model service connects; the seed never
 * claims a connected backend.
 */
import type { LinkedSession } from './session-types';

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
    agents: [
      {
        id: 'agent-squire',
        name: 'Squire',
        role: '个人助理 · 统一入口',
        modelServiceId: 'model-1',
        state: 'idle',
      },
    ],
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
