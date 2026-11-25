/** Agent adapters with the actual shared interface v1; transport doubles only, not live evidence. */
import { describe, expect, it, vi } from 'vitest';
import { createBackendConnection, type BackendDescriptor } from '../../src/app/backend';
import { TransportError, type RequestDiagnostics } from '../../src/client';
import type { AgentListResult, AgentStartResult } from '../../src/client/generated/types';
import { createAgentsApi, createRuntimeActionHandler } from '../../src/features/agents/impl/api';

const descriptor: BackendDescriptor = {
  interfaceVersion: 1, driver: 'daemon', phase: 'ready',
  generation: 'agent-generation-1', catalogDigest: 'agent-catalog',
};

describe('agent adapter compatibility with shared interface v1', () => {
  it.each(['daemon', 'contract-mock'] as const)('uses the same injected client and catalog pin for %s', async driver => {
    const invoke = vi.fn()
      .mockResolvedValueOnce({ ok: true, agents: [], agentsWithoutRoutine: [] } satisfies AgentListResult)
      .mockResolvedValueOnce({ ok: true, accepted: true, running: false } satisfies AgentStartResult);
    const connection = createBackendConnection({ invoke }, { ...descriptor, driver });
    const api = createAgentsApi(connection.client);
    await expect(api.list()).resolves.toEqual([]);
    await expect(api.start('worker')).resolves.toEqual({ accepted: true, running: false, warnings: [] });
    expect(invoke.mock.calls).toEqual([
      ['agent.list', {}, { retry: 'safe', catalogDigest: 'agent-catalog' }],
      ['agent.start', { name: 'worker' }, { retry: 'never', catalogDigest: 'agent-catalog' }],
    ]);
  });

  it('preserves retired-connection diagnostics without dispatch or readback', async () => {
    const invoke = vi.fn();
    const after = vi.fn();
    const connection = createBackendConnection({ invoke }, descriptor);
    const action = createRuntimeActionHandler(createAgentsApi(connection.client), after);
    connection.retire();
    expect(await action('start', 'worker')).toMatchObject({
      tone: 'error', code: 'BACKEND_CONNECTION_RETIRED', dispatched: false,
      diagnostics: { dispatch: 'not-dispatched', requestId: null, attempts: 0, catalogDigest: 'agent-catalog' },
    });
    expect(invoke).not.toHaveBeenCalled();
    expect(after).not.toHaveBeenCalled();
  });

  it('keeps an in-flight unknown request intact when the common connection retires', async () => {
    let reject!: (error: unknown) => void;
    const invoke = vi.fn(() => new Promise<never>((_resolve, rejectRequest) => { reject = rejectRequest; }));
    const connection = createBackendConnection({ invoke }, descriptor);
    const api = createAgentsApi(connection.client);
    const pending = api.start('worker');
    connection.retire();
    const diagnostics: RequestDiagnostics = {
      method: 'agent.start', requestId: 'original-agent-request', status: null,
      dispatch: 'unknown', catalogDigest: 'agent-catalog', attempts: 1, startedAt: 1, durationMs: 20,
    };
    const outcome = expect(pending).rejects.toMatchObject({ code: 'REQUEST_TIMEOUT', dispatch: 'unknown', diagnostics });
    reject(new TransportError('timeout', 'REQUEST_TIMEOUT', 'response lost', diagnostics));
    await outcome;
    expect(invoke).toHaveBeenCalledOnce();
  });
});
