/** Initialization is one write; observation belongs to the status/transition owner. */
import { describe, expect, it, vi } from 'vitest';
import { DomainError, TransportError } from '../../../../src/client';
import { createConnectedDeviceActions } from '../../../../src/features/settings';

type DeviceClient = Parameters<typeof createConnectedDeviceActions>[0];
const diagnostics = { method: 'onboarding.initialize', requestId: 'test', status: 500,
  dispatch: 'dispatched' as const, catalogDigest: 'test', attempts: 1, startedAt: 0, durationMs: 1 };
function deviceClient(state = 'promoting', initialized = false) {
  const initialize = vi.fn().mockResolvedValue({ ok: true, operationId: 'init-1', receiptId: 'receipt-1', state, initialized });
  const status = vi.fn().mockResolvedValue({ authenticated: true, deviceReady: true, initialized: true, phase: 'serving', operations: [] });
  const ensure = vi.fn();
  const client = { onboarding: { initialize, status, device: { ensure } } } as unknown as DeviceClient;
  return { client, initialize, status, ensure };
}

describe('createConnectedDeviceActions', () => {
  it.each(['accepted', 'promoting', 'unknown'])('keeps %s unresolved without polling or replaying writes', async state => {
    const { client, initialize, status, ensure } = deviceClient(state);
    const transition = vi.fn();
    const actions = createConnectedDeviceActions(client, undefined, transition);
    expect((await actions.initializeDevice!()).status).toBe('unknown');
    expect((await actions.initializeDevice!()).status).toBe('unknown');
    expect(initialize).toHaveBeenCalledTimes(1);
    expect(status).not.toHaveBeenCalled();
    expect(ensure).not.toHaveBeenCalled();
    expect(transition).toHaveBeenCalledTimes(state === 'unknown' ? 0 : 1);
  });
  it('returns unresolved without a transition owner, without polling', async () => {
    const { client, initialize, status } = deviceClient();
    expect((await createConnectedDeviceActions(client).initializeDevice!()).status).toBe('unknown');
    expect(initialize).toHaveBeenCalledTimes(1);
    expect(status).not.toHaveBeenCalled();
  });
  it('accepts only an authoritative completed receipt as ready', async () => {
    const { client, initialize, ensure, status } = deviceClient('completed', true);
    expect(await createConnectedDeviceActions(client).initializeDevice!()).toEqual({ status: 'ready' });
    expect(initialize.mock.calls[0][0]).toEqual({ operationKey: expect.any(String) });
    expect(ensure).not.toHaveBeenCalled();
    expect(status).not.toHaveBeenCalled();
    const incomplete = deviceClient('completed', false);
    expect((await createConnectedDeviceActions(incomplete.client).initializeDevice!()).status).toBe('unknown');
  });
  it('coalesces concurrent writes', async () => {
    const { client, initialize } = deviceClient();
    const actions = createConnectedDeviceActions(client);
    const [first, second] = await Promise.all([actions.initializeDevice!(), actions.initializeDevice!()]);
    expect(first).toEqual(second);
    expect(initialize).toHaveBeenCalledTimes(1);
  });
  it('checks initialize access without depending on status or ensure availability', async () => {
    const { client, initialize } = deviceClient();
    const access = vi.fn(method => ({ available: method === 'onboarding.initialize' }));
    expect((await createConnectedDeviceActions(client, access).initializeDevice!()).status).toBe('unknown');
    expect(access).toHaveBeenCalledTimes(1);
    expect(initialize).toHaveBeenCalledTimes(1);
    const denied = deviceClient();
    expect((await createConnectedDeviceActions(denied.client, () => ({ available: false })).initializeDevice!()).status).toBe('unavailable');
    expect(denied.initialize).not.toHaveBeenCalled();
  });
  it.each(['dispatched', 'unknown'] as const)('does not retry an unknown outcome with %s dispatch', async dispatch => {
    const { client, initialize } = deviceClient();
    initialize.mockRejectedValue(new DomainError('SUBMIT_OUTCOME_UNKNOWN', 'unconfirmed', { retryable: false }, { ...diagnostics, dispatch }));
    const actions = createConnectedDeviceActions(client);
    expect((await actions.initializeDevice!()).status).toBe('unknown');
    expect((await actions.initializeDevice!()).status).toBe('unknown');
    expect(initialize).toHaveBeenCalledTimes(1);
  });
  it('preserves declared device failure and not-dispatched transport failure', async () => {
    const { client, initialize } = deviceClient();
    initialize.mockRejectedValue(new DomainError('DEVICE_ENSURE_FAILED', 'failed', { retryable: true }, diagnostics));
    const actions = createConnectedDeviceActions(client);
    expect(await actions.initializeDevice!()).toMatchObject({ status: 'failed', retryable: true, message: expect.stringContaining('DEVICE_ENSURE_FAILED') });
    initialize.mockRejectedValue(new TransportError('network', 'HTTP_NOT_READY', 'offline', { ...diagnostics, dispatch: 'not-dispatched' }));
    expect(await actions.initializeDevice!()).toMatchObject({ status: 'failed', retryable: true });
  });
});
