/**
 * Decision record (group post adapter tests, slice GC5-2, 2026-11-21):
 * - Verifies `createGroupPostApiFromClient`: 43-char in-memory idempotencyKey
 *   (no browser storage writes), first-line 80-char summary derivation,
 *   optional `replyTo`/`mentions`, strict `operationId === idempotencyKey` and
 *   delivery validation, Contract Mock mismatch -> `unknown`, all four refusal
 *   codes (`CONVERSATION_NOT_MEMBER`, `CONVERSATION_NOT_FOUND`,
 *   `SUMMARY_REQUIRED`, `INVALID_ARGUMENT`), lost responses, and pre-dispatch
 *   refusals.
 */

import { describe, expect, it, vi } from 'vitest';
import { HttpError, MethodRefusedError, TransportError, type MethodAccess } from '../../src/client';
import { createGeneratedClient } from '../../src/client/generated';
import {
  GROUP_POST_ACCEPTED_NOTICE,
  GROUP_POST_INVALID_INTENT_NOTICE,
  GROUP_POST_UNKNOWN_NOTICE,
  createGroupPostApiFromClient,
} from '../../src/features/chat-channels';

const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';

function bytes32(fill: number): Uint8Array {
  return new Uint8Array(32).fill(fill);
}

function acceptedReply(params: Record<string, unknown>) {
  return {
    ok: true,
    actingPrincipal: 'user:alice',
    operationId: params.idempotencyKey,
    conversationId: GROUP_ID,
    messageId: 'm-new-1',
    deliveries: [{
      accepted: true,
      conversationId: GROUP_ID,
      messageId: 'm-new-1',
      queued: false,
      target: GROUP_ID,
    }],
  };
}

function makeClient(handler: (method: string, params: Record<string, unknown>) => Promise<unknown>, access?: MethodAccess) {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const client = createGeneratedClient({
    invoke: async <T>(method: string, params: unknown = {}): Promise<T> => {
      const record = params as Record<string, unknown>;
      calls.push({ method, params: record });
      return handler(method, record) as Promise<T>;
    },
  });
  return { calls, api: createGroupPostApiFromClient(client, { access, randomBytes: () => bytes32(7) }) };
}

