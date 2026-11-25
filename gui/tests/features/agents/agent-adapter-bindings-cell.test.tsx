import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DomainError } from '../../../src/client';
import type { GeneratedClient } from '../../../src/client/generated';
import {
  createAgentAdapterBindingsFromClient,
  type AgentAdapterBindingsApi,
  type AgentAdapterBindingRow,
} from '../../../src/features/agents/impl/api/agent-adapter-bindings';
import { PlatformBindingsTab } from '../../../src/features/agents/impl/detail/PlatformBindingsTab';

const AGENT = 'agent:owner:node:a.worker';

interface CellShape {
  readonly members: readonly { ref: string; role: string }[];
  readonly params: Record<string, unknown>;
}

function cellClient(cell: CellShape) {
  const updates: Record<string, unknown>[] = [];
  const client = {
    adapter: { list: async () => ({ ok: true, adapters: [] }) },
    agent: {
      cell: {
        get: async () => ({ ok: true, cell: { revision: 3, ...cell } }),
        update: async (params: Record<string, unknown>) => {
          updates.push(params);
          return { ok: true, revision: 4 };
        },
      },
    },
  };
  const api = createAgentAdapterBindingsFromClient(
    client as unknown as Pick<GeneratedClient, 'adapter' | 'agent'>,
  );
  return { api, updates };
}

const HARNESS = { ref: AGENT, role: 'harness' };

describe('cell-backed adapter binding writes', () => {
  it('unbinds a member whose ref differs from the adapter name', async () => {
    const { api, updates } = cellClient({
      members: [HARNESS, { ref: 'im', role: 'adapter' }],
      params: { 'cell.entry': AGENT, 'cell.members': { [AGENT]: {}, im: { adapter: 'lark-main' } } },
    });

    await api.unbind('lark-main', AGENT);

    expect(updates).toEqual([{
      name: AGENT,
      expectedRevision: 3,
      members: [HARNESS],
      params: { 'cell.entry': AGENT, 'cell.members': { [AGENT]: {} } },
    }]);
  });

  it('refuses an unbind that matches no adapter member instead of silently succeeding', async () => {
    const { api, updates } = cellClient({
      members: [HARNESS],
      params: { 'cell.members': { [AGENT]: {} } },
    });

    await expect(api.unbind('lark-main', AGENT)).rejects.toThrow('lark-main');
    expect(updates).toEqual([]);
  });

  it('treats a bind as done when any adapter member already points at the adapter', async () => {
    const { api, updates } = cellClient({
      members: [HARNESS, { ref: 'alerts', role: 'adapter' }, { ref: 'im', role: 'adapter' }],
      params: { 'cell.members': { [AGENT]: {}, alerts: { adapter: 'lark-alerts' }, im: { adapter: 'lark-main' } } },
    });

    await api.bind('lark-main', AGENT);

    expect(updates).toEqual([]);
  });
});

function refusal(code: string, data: unknown): DomainError {
  return new DomainError(code, 'opaque backend diagnostic', data, {
    method: 'agent.cell.update', requestId: 'test', status: 400, catalogDigest: null,
    attempts: 1, startedAt: 0, durationMs: 1, dispatch: 'dispatched',
  });
}

function refusingApi(error: unknown): AgentAdapterBindingsApi {
  const row: AgentAdapterBindingRow = {
    name: 'feishu-main', provider: 'lark', online: true, processRunning: true, boundAgent: null,
  };
  return {
    routes: { available: false },
    async list() { return [row]; },
    async bind() { throw error; },
    async unbind() {},
  };
}

describe('binding refusal notices', () => {
  it('names a revision conflict as a changed binding', async () => {
    render(<PlatformBindingsTab agentActor={AGENT} api={refusingApi(refusal('INVALID_ARGUMENT', { expected: 3, current: 4 }))} />);
    await userEvent.setup().click(await screen.findByRole('button', { name: '绑定' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('绑定已变化');
  }, 10000);

  it('does not call a structural INVALID_ARGUMENT a changed binding', async () => {
    render(<PlatformBindingsTab agentActor={AGENT} api={refusingApi(refusal('INVALID_ARGUMENT', undefined))} />);
    await userEvent.setup().click(await screen.findByRole('button', { name: '绑定' }));
    const alert = await screen.findByRole('alert');
    expect(alert).not.toHaveTextContent('绑定已变化');
    expect(alert).toHaveTextContent('INVALID_ARGUMENT');
  }, 10000);
});
