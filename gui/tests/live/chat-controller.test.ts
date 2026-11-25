import { LiveError, type LivePort } from '../../src/adapters/live';
import { ChatController } from '../../src/features/agent-chat';

const target = 'agent:test@example.test:node:one';
const otherSquire = 'agent:other@example.test:remote:squire';
const ownSquire = 'agent:test@example.test:node:squire';

it('selects only the trusted personal Squire when another person appears first', async () => {
  const call = vi.fn(async (operation: string, params: Record<string, unknown> = {}) => {
    if (operation === 'targets.list') return { targets: [otherSquire, ownSquire].map((targetUri) => ({ targetUri, status: 'online' })), preferredTargetUri: ownSquire };
    if (operation === 'sessions.pending') return { requests: [] };
    if (operation === 'sessions.create') return { sessionId: params.targetUri, conversationId: `c:${params.targetUri}`, targetUri: params.targetUri };
    if (operation === 'sessions.history') return { events: [], nextCursor: null };
    throw new Error(operation);
  });
  const c = new ChatController({ call }); await c.start();
  expect(c.getSnapshot().activeTargetId).toBe(ownSquire);
  expect(c.getSnapshot().targets.find((row) => row.id === otherSquire)?.role).toBe(otherSquire);
  expect(call).toHaveBeenCalledWith('sessions.create', { targetUri: ownSquire });
  expect(call).not.toHaveBeenCalledWith('sessions.create', { targetUri: otherSquire });
});

