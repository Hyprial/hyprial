import { describe, expect, it, vi } from 'vitest';
import {
  attemptView,
  createFirstRunReplyActions,
  type FirstRunReplyClient,
} from '../src/features/onboarding/impl/reply-actions';
import type { ChatSendApi } from '../src/features/chat-channels';
import type { MethodAccess } from '../src/client';

const ATTEMPT = {
  attemptId: 'att-0123456789abcdef',
  revision: 2,
  state: 'pending',
  operationKey: 'opkey-0123456789abcdef',
  sendKey: 'frs-0123456789abcdef0123456789abcdef',
  receiptRevision: 0,
  loginEpoch: 3,
  agentUri: 'agent:alice@node:first-run',
  agentCreatedAtMs: 1760000000000,
  provider: 'deepseek',
  model: 'deepseek-flash',
  harness: 'pi',
  configRevision: null,
  credentialKind: 'key',
  requestMessageId: null,
  conversationId: null,
  replyMessageId: null,
} as const;

const BOUND = {
  ...ATTEMPT,
  revision: 3,
  state: 'bound',
  requestMessageId: 'm.request-0123456789',
  conversationId: 'c.0123456789abcdef',
} as const;

const CONFIRMED = {
  ...BOUND,
  revision: 4,
  state: 'confirmed',
  receiptRevision: 0,
  replyMessageId: 'm.reply-0123456789',
} as const;

const RECEIPT = {
  state: 'recorded',
  revision: 1,
  confirmed: ['configuration', 'reply'],
  updatedAt: '2026-11-18T00:00:00Z',
} as const;

function client(overrides: Partial<{
  begin: ReturnType<typeof vi.fn>;
  bind: ReturnType<typeof vi.fn>;
  confirm: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
  history: ReturnType<typeof vi.fn>;
}> = {}) {
  return {
    onboarding: {
      firstRun: {
        begin: overrides.begin ?? vi.fn(async () => ({ ok: true, attempt: ATTEMPT })),
        bind: overrides.bind ?? vi.fn(async () => ({ ok: true, attempt: BOUND })),
        confirm: overrides.confirm ?? vi.fn(async () => ({
          ok: true, attempt: CONFIRMED, firstRun: RECEIPT,
          requestMessageId: BOUND.requestMessageId, replyMessageId: CONFIRMED.replyMessageId,
        })),
        cancel: overrides.cancel ?? vi.fn(async () => ({
          ok: true, attempt: { ...BOUND, revision: 4, state: 'cancelled' },
        })),
      },
    },
    message: {
      history: overrides.history ?? vi.fn(async () => ({ entries: [] })),
    },
  };
}

function chat() {
  return {
    sendWithKey: vi.fn<ChatSendApi['sendWithKey']>(async () => ({
      status: 'accepted' as const,
      operationKey: ATTEMPT.sendKey,
      conversationId: BOUND.conversationId,
      messageId: BOUND.requestMessageId,
      notice: '消息已提交。',
    })),
    recoverKey: vi.fn<ChatSendApi['recoverKey']>(async (operationKey) => ({
      status: 'unknown' as const,
      operationKey,
      reason: 'not-found' as const,
      notice: '未找到。',
    })),
  };
}

function actionsFor(
  api: ReturnType<typeof client>,
  transport: ReturnType<typeof chat> = chat(),
  access?: MethodAccess,
) {
  return createFirstRunReplyActions(
    api as unknown as FirstRunReplyClient,
    transport as unknown as Pick<ChatSendApi, 'sendWithKey' | 'recoverKey'>,
    access,
  );
}

describe('attempt projection', () => {
  it('accepts the server attempt and keeps the server operation key', () => {
    expect(attemptView(ATTEMPT)).toEqual(ATTEMPT);
  });

  it('refuses a malformed attempt instead of treating it as begun', () => {
    const { operationKey: _omitted, ...missingKey } = ATTEMPT;
    expect(attemptView(missingKey)).toBeNull();
    expect(attemptView({ ...ATTEMPT, revision: 0 })).toBeNull();
    expect(attemptView({ ...ATTEMPT, credentialKind: 'other' })).toBeNull();
    expect(attemptView(null)).toBeNull();
  });
});

