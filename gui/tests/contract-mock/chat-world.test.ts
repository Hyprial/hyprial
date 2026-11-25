// @vitest-environment node
/**
 * The published chat examples describe one world, and the mock keeps it.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Ported from tests/daemon/contract_mock/chat_read (chat layout increment,
 *   2026-11-05) and the send half of chat_send, on the real contract table
 *   instead of a per-test export. A browser-only journey cannot prove the mock
 *   answers reads consistently: the chat page asks for inbox and outbox, then
 *   reads history for the conversation it found, so targets, query, history,
 *   send and key lookup must name the same agents, conversation and message.
 * - A request whose selector fields (fields the published result also speaks
 *   about, such as `conversationId` or `view`) contradict every case is refused
 *   with 409 `MOCK_EXAMPLE_MISSING`, never answered with another conversation.
 *   Keys declared in `x-hyprial-mock-echo` (caller-minted identity keys) are
 *   excluded from selectors and echoed into matching success results.
 * - Also here, from the org management and orgfs increments: a send to several
 *   targets is refused before the success scenario, and host-injected fields
 *   are refused on the org file system writes.
 */

import { describe, expect, it } from 'vitest';
import {
  MOCK_EXAMPLE_MISSING,
  createContractMock,
  projectContract,
  withDigest,
} from '../../contract-mock/index.ts';
import type { Scenario } from '../../contract-mock/index.ts';
import { realEnvelope, realOpenapi } from './synthetic.ts';

const table = withDigest(projectContract(realOpenapi(), realEnvelope()));

type Obj = Record<string, any>;

function mockFor(scenario: Scenario) {
  return createContractMock(table, { scenario });
}

function answer(mock: ReturnType<typeof mockFor>, method: string, params: Obj): Obj {
  const reply = mock.handle(method, params);
  expect(reply.status, JSON.stringify(reply.payload)).toBe(200);
  expect(reply.payload.dispatch).toBe('dispatched');
  expect(reply.payload.ok).toBe(true);
  return reply.payload.result as Obj;
}

function agentTargets(result: Obj): Set<string> {
  return new Set(
    result.targets.filter((row: Obj) => row.targetKind === 'agent' && row.deliverable).map((row: Obj) => row.targetUri),
  );
}

