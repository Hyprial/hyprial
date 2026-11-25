import { describe, expect, it } from 'vitest';
import { createDevSettingsApis } from '../src/dev/mock/settings';

describe('development settings and device fixture', () => {
  it('exposes the configured device cards and nothing else', async () => {
    const apis = createDevSettingsApis();
    expect(Object.keys(apis).sort()).toEqual(['diagnostic', 'localManagement', 'logs', 'upgradeStatus']);
    await expect(apis.localManagement.status()).resolves.toMatchObject({ phase: 'serving', running: true });
    await expect(apis.diagnostic.read()).resolves.toMatchObject({ actorCount: 0, strandedCount: 0 });
  });

  it('starts without invented historical logs or migration successes', async () => {
    const apis = createDevSettingsApis();
    expect(await apis.logs.read()).toMatchObject({ entries: [], nextCursor: null, order: 'file-offset' });
    expect(await apis.upgradeStatus.steps?.()).toMatchObject({ steps: [], legacyFileCount: 0, legacyProcessCount: 0 });
  });

  it('gates reads on login and full initialization, and follows agent creation and logout', async () => {
    let loggedIn = false;
    let initialized = false;
    let count = 0;
    const apis = createDevSettingsApis({
      getAccountState: async () => ({ account: { status: loggedIn ? 'authenticated' : 'unauthenticated' }, device: { status: initialized ? 'ready' : 'not-ready' } }),
      actorCount: () => count,
    });
    await expect(apis.localManagement.status()).rejects.toThrow('DEVICE_NOT_INITIALIZED');
    loggedIn = true;
    await expect(apis.diagnostic.read()).rejects.toThrow('DEVICE_NOT_INITIALIZED');
    initialized = true;
    expect(await apis.diagnostic.read()).toMatchObject({ actorCount: 0 });
    count = 1;
    expect(await apis.diagnostic.read()).toMatchObject({ actorCount: 1 });
    loggedIn = false;
    await expect(apis.logs.read()).rejects.toThrow('DEVICE_NOT_INITIALIZED');
  });
});