describe('begin', () => {
  it('begins with the selected Agent and a caller operation key', async () => {
    const api = client();
    const actions = actionsFor(api);
    const result = await actions.begin(ATTEMPT.agentUri);
    expect(result).toMatchObject({ status: 'begun', attempt: ATTEMPT });
    const params = api.onboarding.firstRun.begin.mock.calls[0][0];
    expect(params.agentUri).toBe(ATTEMPT.agentUri);
    expect(params.operationKey.length).toBeGreaterThanOrEqual(16);
  });

  it('refuses before dispatch when the catalog does not admit the method', async () => {
    const api = client();
    const access: MethodAccess = method => method === 'onboarding.firstRun.begin'
      ? { available: false, code: 'METHOD_NOT_CALLABLE' }
      : { available: true };
    const actions = actionsFor(api, chat(), access);
    const result = await actions.begin(ATTEMPT.agentUri);
    expect(result.status).toBe('unavailable');
    expect(api.onboarding.firstRun.begin).not.toHaveBeenCalled();
  });

  it('keeps an unverifiable begin unknown', async () => {
    const api = client({ begin: vi.fn(async () => ({ ok: true, attempt: { ...ATTEMPT, sendKey: '' } })) });
    const actions = actionsFor(api);
    expect((await actions.begin(ATTEMPT.agentUri)).status).toBe('unknown');
  });

  it('reuses the same begin operation key after an unknown begin', async () => {
    const begin = vi.fn(async (..._args: unknown[]) => { throw { dispatch: 'unknown' }; });
    const actions = actionsFor(client({ begin }));
    expect((await actions.begin(ATTEMPT.agentUri)).status).toBe('unknown');
    expect((await actions.begin(ATTEMPT.agentUri)).status).toBe('unknown');
    const keys = begin.mock.calls.map(call => (call[0] as { operationKey: string }).operationKey);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    await actions.begin('agent:owner@other:node:first-run');
    const changed = begin.mock.calls[2][0] as { operationKey: string };
    expect(changed.operationKey).not.toBe(keys[0]);
  });

  it('mints a new begin key after a definitive begin failure', async () => {
    const begin = vi.fn(async (..._args: unknown[]) => { throw { dispatch: 'not-dispatched', code: 'INVALID' }; });
    const actions = actionsFor(client({ begin }));
    expect((await actions.begin(ATTEMPT.agentUri)).status).toBe('failed');
    expect((await actions.begin(ATTEMPT.agentUri)).status).toBe('failed');
    const keys = begin.mock.calls.map(call => (call[0] as { operationKey: string }).operationKey);
    expect(keys[1]).not.toBe(keys[0]);
  });
});

describe('send', () => {
  it('sends with the server send key and binds the accepted request', async () => {
    const api = client();
    const transport = chat();
    const actions = actionsFor(api, transport);
    const result = await actions.send(attemptView(ATTEMPT)!, '你好');
    expect(transport.sendWithKey).toHaveBeenCalledWith(
      expect.objectContaining({ to: [ATTEMPT.agentUri], message: '你好' }),
      ATTEMPT.sendKey,
    );
    expect(api.onboarding.firstRun.bind).toHaveBeenCalledWith({
      attemptId: ATTEMPT.attemptId,
      expectedRevision: ATTEMPT.revision,
      operationKey: ATTEMPT.operationKey,
      conversationId: BOUND.conversationId,
      requestMessageId: BOUND.requestMessageId,
    });
    expect(result).toMatchObject({ status: 'bound', attempt: BOUND });
  });

  it('binds an existing durable send instead of dispatching again', async () => {
    const api = client();
    const transport = chat();
    transport.recoverKey.mockResolvedValueOnce({
      status: 'accepted', operationKey: ATTEMPT.sendKey,
      conversationId: BOUND.conversationId, messageId: BOUND.requestMessageId, notice: '已有记录。',
    });
    const actions = actionsFor(api, transport);
    const result = await actions.send(attemptView(ATTEMPT)!, '你好');
    expect(transport.sendWithKey).not.toHaveBeenCalled();
    expect(result.status).toBe('bound');
  });

  it('keeps an unknown dispatch unknown and never resends automatically', async () => {
    const api = client();
    const transport = chat();
    transport.sendWithKey.mockResolvedValueOnce({
      status: 'unknown', operationKey: ATTEMPT.sendKey,
      reason: 'dispatch-unknown', notice: '未知。',
    });
    const actions = actionsFor(api, transport);
    const result = await actions.send(attemptView(ATTEMPT)!, '你好');
    expect(transport.sendWithKey).toHaveBeenCalledTimes(1);
    expect(api.onboarding.firstRun.bind).not.toHaveBeenCalled();
    expect(result.status).toBe('unknown');
  });

  it('binds from key lookup after a lost response', async () => {
    const api = client();
    const transport = chat();
    transport.recoverKey
      .mockResolvedValueOnce({ status: 'unknown', operationKey: ATTEMPT.sendKey, reason: 'not-found', notice: '' })
      .mockResolvedValueOnce({
        status: 'accepted', operationKey: ATTEMPT.sendKey,
        conversationId: BOUND.conversationId, messageId: BOUND.requestMessageId, notice: '',
      });
    transport.sendWithKey.mockResolvedValueOnce({
      status: 'unknown', operationKey: ATTEMPT.sendKey,
      reason: 'dispatch-unknown', notice: '',
    });
    const actions = actionsFor(api, transport);
    const result = await actions.send(attemptView(ATTEMPT)!, '你好');
    expect(result.status).toBe('bound');
  });
});