describe('chat reads', () => {
  it.each(['inbox', 'outbox'])('answer the %s view with directory agents', (view) => {
    const mock = mockFor('success');
    const targets = answer(mock, 'targets', { kind: 'agent' });
    const query = answer(mock, 'message.query', { view });
    expect(query.view).toBe(view);
    expect(query.entries.length).toBeGreaterThan(0);
    const available = agentTargets(targets);
    for (const entry of query.entries) {
      const peers = new Set(['sender', 'recipient', 'from', 'to'].map((field) => entry[field]).filter((value) => available.has(value)));
      expect(peers.size, JSON.stringify(entry)).toBe(1);
    }
  });

  it.each(['inbox', 'outbox'])('answer the %s view in the empty scenario', (view) => {
    const query = answer(mockFor('empty'), 'message.query', { view });
    expect(query.view).toBe(view);
    expect(query.entries).toEqual([]);
  });

  it('describe one conversation across reads, send and lookup', () => {
    const mock = mockFor('success');
    const query = answer(mock, 'message.query', { view: 'inbox' });
    const conversation = query.entries[0].conversationId;
    const messageId = query.entries[0].messageId;
    const history = answer(mock, 'message.history', { conversationId: conversation });
    expect(history.conversationId).toBe(conversation);
    expect(history.entries.map((entry: Obj) => entry.messageId)).toEqual([messageId]);
    const lookup = answer(mock, 'message.send.key_lookup', { operationKey: 'a'.repeat(43) });
    expect(lookup.status).toBe('accepted');
    expect(lookup.conversationId).toBe(conversation);
    expect(lookup.messageId).toBe(messageId);
    const send = answer(mock, 'message.send', {
      to: ['agent:alice:node:worker'],
      message: 'hello',
      summary: 'hello',
      conversationId: conversation,
      idempotencyKey: 'frozen-operation-1-extended',
    });
    expect(send.conversationId).toBe(conversation);
    expect(send.messageId).toBe(messageId);
    expect(send.deliveries.map((delivery: Obj) => delivery.target)).toEqual(['agent:alice:node:worker']);
  });

  it('prefer the example whose params match the request', () => {
    const outbox = mockFor('success').handle('message.query', { view: 'outbox' });
    expect(outbox.status).toBe(200);
    expect((outbox.payload.result as Obj).view).toBe('outbox');
  });

  it.each(['success', 'empty'] as const)('never answer an unknown conversation with another in %s', (scenario) => {
    const reply = mockFor(scenario).handle('message.history', { conversationId: 'some-other-conversation' });
    expect(reply.status).toBe(409);
    expect(reply.payload.ok).toBe(false);
    expect(reply.payload.code).toBe(MOCK_EXAMPLE_MISSING);
    expect(reply.payload.dispatch).toBe('not-dispatched');
    expect(reply.payload.method).toBe('message.history');
    expect(reply.payload).not.toHaveProperty('result');
    expect(reply.payload.message).toContain('message.history');
    expect(reply.payload.message).toContain('conversationId');
    expect(JSON.stringify(reply.payload)).not.toContain('message-1');
  });

  it('echoes declared caller-minted identity keys into matching success replies without mutating the example', () => {
    const mock = mockFor('success');
    const fresh = 'b'.repeat(43);
    const sendPublished = table.methods['message.send'].examples.find((example) => example.kind === 'success')!;
    const originalOperationId = (sendPublished.result as Obj).operationId;
    const send = mock.handle('message.send', {
      to: ['agent:alice:node:worker'],
      message: 'a typed message no example declares',
      summary: 'a typed message no example declares',
      conversationId: 'conversation-1',
      idempotencyKey: fresh,
    });
    expect(send.status, JSON.stringify(send.payload)).toBe(200);
    expect(send.payload.result).toEqual({ ...(sendPublished.result as Obj), operationId: fresh });
    expect((sendPublished.result as Obj).operationId).toBe(originalOperationId);

    const lookup = mock.handle('message.send.key_lookup', { operationKey: fresh });
    expect(lookup.status).toBe(200);
    expect((lookup.payload.result as Obj).status).toBe('accepted');
    expect((lookup.payload.result as Obj).commandId).toBe(fresh);

    const notFound = createContractMock(table, {
      scenario: 'success',
      cases: { 'message.send.key_lookup': 'not-found' },
    }).handle('message.send.key_lookup', { operationKey: fresh });
    expect(notFound.status).toBe(200);
    expect(notFound.payload.result).toEqual({ status: 'unknown', reason: 'not-found' });

    const posted = mock.handle('conversations.post', {
      conversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
      message: 'typed preview post',
      summary: 'typed preview post',
      idempotencyKey: fresh,
    });
    expect(posted.status).toBe(200);
    expect((posted.payload.result as Obj).operationId).toBe(fresh);
  });

  it('resolve identical candidate params to the first declared case', () => {
    const published = table.methods['message.send.key_lookup'].examples.filter((example) => example.kind === 'success');
    expect(published[0].params).toEqual(published[published.length - 1].params);
    const reply = mockFor('success').handle('message.send.key_lookup', { operationKey: 'a'.repeat(43) });
    expect(reply.status).toBe(200);
    expect(reply.payload.result).toEqual({ ...(published[0].result as Obj), commandId: 'a'.repeat(43) });
  });
});