describe('createGroupPostApiFromClient', () => {
  it('generates a 43-char in-memory key, derives first-line 80-char summary, and validates acceptance', async () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    const getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
    const longFirstLine = 'a'.repeat(95);
    const { calls, api } = makeClient(async (_method, params) => acceptedReply(params));

    const outcome = await api.post({
      conversationId: GROUP_ID,
      message: `  ${longFirstLine}\nsecond line`,
      replyTo: 'm-parent',
      mentions: ['a.workeraaaaaaaaaaaaaaaaaaaa'],
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('conversations.post');
    const sent = calls[0].params;
    expect(sent.idempotencyKey).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(sent.summary).toBe('a'.repeat(80));
    expect(sent.replyTo).toBe('m-parent');
    expect(sent.mentions).toEqual(['a.workeraaaaaaaaaaaaaaaaaaaa']);
    expect(outcome).toEqual({
      status: 'accepted',
      operationKey: sent.idempotencyKey,
      conversationId: GROUP_ID,
      messageId: 'm-new-1',
      duplicate: false,
      notice: GROUP_POST_ACCEPTED_NOTICE,
    });
    expect(setItemSpy).not.toHaveBeenCalled();
    expect(getItemSpy).not.toHaveBeenCalled();
    setItemSpy.mockRestore();
    getItemSpy.mockRestore();
  });

  it('returns unknown when operationId does not equal the sent key or response shape mismatches', async () => {
    const variants: Array<(params: Record<string, unknown>) => unknown> = [
      () => ({
        ok: true,
        actingPrincipal: 'user:alice',
        operationId: 'example-post-release-tag-01',
        conversationId: GROUP_ID,
        messageId: 'm-example',
        deliveries: [{ accepted: true, conversationId: GROUP_ID, messageId: 'm-example', queued: false, target: GROUP_ID }],
      }),
      params => ({ ...acceptedReply(params), ok: false }),
      params => ({ ...acceptedReply(params), conversationId: 'g.otheraaaaaaaaaaaaaaaaaaaaa' }),
      params => ({ ...acceptedReply(params), messageId: '' }),
      params => ({ ...acceptedReply(params), deliveries: [] }),
      params => ({
        ...acceptedReply(params),
        deliveries: [{ accepted: false, conversationId: GROUP_ID, messageId: 'm-new-1', queued: false, target: GROUP_ID }],
      }),
      params => ({
        ...acceptedReply(params),
        deliveries: [{ accepted: true, conversationId: GROUP_ID, messageId: 'm-other', queued: false, target: GROUP_ID }],
      }),
    ];

    for (const variant of variants) {
      const { calls, api } = makeClient(async (_method, params) => variant(params));
      const outcome = await api.post({ conversationId: GROUP_ID, message: 'hello' });
      expect(calls).toHaveLength(1);
      expect(outcome).toMatchObject({
        status: 'unknown',
        reason: 'response-mismatch',
        notice: GROUP_POST_UNKNOWN_NOTICE,
      });
    }
  });

  it.each([
    ['CONVERSATION_NOT_MEMBER', '你已不在这个群里，本次未发送。'],
    ['CONVERSATION_NOT_FOUND', '这个群已不存在，本次未发送。'],
    ['SUMMARY_REQUIRED', '缺少消息摘要，本次未发送。'],
    ['INVALID_ARGUMENT', '发送参数无效，本次未发送。'],
  ] as const)('translates dispatched refusal %s to Chinese', async (code, expectedNotice) => {
    const { calls, api } = makeClient(async () => {
      throw new HttpError(code, 'refused', 400, {
        method: 'conversations.post',
        requestId: 'req-1',
        status: 400,
        dispatch: 'dispatched',
        catalogDigest: 'd',
        attempts: 1,
        startedAt: 1,
        durationMs: 1,
      });
    });

    const outcome = await api.post({ conversationId: GROUP_ID, message: 'hello' });
    expect(calls).toHaveLength(1);
    expect(outcome).toMatchObject({
      status: 'refused',
      reason: 'dispatch-refused',
      code,
      notice: expectedNotice,
    });
  });

  it('returns unknown when response is lost or dispatch outcome is unknown', async () => {
    const errors: unknown[] = [
      new Error('socket closed'),
      new TransportError('network', 'NETWORK_ERROR', 'lost', {
        method: 'conversations.post',
        requestId: 'req-2',
        status: null,
        dispatch: 'unknown',
        catalogDigest: 'd',
        attempts: 1,
        startedAt: 1,
        durationMs: 1,
      }),
      new HttpError('INTERNAL_ERROR', 'boom', 500, {
        method: 'conversations.post',
        requestId: 'req-3',
        status: 500,
        dispatch: 'dispatched',
        catalogDigest: 'd',
        attempts: 1,
        startedAt: 1,
        durationMs: 1,
      }),
    ];

    for (const error of errors) {
      const { calls, api } = makeClient(async () => { throw error; });
      const outcome = await api.post({ conversationId: GROUP_ID, message: 'hello' });
      expect(calls).toHaveLength(1);
      expect(outcome).toMatchObject({
        status: 'unknown',
        reason: 'dispatch-unknown',
        notice: GROUP_POST_UNKNOWN_NOTICE,
      });
    }
  });

  it('rejects invalid intents and pre-dispatch refusals without calling conversations.post', async () => {
    const { calls, api } = makeClient(async (_method, params) => acceptedReply(params));
    expect(await api.post({ conversationId: '', message: 'hi' })).toEqual({
      status: 'not-started',
      reason: 'invalid-intent',
      notice: GROUP_POST_INVALID_INTENT_NOTICE,
    });
    expect(await api.post({ conversationId: GROUP_ID, message: '   \n  ' })).toEqual({
      status: 'not-started',
      reason: 'invalid-intent',
      notice: GROUP_POST_INVALID_INTENT_NOTICE,
    });
    expect(calls).toHaveLength(0);

    const refusedAccess = makeClient(
      async (_method, params) => acceptedReply(params),
      () => ({ available: false, code: 'METHOD_NOT_FOUND' }),
    );
    expect(await refusedAccess.api.post({ conversationId: GROUP_ID, message: 'hi' })).toEqual({
      status: 'not-started',
      reason: 'method-refused',
      code: 'METHOD_NOT_FOUND',
      source: 'conversations.post',
    });
    expect(refusedAccess.calls).toHaveLength(0);

    const transportRefused = makeClient(async () => {
      throw new MethodRefusedError('conversations.post', 'WRITE_METHOD_UNVERIFIED');
    });
    expect(await transportRefused.api.post({ conversationId: GROUP_ID, message: 'hi' })).toEqual({
      status: 'not-started',
      reason: 'method-refused',
      code: 'WRITE_METHOD_UNVERIFIED',
      source: 'conversations.post',
    });
  });
});