describe('independent recovery and association probes', () => {
  it('does not dispatch a second send while the first result remains unknown', async () => {
    const transport = chat();
    transport.sendWithKey.mockResolvedValue({
      status: 'unknown', operationKey: ATTEMPT.sendKey,
      reason: 'dispatch-unknown', notice: '未知。',
    });
    const actions = actionsFor(client(), transport);
    await actions.send(attemptView(ATTEMPT)!, '你好');
    await actions.send(attemptView(ATTEMPT)!, '你好');
    expect(transport.sendWithKey).toHaveBeenCalledTimes(1);
  });

  it('only looks up a restored attempt and never dispatches it', async () => {
    const transport = chat();
    const actions = actionsFor(client(), transport);
    const result = await actions.send(attemptView(ATTEMPT)!, '你好', { dispatch: false });
    expect(transport.recoverKey).toHaveBeenCalledTimes(1);
    expect(transport.sendWithKey).not.toHaveBeenCalled();
    expect(result.status).toBe('unknown');
  });

  it.each([
    { attempt: { ...CONFIRMED, attemptId: 'att-different-0123456789' } },
    { attempt: { ...CONFIRMED, receiptRevision: 1 } },
    { firstRun: { ...RECEIPT, confirmed: ['reply'] } },
    { firstRun: { ...RECEIPT, revision: 2 } },
    { requestMessageId: 'm.different-request-0123456789' },
    { replyMessageId: 'm.different-reply-0123456789' },
  ])('rejects a confirmation that does not prove this complete attempt: %j', async (override) => {
    const api = client({ confirm: vi.fn(async () => ({
      ok: true, attempt: CONFIRMED, firstRun: RECEIPT,
      requestMessageId: BOUND.requestMessageId, replyMessageId: CONFIRMED.replyMessageId,
      ...override,
    })) });
    expect((await actionsFor(api).confirm(attemptView(BOUND)!, 0)).status).toBe('unknown');
  });
});

describe('conversation history', () => {
  it('maps the exact conversation entries and keeps only real bodies', async () => {
    const history = vi.fn(async () => ({ entries: [
      { messageId: 'm.request', from: 'user:alice', message: '你好', messageClass: 'interaction' },
      { messageId: 'm.reply', from: ATTEMPT.agentUri, message: '真实回复', messageClass: 'interaction' },
      { messageId: 'm.system', from: 'node:local:system', message: '系统提示', messageClass: 'system' },
      { messageId: 'm.empty', from: 'user:alice' },
      { messageId: 'm.bad', from: 7, message: 'x' },
    ] }));
    const actions = actionsFor(client({ history }));
    await expect(actions.history(attemptView(BOUND)!)).resolves.toEqual([
      { id: 'm.request', author: 'user', text: '你好' },
      { id: 'm.reply', author: 'agent', text: '真实回复' },
      { id: 'm.system', author: 'system', text: '系统提示' },
    ]);
    expect(history).toHaveBeenCalledWith({ conversationId: BOUND.conversationId });
  });

  it('returns no history on an unbound attempt or a read failure', async () => {
    const history = vi.fn(async () => { throw new Error('read failed'); });
    const actions = actionsFor(client({ history }));
    await expect(actions.history(attemptView(ATTEMPT)!)).resolves.toEqual([]);
    await expect(actions.history(attemptView(BOUND)!)).resolves.toEqual([]);
    expect(history).toHaveBeenCalledTimes(1);
  });
});

describe('confirm and cancel', () => {
  it('completes only when the server receipt says both steps are recorded', async () => {
    const api = client();
    const actions = actionsFor(api);
    const result = await actions.confirm(attemptView(BOUND)!, 0);
    expect(api.onboarding.firstRun.confirm).toHaveBeenCalledWith({
      attemptId: BOUND.attemptId,
      expectedAttemptRevision: BOUND.revision,
      expectedReceiptRevision: 0,
      operationKey: BOUND.operationKey,
    });
    expect(result).toMatchObject({ status: 'confirmed', firstRun: RECEIPT });
  });

  it('does not accept a confirm response whose attempt is still bound', async () => {
    const api = client({ confirm: vi.fn(async () => ({
      ok: true, attempt: BOUND, firstRun: { ...RECEIPT, confirmed: ['configuration'] },
      requestMessageId: BOUND.requestMessageId, replyMessageId: 'm.reply-0123456789',
    })) });
    const actions = actionsFor(api);
    expect((await actions.confirm(attemptView(BOUND)!, 0)).status).toBe('unknown');
  });

  it('reports an unverified reply as failed, not complete', async () => {
    const api = client({ confirm: vi.fn(async () => {
      throw { code: 'FIRST_RUN_REPLY_UNVERIFIED', dispatch: 'dispatched' };
    }) });
    const actions = actionsFor(api);
    const result = await actions.confirm(attemptView(BOUND)!, 0);
    expect(result.status).toBe('failed');
    expect(result.message).toContain('FIRST_RUN_REPLY_UNVERIFIED');
  });

  it('cannot confirm a pending attempt without a bound request', async () => {
    const api = client();
    const actions = actionsFor(api);
    expect((await actions.confirm(attemptView(ATTEMPT)!, 0)).status).toBe('failed');
    expect(api.onboarding.firstRun.confirm).not.toHaveBeenCalled();
  });

  it('cancels the attempt and keeps the server state', async () => {
    const api = client();
    const actions = actionsFor(api);
    const result = await actions.cancel(attemptView(BOUND)!);
    expect(result.status).toBe('cancelled');
    expect(result.attempt?.state).toBe('cancelled');
  });
});
