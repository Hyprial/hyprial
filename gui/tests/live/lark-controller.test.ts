import { LarkController, LiveError, type LivePort } from '../../src/adapters/live';
const agent = 'agent:a@b:node:one';
const auth = { adapter: 'lark-main', appId: 'app', authorization: 'authorized', capabilities: [], checkedAt: 1 };
const snapshot = (actor: string | null = null) => ({ adapter: 'lark-main', bindingAvailable: true, authorization: auth, binding: { actor }, runtime: { online: true } });
function backend() {
  let current = snapshot();
  let clear: 'lost-after-clear' | 'lost-unchanged' | 'lost-third-party' | 'success' = 'lost-after-clear';
  const call = vi.fn(async (operation: string, params: Record<string, unknown> = {}): Promise<unknown> => {
    if (operation === 'lark.adapters') return { adapters: [current] };
    if (operation === 'lark.authorization.start') return { operationId: 'auth-1', adapter: 'lark-main', appId: 'app', status: 'awaiting_user', authorization: auth, verificationUrl: 'https://accounts.feishu.cn/device', expiresAt: 2_000_000_000 };
    if (operation === 'lark.authorization.cancel') return { operationId: 'auth-1', adapter: 'lark-main', appId: 'app', status: 'cancelled', authorization: auth };
    if (operation === 'lark.binding.set') { current = snapshot(params.agentId as string); throw new LiveError('transport', 'lost response'); }
    if (operation === 'lark.binding.clear') {
      if (clear === 'success') { current = snapshot(); return current; }
      if (clear === 'lost-after-clear') current = snapshot();
      if (clear === 'lost-third-party') current = snapshot('agent:third@b:node:other');
      throw new LiveError('transport', 'lost response');
    }
    throw new Error(operation);
  });
  return { port: { call } as LivePort, call,
    setActor: (actor: string) => { current = snapshot(actor); },
    setClear: (value: typeof clear) => { clear = value; } };
}
it('requires an explicit frozen confirmation and does not retry a binding with an unknown response', async () => {
  const b = backend(), c = new LarkController(b.port);
  await c.refresh(); c.chooseAgent(agent); c.prepareSwitch();
  expect(c.getSnapshot().confirmation).toMatchObject({ adapter: 'lark-main', expectedActor: null, agentId: agent });
  await c.confirmSwitch();
  expect(b.call).toHaveBeenCalledWith('lark.binding.set', { adapter: 'lark-main', agentId: agent, expectedActor: null });
  expect(c.getSnapshot().switchOperations['lark-main']?.status).toBe('unknown');
  c.prepareSwitch(); await c.confirmSwitch();
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.binding.set')).toHaveLength(1);
  await c.refresh();
  expect(c.getSnapshot().switchOperations['lark-main']?.status).toBe('success');
});
it('keeps confirmation expectedActor frozen if the binding changes before confirmation', async () => {
  const b = backend(), c = new LarkController(b.port); await c.refresh(); c.chooseAgent(agent); c.prepareSwitch();
  b.setActor('agent:other@b:node:other'); await c.refresh();
  await c.confirmSwitch();
  expect(b.call).toHaveBeenCalledWith('lark.binding.set', expect.objectContaining({ expectedActor: null }));
});
it('reconciles a lost unbind response to success without replaying the write', async () => {
  const b = backend(); b.setActor(agent);
  const c = new LarkController(b.port); await c.refresh(); c.prepareUnbind();
  expect(c.getSnapshot().unbindConfirmation).toEqual({ adapter: 'lark-main', expectedActor: agent });
  await c.confirmUnbind();
  expect(c.getSnapshot().unbindOperations['lark-main']?.status).toBe('success');
  expect(c.getSnapshot().adapters[0].binding.actor).toBeNull();
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.binding.clear')).toHaveLength(1);
});
it('keeps unchanged unknown unbind retryable only through fresh confirmation', async () => {
  const b = backend(); b.setActor(agent); b.setClear('lost-unchanged');
  const c = new LarkController(b.port); await c.refresh(); c.prepareUnbind(); await c.confirmUnbind();
  expect(c.getSnapshot().unbindOperations['lark-main']?.status).toBe('unknown');
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.binding.clear')).toHaveLength(1);
  c.retryUnbind();
  expect(c.getSnapshot().unbindConfirmation).toEqual({ adapter: 'lark-main', expectedActor: agent });
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.binding.clear')).toHaveLength(1);
  b.setClear('success'); await c.confirmUnbind();
  expect(c.getSnapshot().unbindOperations['lark-main']?.status).toBe('success');
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.binding.clear')).toHaveLength(2);
});
it('resolves an unknown unbind to definitive failure when a third Agent owns the binding', async () => {
  const b = backend(); b.setActor(agent); b.setClear('lost-third-party');
  const c = new LarkController(b.port); await c.refresh(); c.prepareUnbind(); await c.confirmUnbind();
  expect(c.getSnapshot().unbindOperations['lark-main']).toMatchObject({
    status: 'definitive-failure', error: '绑定已变化，请刷新后重新确认。',
  });
  c.retryUnbind();
  expect(c.getSnapshot().unbindConfirmation).toBeUndefined();
});
it('keeps switch and unbind mutations mutually exclusive', async () => {
  const b = backend(); b.setActor(agent); b.setClear('lost-unchanged');
  const c = new LarkController(b.port); await c.refresh(); c.chooseAgent('agent:next@b:node:two');
  c.prepareUnbind(); c.prepareSwitch();
  expect(c.getSnapshot().confirmation).toBeUndefined();
  c.dismissUnbind(); c.prepareSwitch(); c.prepareUnbind();
  expect(c.getSnapshot().unbindConfirmation).toBeUndefined();
  c.dismissConfirmation(); c.prepareUnbind(); await c.confirmUnbind(); c.prepareSwitch();
  expect(c.getSnapshot().confirmation).toBeUndefined();
  c.retryUnbind(); await c.confirmUnbind();
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.binding.set')).toHaveLength(0);
});
it('cancelling a new authorization does not erase prior grants', async () => {
  const b = backend(), c = new LarkController(b.port); await c.refresh(); c.chooseAgent(agent);
  await c.authorize(); await c.cancelAuthorization();
  const state = c.getSnapshot();
  expect(state.authorizationOperations['lark-main']?.status).toBe('cancelled');
  expect(state.adapters[0].authorization.authorization).toBe('authorized');
});
it('rejects unsafe authorization URLs as unknown without opening them', async () => {
  const b = backend(), c = new LarkController(b.port); await c.refresh(); c.chooseAgent(agent);
  b.call.mockResolvedValueOnce({ operationId: 'bad', adapter: 'lark-main', appId: 'app', status: 'awaiting_user', authorization: auth, verificationUrl: 'javascript:alert(1)', expiresAt: 2_000_000_000 });
  await c.authorize();
  expect(c.getSnapshot().authorizationOperations['lark-main']?.status).toBe('unknown');
  expect(c.getSnapshot().authorizationOperations['lark-main']?.verificationUrl).toBeUndefined();
});
it('does not let a slow pre-authorization inventory overwrite a newer grant check', async () => {
  const b = backend(), c = new LarkController(b.port); await c.refresh(); c.chooseAgent(agent);
  let release!: (value: unknown) => void;
  b.call.mockImplementationOnce(async () => new Promise((resolve) => { release = resolve; }));
  const stale = c.refresh();
  b.call.mockResolvedValueOnce({ operationId: 'auth-1', adapter: 'lark-main', appId: 'app', status: 'succeeded', authorization: auth });
  await c.authorize();
  release({ adapters: [{ ...snapshot(), authorization: { ...auth, authorization: 'unauthorized' } }] });
  await stale;
  expect(c.getSnapshot().adapters[0].authorization.authorization).toBe('authorized');
});
it('recovers an existing authorization after reload without starting another one', async () => {
  const b = backend();
  b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), operation: { operationId: 'existing', adapter: 'lark-main', appId: 'app', status: 'awaiting_user', authorization: auth, verificationUrl: 'https://accounts.feishu.cn/device', expiresAt: 2_000_000_000 } }] });
  const c = new LarkController(b.port); await c.refresh(); c.chooseAgent(agent);
  expect(c.getSnapshot().authorizationOperations['lark-main']?.operationId).toBe('existing');
  await c.authorize();
  expect(b.call.mock.calls.some(([operation]) => operation === 'lark.authorization.start')).toBe(false);
});
it('does not offer a binding write without positive conditional-write capability', async () => {
  const b = backend(); b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), bindingAvailable: false }] });
  const c = new LarkController(b.port); await c.refresh(); c.chooseAgent(agent); c.prepareSwitch();
  expect(c.getSnapshot().confirmation).toBeUndefined();
});
it('keeps newer revoked grants when authorization progress contains an older grant snapshot', async () => {
  const b = backend(), c = new LarkController(b.port);
  const existing = { operationId: 'old', adapter: 'lark-main', appId: 'app', status: 'awaiting_user', authorization: auth };
  b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), operation: existing }] });
  await c.refresh();
  b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), authorization: { ...auth, checkedAt: 2, authorization: 'unauthorized' }, operation: existing }] });
  await c.refresh(); b.call.mockResolvedValueOnce(existing); await c.pollAuthorization();
  expect(c.getSnapshot().adapters[0].authorization).toMatchObject({ checkedAt: 2, authorization: 'unauthorized' });
});
it('drops an old App authorization operation when the configured App changes', async () => {
  const b = backend(), c = new LarkController(b.port);
  const existing = { operationId: 'old', adapter: 'lark-main', appId: 'app', status: 'succeeded', authorization: auth };
  b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), operation: existing }] }); await c.refresh();
  b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), authorization: { ...auth, checkedAt: 2, appId: 'new-app', authorization: 'unauthorized' }, operation: existing }] }); await c.refresh();
  expect(c.getSnapshot().authorizationOperations['lark-main']).toBeUndefined();
});