describe('refusals before the success scenario', () => {
  it('refuses a send to several targets as invalid params', () => {
    const reply = mockFor('success').handle('message.send', {
      to: ['agent:alice:node:worker', 'agent:alice:node:other'],
      message: 'hello',
      summary: 'hello',
      conversationId: 'conversation-1',
      idempotencyKey: 'a'.repeat(43),
    });
    expect(reply.status).toBe(400);
    expect(reply.payload.code).toBe('INVALID_ARGUMENT');
    expect(reply.payload.dispatch).toBe('not-dispatched');
  });

  it('refuses host-injected fields on every org file system write', () => {
    const mock = mockFor('success');
    const writes = Object.keys(table.methods).filter((name) => /^orgfs\.(create|write|export|import|mkdir|move|remove|restore)$/.test(name));
    expect(writes.length).toBeGreaterThan(5);
    for (const method of writes) {
      const params = table.methods[method].examples.find((example) => example.kind === 'success')?.params as Obj;
      for (const field of ['actor', 'from', 'sessionRef']) {
        const reply = mock.handle(method, { ...params, [field]: 'untrusted' });
        expect(reply.status, `${method} ${field}`).toBe(400);
        expect(reply.payload.code).toBe('INVALID_ARGUMENT');
        expect(reply.payload.dispatch).toBe('not-dispatched');
      }
    }
  });

  it('serves the org.delete preview in the success scenario and refusals only when pinned', () => {
    const entry = table.methods['org.delete'];
    const preview = entry.examples.find((example) => example.name === 'preview')!;
    expect(preview.kind).toBe('success');
    const previewed = mockFor('success').handle('org.delete', preview.params);
    expect(previewed.status).toBe(200);
    expect(previewed.payload.ok).toBe(true);
    const refusal = entry.examples.find((example) => example.error?.code === 'ORG_ALREADY_DELETED')!;
    const pinned = createContractMock(table, { scenario: 'success', cases: { 'org.delete': refusal.name } });
    const served = pinned.handle('org.delete', refusal.params);
    expect(served.payload.code).toBe('ORG_ALREADY_DELETED');
    expect(served.payload.dispatch).toBe('dispatched');
    expect(served.payload.ok).toBe(false);
  });

  it('completes an org.delete confirm bound to the preview operationId', () => {
    type Deletion = { readonly phase: string; readonly operationId: string };
    const mock = mockFor('success');
    const example = table.methods['org.delete'].examples.find((entry) => entry.name === 'preview')!;
    const org = (example.params as { readonly org: string }).org;
    const previewed = mock.handle('org.delete', { org }).payload.result as Deletion;
    expect(previewed.phase).toBe('pending-confirmation');
    const confirmed = mock.handle('org.delete', {
      org,
      confirmed: true,
      operationId: previewed.operationId,
    });
    expect(confirmed.status).toBe(200);
    expect((confirmed.payload.result as Deletion).phase).toBe('complete');
  });
});

describe('group story', () => {
  it('answers create with a.<id> members, history with memberNames, and remove for either agent', () => {
    const mock = mockFor('success');
    const created = answer(mock, 'conversations.create', {
      title: 'release',
      members: ['a.workeraaaaaaaaaaaaaaaaaaaa', 'a.revieweraaaaaaaaaaaaaaaaaa'],
      idempotencyKey: 'b'.repeat(43),
    });
    const groupId = created.conversationId as string;
    const history = answer(mock, 'conversations.history', { conversationId: groupId });
    expect(history.memberNames).toEqual({
      'user:alice': 'alice',
      'a.workeraaaaaaaaaaaaaaaaaaaa': 'worker',
      'a.revieweraaaaaaaaaaaaaaaaaa': 'reviewer',
    });
    const removedReviewer = answer(mock, 'conversations.members.remove', {
      conversationId: groupId,
      memberId: 'a.revieweraaaaaaaaaaaaaaaaaa',
      idempotencyKey: 'c'.repeat(43),
    });
    expect(removedReviewer.members.map((member: Obj) => member.memberId)).toEqual([
      'user:alice',
      'a.workeraaaaaaaaaaaaaaaaaaaa',
    ]);
    const removedWorker = answer(mock, 'conversations.members.remove', {
      conversationId: groupId,
      memberId: 'a.workeraaaaaaaaaaaaaaaaaaaa',
      idempotencyKey: 'd'.repeat(43),
    });
    expect(removedWorker.members.map((member: Obj) => member.memberId)).toEqual([
      'user:alice',
      'a.revieweraaaaaaaaaaaaaaaaaa',
    ]);
  });
});
