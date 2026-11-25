import { expect, it, vi } from 'vitest';
import { createBackendConnection } from '../../../../src/app/backend';
import { createGeneratedClient } from '../../../../src/client/generated';
import { createSettingsReadApis } from '../../../../src/features/settings/impl/read-adapters';

it('uses the injected generated client for the device reads, forwarding cancellation and selectors', async () => {
  const results: Record<string, unknown> = {
    'daemon.status': { running: true, version: 'test', phase: 'serving' },
    'autoupdate.status': { ok: true, active: false },
    'upgrade.steps.status': { steps: [
      { id: 'future-step', state: 'future-state', attempts: 2, updatedAt: null, detail: 'private', data: {} },
    ], legacyArgvFiles: ['/private'], legacyProcesses: [{}], pending: [] },
    'log.page': { entries: [], nextCursor: null, order: 'file-offset', partial: true,
      partialReasons: ['logs-unavailable'], skippedLines: 0, snapshotExpiresInSeconds: 60 },
  };
  const invoke = vi.fn().mockImplementation(async (method: string) => results[method]);
  const api = createSettingsReadApis(createGeneratedClient({ invoke }));
  const signal = new AbortController().signal;
  expect(await api.localManagement.status(signal)).toEqual(results['daemon.status']);
  expect(await api.upgradeStatus.status(signal)).toEqual(results['autoupdate.status']);
  expect(await api.upgradeStatus.steps!(signal)).toEqual({ steps: [
    { id: 'other', state: 'unknown', attempts: 2, updatedAt: null, hasDetail: true },
  ], legacyFileCount: 1, legacyProcessCount: 1 });
  expect(await api.logs.read('opaque-cursor', signal)).toEqual(results['log.page']);
  for (const method of Object.keys(results)) {
    const params = method === 'log.page' ? { cursor: 'opaque-cursor', limit: 20 } : {};
    expect(invoke).toHaveBeenCalledWith(method, params, { signal, retry: 'safe' });
  }
  expect(invoke).toHaveBeenCalledTimes(4);
});

it('rejects all retained capabilities after the common connection retires', async () => {
  const invoke = vi.fn();
  const connection = createBackendConnection({ invoke }, { interfaceVersion: 1, driver: 'daemon',
    phase: 'ready', generation: 'g1', catalogDigest: 'digest' });
  const api = createSettingsReadApis(connection.client);
  connection.retire();
  for (const call of [() => api.localManagement.status(), () => api.upgradeStatus.status(),
    () => api.upgradeStatus.steps!(), () => api.logs.read()]) {
    await expect(call()).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RETIRED', dispatch: 'not-dispatched' });
  }
  expect(invoke).not.toHaveBeenCalled();
});

it('rejects malformed daemon status without inferring that the daemon stopped', async () => {
  const invoke = vi.fn().mockResolvedValue({ running: false });
  await expect(createSettingsReadApis(createGeneratedClient({ invoke })).localManagement.status())
    .rejects.toThrow('DAEMON_STATUS_RESULT_INVALID');
});

it('preserves a partial log page and omits an absent cursor', async () => {
  const page = { entries: [], nextCursor: null, partial: true, partialReasons: ['logs-unavailable'] };
  const invoke = vi.fn().mockResolvedValue(page);
  expect(await createSettingsReadApis(createGeneratedClient({ invoke })).logs.read()).toBe(page);
  expect(invoke).toHaveBeenCalledExactlyOnceWith('log.page', { limit: 20 }, { signal: undefined, retry: 'safe' });
});

it('lets the common catalog reject an unavailable method before dispatch', async () => {
  const invoke = vi.fn();
  const connection = createBackendConnection({ invoke }, { interfaceVersion: 1, driver: 'daemon',
    phase: 'ready', generation: 'g1', catalogDigest: 'digest', methods: [
      { name: 'daemon.status', callable: false, state: 'unavailable', mutates: false },
    ] });
  const api = createSettingsReadApis(connection.client);
  await expect(api.localManagement.status())
    .rejects.toMatchObject({ code: 'METHOD_NOT_CALLABLE', dispatch: 'not-dispatched' });
  expect(invoke).not.toHaveBeenCalled();
});
