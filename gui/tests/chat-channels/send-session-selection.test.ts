import { sendSessionsFromMessages } from '../../src/features/chat-channels';

const agent = 'agent:owner:node:worker';
const other = 'agent:owner:node:other';
const targets = [{ targetKind: 'agent', targetUri: agent, deliverable: true, displayName: 'Alfred' },
  { targetKind: 'agent', targetUri: other, deliverable: true }];

it('offers only an existing conversation with one directory-confirmed canonical agent', () => {
  const sessions = sendSessionsFromMessages([
    { messageId: 'one', conversationId: 'conversation-1', sender: 'user:owner', recipient: agent, createdAtMs: 10 },
    { messageId: 'two', conversationId: 'conversation-1', sender: agent, recipient: 'user:owner', createdAtMs: 20 },
    { messageId: 'three', conversationId: 'conversation-2', sender: 'user:owner', recipient: other, createdAtMs: 30 },
  ], targets);
  expect(sessions).toEqual([
    { conversationId: 'conversation-2', targetUri: other, displayName: other },
    { conversationId: 'conversation-1', targetUri: agent, displayName: 'Alfred' },
  ]);
});

it('rejects ambiguous, unlisted, noncanonical and empty conversation bindings', () => {
  expect(sendSessionsFromMessages([
    { messageId: 'a', conversationId: 'mixed', sender: agent, recipient: other },
    { messageId: 'b', conversationId: 'missing-target', sender: 'user:owner', recipient: 'agent:owner:node:unknown' },
    { messageId: 'c', conversationId: '', sender: 'user:owner', recipient: agent },
    { messageId: 'd', conversationId: 'bad-shape', sender: 'user:owner', recipient: 'agent:bad' },
  ], targets)).toEqual([]);
});

it('never admits a target that the current directory marks undeliverable', () => {
  expect(sendSessionsFromMessages([
    { messageId: 'one', conversationId: 'conversation-1', sender: 'user:owner', recipient: agent },
  ], [{ targetKind: 'agent', targetUri: agent, deliverable: false }])).toEqual([]);
});
