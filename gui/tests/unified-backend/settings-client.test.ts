import { describe, expect, it, vi } from 'vitest';
import { createGeneratedClient } from '../../src/client/generated';
import { createDiagnosticApiFromClient } from '../../src/features/settings';

describe('settings adapters consume one injected generated client', () => {
  it('preserves readonly requests and cancellation through the shared instance', async () => {
    const invoke = vi.fn().mockImplementation(async (method: string) => {
      if (method === 'top.snapshot') return { daemon: {}, quota: {}, actors: [], stranded: [], nowMs: 42 };
      throw new Error(`unexpected method ${method}`);
    });
    const client = createGeneratedClient({ invoke });
    const signal = new AbortController().signal;
    expect(await createDiagnosticApiFromClient(client).read(signal)).toEqual({ observedAtMs: 42, actorCount: 0, strandedCount: 0 });
    expect(invoke.mock.calls).toEqual([
      ['top.snapshot', {}, { signal, retry: 'safe' }],
    ]);
  });

  it('does not turn read failures into empty data', async () => {
    const failure = new Error('HTTP_NOT_READY');
    const invoke = vi.fn().mockRejectedValue(failure);
    const client = createGeneratedClient({ invoke });
    await expect(createDiagnosticApiFromClient(client).read()).rejects.toBe(failure);
    expect(invoke).toHaveBeenCalledTimes(1);
  });
});