it('does not silently select a remote Agent when the personal Squire is absent', async () => {
  const call = vi.fn(async (operation: string) => {
    if (operation === 'targets.list') return { targets: [{ targetUri: otherSquire, status: 'online' }], preferredTargetUri: null };
    if (operation === 'sessions.pending') return { requests: [] };
    throw new Error(operation);
  });
  const c = new ChatController({ call }); await c.start();
  expect(c.getSnapshot().activeTargetId).toBeNull();
  expect(call).not.toHaveBeenCalledWith('sessions.create', expect.anything());
});
it('labels targets by displayName, otherwise by the full targetUri', async () => {
  const renamed = 'agent:test@example.test:node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa';
  const unnamed = 'agent:test@example.test:node:a.bbbbbbbbbbbbbbbbbbbbbbbbbb';
  const pendingTarget = 'agent:test@example.test:node:a.cccccccccccccccccccccccccc';
  const call = vi.fn(async (operation: string) => {
    if (operation === 'targets.list') {
      return { targets: [
        { targetUri: renamed, status: 'online', actor: renamed, agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa', displayName: 'reviewer' },
        { targetUri: unnamed, status: 'online', actor: 'worker', agentId: 'a.bbbbbbbbbbbbbbbbbbbbbbbbbb', displayName: null },
        { targetUri: target, status: 'online' },
      ], preferredTargetUri: null };
    }
    if (operation === 'sessions.pending') {
      return { requests: [{ sessionId: 's', targetUri: pendingTarget, status: 'unknown', request: { text: 'x', idempotencyKey: 'k' } }] };
    }
    throw new Error(operation);
  });
  const c = new ChatController({ call }); await c.start();
  expect(c.getSnapshot().targets.map((row) => [row.id, row.displayName])).toEqual([
    [renamed, 'reviewer'], [unnamed, unnamed], [target, target], [pendingTarget, pendingTarget],
  ]);
});

function backend() {
  let lost = true;
  let pending: unknown[] = [];
  const calls: { operation: string; params: Record<string, unknown> }[] = [];
  const call = vi.fn(async (operation: string, params: Record<string, unknown> = {}) => {
    calls.push({ operation, params });
    if (operation === 'targets.list') return { targets: [{ targetUri: target, nodeId: 'node', status: 'online' }], preferredTargetUri: target };
    if (operation === 'sessions.create') return { sessionId: 'session-1', conversationId: 'conversation-1', targetUri: target };
    if (operation === 'sessions.pending') return { requests: pending };
    if (operation === 'sessions.history') return { events: [], nextCursor: null };
    if (operation === 'sessions.send') {
      pending = [{ sessionId: 'session-1', targetUri: target, request: params.request, status: 'unknown' }];
      throw new LiveError('transport', '响应丢失', false);
    }
    if (operation === 'sessions.send.confirm') return { status: 'accepted', commandId: (params.request as { idempotencyKey: string }).idempotencyKey, conversationId: 'conversation-1', messageId: 'message-1' };
    if (operation === 'sessions.send.lookup') {
      if (lost) return { status: 'unknown', reason: 'not-found' };
      pending = [];
      return { status: 'accepted', commandId: (params.request as { idempotencyKey: string }).idempotencyKey, conversationId: 'conversation-1', messageId: 'message-1' };
    }
    throw new Error(operation);
  });
  return { port: { call } as LivePort, calls, delivered: () => { lost = false; } };
}

it('never resends unknown, preserves frozen request through reload, then clears matching draft on lookup', async () => {
  const b = backend();
  const first = new ChatController(b.port, () => 'original-key');
  await first.start();
  await first.select(target);
  first.setDraft(target, 'hello');
  await first.send(target, 'hello');
  expect(first.getSnapshot().entries[target].operation?.status).toBe('unknown');
  expect(first.getSnapshot().drafts[target]).toBe('hello');
  await first.send(target, 'hello');
  await first.retry('original-key');
  expect(b.calls.filter((c) => c.operation === 'sessions.send')).toHaveLength(1);
  const reloaded = new ChatController(b.port);
  await reloaded.start();
  expect(reloaded.getSnapshot().entries[target].operation?.requestId).toBe('original-key');
  expect(reloaded.getSnapshot().drafts[target]).toBe('hello');
  await reloaded.reconcile(target);
  expect(reloaded.getSnapshot().entries[target].operation?.status).toBe('unknown');
  b.delivered();
  await reloaded.reconcile(target);
  expect(reloaded.getSnapshot().entries[target].operation).toMatchObject({ status: 'success', confirmationPending: false });
  expect(b.calls.filter((c) => c.operation === 'sessions.send.confirm')).toHaveLength(1);
  expect(reloaded.getSnapshot().drafts[target]).toBe('');
  expect(b.calls.filter((c) => c.operation === 'sessions.send')).toHaveLength(1);
});

it('does not clear newer edits after original request is accepted', async () => {
  const b = backend(); const c = new ChatController(b.port, () => 'key');
  await c.start(); await c.select(target); c.setDraft(target, 'first'); await c.send(target, 'first');
  c.setDraft(target, 'next draft'); b.delivered(); await c.reconcile(target);
  expect(c.getSnapshot().drafts[target]).toBe('next draft');
});

it('keeps lookup failures unknown even when the read rejection is definitive', async () => {
  const b = backend(); const c = new ChatController(b.port, () => 'key');
  await c.start(); await c.select(target); c.setDraft(target, 'draft'); await c.send(target, 'draft');
  vi.mocked(b.port.call).mockRejectedValueOnce(new LiveError('unsupported', 'no lookup', true, true));
  await c.reconcile(target);
  expect(c.getSnapshot().entries[target].operation).toMatchObject({ status: 'unknown', canRetry: false });
});

it('rejects a mismatched accepted response without releasing the send lock', async () => {
  const b = backend(); const c = new ChatController(b.port, () => 'key');
  await c.start(); await c.select(target);
  vi.mocked(b.port.call).mockResolvedValueOnce({ status: 'accepted', commandId: 'other-key', conversationId: 'conversation-1', messageId: 'message' });
  await c.send(target, 'draft');
  expect(c.getSnapshot().entries[target].operation?.status).toBe('unknown');
});

it('isolates slow history responses from the newly selected target and deduplicates page boundaries', async () => {
  const second = 'agent:test@example.test:node:two';
  let release: ((value: unknown) => void) | undefined;
  const call = vi.fn(async (operation: string, params: Record<string, unknown> = {}) => {
    if (operation === 'targets.list') return { targets: [target, second].map((targetUri) => ({ targetUri, status: 'online' })), preferredTargetUri: target };
    if (operation === 'sessions.pending') return { requests: [] };
    if (operation === 'sessions.create') return { sessionId: params.targetUri, conversationId: `c:${params.targetUri}`, targetUri: params.targetUri };
    if (operation === 'sessions.history') {
      if (params.sessionId === target) return new Promise((resolve) => { release = resolve; });
      const args = params.args as { cursor?: string };
      const event = { eventId: 'second-event', kind: 'assistant', text: 'second reply', at: '2026-10-29T00:00:00Z' };
      return { events: [event], nextCursor: args.cursor ? null : 'page2' };
    }
    throw new Error(operation);
  });
  const c = new ChatController({ call }); const start = c.start();
  await vi.waitFor(() => expect(release).toBeDefined());
  await c.select(second);
  release!({ events: [{ eventId: 'first-event', kind: 'assistant', text: 'old target', at: '2026-10-29T00:00:00Z' }], nextCursor: null });
  await start;
  expect(c.getSnapshot().activeTargetId).toBe(second);
  expect(c.getSnapshot().entries[second].messages).toHaveLength(1);
  expect(c.getSnapshot().entries[second].messages[0].text).toBe('second reply');
});

it('only retries an explicitly permitted definitive refusal with the frozen original request', async () => {
  const b = backend(), c = new ChatController(b.port, () => 'retry-key');
  await c.start(); await c.select(target);
  vi.mocked(b.port.call).mockRejectedValueOnce(new LiveError('not-ready', 'refused before effect', true, true));
  c.setDraft(target, 'original'); await c.send(target, 'original'); c.setDraft(target, 'edited');
  vi.mocked(b.port.call).mockResolvedValueOnce({ status: 'accepted', commandId: 'retry-key', conversationId: 'conversation-1', messageId: 'message-1' });
  await c.retry('retry-key');
  expect(b.port.call).toHaveBeenLastCalledWith('sessions.history', expect.any(Object));
  expect(vi.mocked(b.port.call).mock.calls.filter(([name]) => name === 'sessions.send').map(([, params]) => params?.request))
    .toEqual([{ text: 'original', idempotencyKey: 'retry-key' }, { text: 'original', idempotencyKey: 'retry-key' }]);
  expect(c.getSnapshot().drafts[target]).toBe('edited');
});

it('allows explicit editing after definitive refusal with a new key, but never resets an unknown result', async () => {
  const b = backend(); let serial = 0;
  const c = new ChatController(b.port, () => `key-${++serial}`);
  await c.start(); await c.select(target);
  vi.mocked(b.port.call).mockRejectedValueOnce(new LiveError('invalid', 'refused', true, false));
  c.setDraft(target, 'draft'); await c.send(target, 'draft');
  c.resetFailure(target);
  expect(c.getSnapshot().drafts[target]).toBe('draft');
  expect(c.getSnapshot().entries[target].operation).toBeUndefined();
  c.setDraft(target, 'corrected'); await c.send(target, 'corrected');
  expect(c.getSnapshot().entries[target].operation?.requestId).toBe('key-2');
  expect(c.getSnapshot().entries[target].operation?.status).toBe('unknown');
  c.resetFailure(target);
  expect(c.getSnapshot().entries[target].operation?.status).toBe('unknown');
});
it('continues bounded history chunks until a 4001st reply is visible and polls only the final page thereafter', async () => {
  const events = Array.from({ length: 4001 }, (_, i) => ({ eventId: `event-${String(i).padStart(5, '0')}`, kind: 'assistant', text: `message-${i}`, at: '2026-10-29T00:00:00Z' }));
  const starts: number[] = [];
  const call = vi.fn(async (operation: string, params: Record<string, unknown> = {}): Promise<unknown> => {
    if (operation === 'targets.list') return { targets: [{ targetUri: target, status: 'online' }], preferredTargetUri: target };
    if (operation === 'sessions.pending') return { requests: [] };
    if (operation === 'sessions.create') return { sessionId: 'session', conversationId: 'conversation', targetUri: target };
    if (operation === 'sessions.history') {
      const args = params.args as { cursor?: string; limit: number }; const start = Number(args.cursor ?? 0);
      starts.push(start); const end = Math.min(events.length, start + args.limit);
      return { events: events.slice(start, end), nextCursor: end < events.length ? String(end) : null };
    }
    throw new Error(operation);
  });
  const c = new ChatController({ call }); await c.start();
  for (let i = 0; i < 12; i++) await c.poll();
  expect(c.getSnapshot().entries[target].messages).toHaveLength(4001);
  expect(c.getSnapshot().entries[target].messages.at(-1)?.text).toBe('message-4000');
  const count = starts.length; await c.poll();
  expect(starts.slice(count)).toHaveLength(1);
  expect(starts.at(-1)).toBeGreaterThan(0);
});
it('confirms a validated accepted receipt before allowing another send; failed confirmation never erases acceptance', async () => {
  const b = backend(), c = new ChatController(b.port, () => 'accepted-key');
  await c.start(); await c.select(target); c.setDraft(target, 'accepted message');
  const receipt = { status: 'accepted', commandId: 'accepted-key', conversationId: 'conversation-1', messageId: 'accepted-id' };
  vi.mocked(b.port.call).mockResolvedValueOnce(receipt).mockRejectedValueOnce(new LiveError('transport', 'confirmation lost'));
  await c.send(target, 'accepted message');
  expect(c.getSnapshot().entries[target].operation).toMatchObject({ status: 'success', confirmationPending: true });
  expect(c.getSnapshot().drafts[target]).toBe('');
  c.setDraft(target, 'next'); await c.send(target, 'next');
  expect(vi.mocked(b.port.call).mock.calls.filter(([op]) => op === 'sessions.send')).toHaveLength(1);
  vi.mocked(b.port.call).mockResolvedValueOnce(receipt);
  await c.confirmAcceptance(target);
  expect(c.getSnapshot().entries[target].operation).toMatchObject({ status: 'success', confirmationPending: false });
  expect(vi.mocked(b.port.call).mock.calls.filter(([op]) => op === 'sessions.send.confirm')).toHaveLength(2);
  expect(vi.mocked(b.port.call).mock.calls.filter(([op]) => op === 'sessions.send')).toHaveLength(1);
});