function cleanupBackend() {
  const current = { ...snapshot(), authorization: { ...auth, appId: 'app-b', authorization: 'unauthorized', checkedAt: 2 } };
  const retained = { operationId: 'old-a', adapter: 'lark-main', appId: 'app-a', status: 'unknown',
    errorCode: 'WORKER_CLEANUP_UNCONFIRMED', authorization: { ...auth, appId: 'app-a' } };
  let cleanupConfirmed = false;
  let cancelled = false;
  const call = vi.fn(async (operation: string): Promise<unknown> => {
    if (operation === 'lark.adapters') return { adapters: [{ ...current, operation: { ...retained, status: cancelled ? 'cancelled' : 'unknown' } }] };
    if (operation === 'lark.authorization.start') return cancelled
      ? { operationId: 'new-b', adapter: 'lark-main', appId: 'app-b', status: 'awaiting_user', authorization: current.authorization,
        verificationUrl: 'https://accounts.feishu.cn/device', expiresAt: 2_000_000_000 } : retained;
    if (operation === 'lark.authorization.cancel') { cancelled = cleanupConfirmed; return { ...retained, status: cancelled ? 'cancelled' : 'unknown' }; }
    if (operation === 'lark.authorization.operation') return retained;
    throw new Error(operation);
  });
  // Initial inventory may precede the host noticing that the old worker remains.
  call.mockResolvedValueOnce({ adapters: [current] });
  return { call, allowCleanup: () => { cleanupConfirmed = true; } };
}
it('retains old App cleanup custody after a new App start response and permits explicit new authorization only after cancellation', async () => {
  const b = cleanupBackend(), c = new LarkController({ call: b.call });
  await c.refresh(); await c.authorize();
  expect(c.getSnapshot().authorizationOperations['lark-main']).toMatchObject({ operationId: 'old-a', status: 'unknown' });
  expect(c.getSnapshot().adapters[0].authorization).toMatchObject({ appId: 'app-b', authorization: 'unauthorized', checkedAt: 2 });
  await c.authorize();
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.authorization.start')).toHaveLength(1);
  b.allowCleanup(); await c.cancelAuthorization();
  expect(b.call).toHaveBeenCalledWith('lark.authorization.cancel', { operationId: 'old-a' });
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.authorization.start')).toHaveLength(1);
  await c.authorize();
  expect(c.getSnapshot().authorizationOperations['lark-main']).toMatchObject({ operationId: 'new-b', status: 'awaiting_user' });
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.authorization.start')).toHaveLength(2);
});
it('keeps failed old App cleanup recoverable across polling, inventory refresh and reload without starting the new App', async () => {
  const b = cleanupBackend(), c = new LarkController({ call: b.call });
  await c.refresh(); await c.authorize(); await c.cancelAuthorization(); await c.poll(); await c.authorize();
  expect(c.getSnapshot().authorizationOperations['lark-main']).toMatchObject({ operationId: 'old-a', status: 'unknown' });
  expect(c.getSnapshot().adapters[0].authorization.appId).toBe('app-b');
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.authorization.start')).toHaveLength(1);
  const restored = new LarkController({ call: b.call }); await restored.refresh(); await restored.authorize();
  expect(restored.getSnapshot().authorizationOperations['lark-main']).toMatchObject({ operationId: 'old-a', status: 'unknown' });
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.authorization.start')).toHaveLength(1);
  b.allowCleanup(); await restored.cancelAuthorization(); await restored.authorize();
  expect(restored.getSnapshot().authorizationOperations['lark-main']?.operationId).toBe('new-b');
});
it('preserves old App identity and cleanup custody when cancellation transport fails', async () => {
  const b = cleanupBackend(), c = new LarkController({ call: b.call });
  await c.refresh(); await c.authorize();
  b.call.mockRejectedValueOnce(new LiveError('transport', 'lost cancellation response'));
  await c.cancelAuthorization();
  expect(c.getSnapshot().authorizationOperations['lark-main']).toMatchObject({ operationId: 'old-a', status: 'unknown', authorization: { appId: 'app-a' } });
  await c.authorize();
  expect(b.call.mock.calls.filter(([op]) => op === 'lark.authorization.start')).toHaveLength(1);
  b.allowCleanup(); await c.cancelAuthorization(); await c.authorize();
  expect(c.getSnapshot().authorizationOperations['lark-main']?.operationId).toBe('new-b');
});
it('ignores a delayed old App cancellation after inventory has observed a newer operation', async () => {
  const b = cleanupBackend(), c = new LarkController({ call: b.call });
  await c.refresh(); await c.authorize();
  const old = c.getSnapshot().authorizationOperations['lark-main'];
  let release!: (value: unknown) => void;
  b.call.mockImplementationOnce(async () => new Promise((resolve) => { release = resolve; }));
  const cancelling = c.cancelAuthorization();
  const current = { ...auth, appId: 'app-b', authorization: 'unauthorized', checkedAt: 2 };
  b.call.mockResolvedValueOnce({ adapters: [{ ...snapshot(), authorization: current,
    operation: { operationId: 'new-b', adapter: 'lark-main', appId: 'app-b', status: 'awaiting_user', authorization: current } }] });
  await c.refresh();
  release({ ...old, appId: 'app-a', status: 'cancelled' }); await cancelling;
  expect(c.getSnapshot().authorizationOperations['lark-main']).toMatchObject({ operationId: 'new-b', status: 'awaiting_user' });
  expect(c.getSnapshot().adapters[0].authorization).toMatchObject({ appId: 'app-b', authorization: 'unauthorized' });
});
