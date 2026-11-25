import { expect, it, vi } from 'vitest';
import { createBackendConnection } from '../../src/app/backend';
import { TransportError } from '../../src/client';
import { createDiagnosticApiFromClient } from '../../src/features/settings';

const descriptor = { interfaceVersion: 1 as const, driver: 'daemon' as const, phase: 'ready' as const,
  generation: 'settings-g1', catalogDigest: 'settings-catalog' };

it('rejects calls retained by the settings adapter after the common connection retires', async () => {
  const invoke = vi.fn();
  const connection = createBackendConnection({ invoke }, descriptor);
  const diagnostic = createDiagnosticApiFromClient(connection.client);
  connection.retire();
  for (const call of [() => diagnostic.read(), () => connection.client.agent.provider.logout({ actor: 'actor', provider: 'pi' })]) {
    await expect(call()).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RETIRED', dispatch: 'not-dispatched',
      diagnostics: { attempts: 0, requestId: null, catalogDigest: descriptor.catalogDigest } });
  }
  expect(invoke).not.toHaveBeenCalled();
});

it('rejects settings reads missing from or disabled by the pinned catalog and dispatches a declared write', async () => {
  const invoke = vi.fn().mockResolvedValue({ ok: true, actor: 'actor', provider: 'pi' });
  const connection = createBackendConnection({ invoke }, { ...descriptor, methods: [
    { name: 'agent.list', callable: false, state: 'disabled', mutates: false },
    { name: 'agent.secret.list', callable: false, state: 'disabled', mutates: false },
    { name: 'agent.provider.default', callable: true, state: 'ready', mutates: true },
    { name: 'agent.provider.logout', callable: true, state: 'ready', mutates: true },
  ] });
  await expect(createDiagnosticApiFromClient(connection.client).read()).rejects.toMatchObject({
    code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched', diagnostics: { attempts: 0 },
  });
  await expect(connection.client.agent.provider.logout({ actor: 'actor', provider: 'pi' }))
    .resolves.toMatchObject({ actor: 'actor', provider: 'pi' });
  expect(invoke).toHaveBeenCalledExactlyOnceWith('agent.provider.logout', { actor: 'actor', provider: 'pi' },
    { retry: 'never', catalogDigest: descriptor.catalogDigest });
});

it('passes provider default parameters through generated write metadata without a local empty-params gate', async () => {
  const invoke = vi.fn().mockResolvedValue({ ok: true, provider: null, previous: null });
  const connection = createBackendConnection({ invoke }, { ...descriptor, methods: [
    { name: 'agent.provider.default', callable: true, state: 'ready', mutates: true },
  ] });
  await expect(connection.client.agent.provider.default({})).resolves.toMatchObject({ provider: null });
  await expect(connection.client.agent.provider.default({ clear: true })).resolves.toMatchObject({ provider: null });
  expect(invoke.mock.calls).toEqual([
    ['agent.provider.default', {}, { retry: 'never', catalogDigest: descriptor.catalogDigest }],
    ['agent.provider.default', { clear: true }, { retry: 'never', catalogDigest: descriptor.catalogDigest }],
  ]);
});

it('keeps a dispatched write unknown when its response is lost during connection replacement', async () => {
  let rejectWrite!: (reason: unknown) => void;
  const invoke = vi.fn(() => new Promise<never>((_resolve, reject) => { rejectWrite = reject; }));
  const connection = createBackendConnection({ invoke }, descriptor);
  const pending = connection.client.agent.provider.logout({ actor: 'actor', provider: 'pi' });
  const error = new TransportError('network', 'NETWORK_ERROR', 'response lost', {
    method: 'agent.provider.logout', requestId: 'write-g1', dispatch: 'unknown', status: null,
    catalogDigest: descriptor.catalogDigest, attempts: 1, startedAt: 1, durationMs: 1,
  });
  const rejection = expect(pending).rejects.toBe(error);
  connection.retire();
  rejectWrite(error);
  await rejection;
  expect(invoke).toHaveBeenCalledExactlyOnceWith('agent.provider.logout', { actor: 'actor', provider: 'pi' },
    { retry: 'never', catalogDigest: descriptor.catalogDigest });
});
