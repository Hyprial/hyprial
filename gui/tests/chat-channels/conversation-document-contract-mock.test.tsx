/**
 * Decision record (chat reads the conversation document through the Contract
 * Mock, slice G1, 2026-11-18)
- Every answer here comes from the published request examples through the
  Contract Mock engine, the product's transport and its generated client; no
  test writes a response. The examples describe one world (`user:alice`,
  `agent:alice:node:worker`): two directory heads for the worker, the default
  conversation `dc.example-default-conversation` and `conversation-1`, each
  with a `conversations.history` page on its `c.` document.
- The preview entry (`connectContractMock`) is used as is for the read flow, so
  what the dev preview shows is what is asserted. Its session principal is
  `user:owner`, not the examples' `user:alice`, so the preview labels the
  examples' own messages as the agent's; authorship is not asserted there
  (handoff 7.3).
- A first send needs a directory that is empty before the send and lists the
  default conversation after it. The engine's preset transitions express that
  (`first-send` until a 200 `message.send`, then `success`); the preset document
  is declared here because it is a test world, not a preview default. It pins
  the no-id send example by name: unpinned, the engine answers a send without
  `conversationId` with the `conversation-1` example (an absent selector agrees
  with any value), so the pin is what makes the answer the default
  conversation and the test's own check that the request omitted the id is
  the proof of the GUI's half.
- The Contract Mock echoes the method's `x-hyprial-mock-echo` identity key
  (`idempotencyKey` -> `operationId`), so the GUI send adapter settles accepted
  sends as `success` and the controller adopts the answered `conversationId`
  (`dc.example-default-conversation` on a first send without id) and loads its
  document through `conversations.history` (never `message.history`).
- The no-id send example is pinned by name in the `first-send` preset: an
  unpinned send without `conversationId` is answered with the
  `conversation-1` example, because an absent selector agrees with any value.
- The preview test follows the browser acceptance script: the newest head is
  the default conversation, whose document holds "hello"; switching to the
  second session shows `conversation-1`; a send through the mock now
  settles as accepted, keeps the sent message in the transcript, and clears the
  composer.
- The published contract is read as tests/contract-mock/synthetic.ts reads
  it, from a path string, because jsdom's URL is not the one node:url
  accepts. Radix Select needs the pointer-capture and scrollIntoView stubs in
  jsdom (as in tests/forms/select-field-contract.test.tsx). The send adapter
  keeps an unsettled operation key in sessionStorage, so each test starts
  without one.
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll } from 'vitest';
import { connectBrowserBackend } from '../../src/app/backend';
import { GuiCapabilitiesProvider, createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { connectContractMock } from '../../src/dev/contract-mock';
import { ConnectedChatController, createMessageReadApiFromClient } from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';
import {
  MOCK_ORIGIN, createContractMock, createContractMockFetch, projectContract, withDigest,
  type PresetDocument,
} from '../../contract-mock/index.ts';

const WORKER = 'agent:alice:node:worker';
const DEFAULT_DOCUMENT = 'c.defaultconversationaaaaaaa';
const DEFAULT_CONVERSATION = 'dc.example-default-conversation';
const CONVERSATION_ONE_DOCUMENT = 'c.conversationoneaaaaaaaaaaa';

const CONTRACT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../contract/daemon-request');
const contractFile = (name: string) => JSON.parse(readFileSync(resolve(CONTRACT, name), 'utf8'));

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

afterEach(() => { sessionStorage.clear(); });

it('the preview shows the worker conversation from conversations.history and switches documents', async () => {
  const user = userEvent.setup();
  const bootstrap = await connectContractMock({ scenario: 'success' });
  const capabilities = createConnectedGuiCapabilities(bootstrap.connection);
  render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  await user.click(await screen.findByRole('button', { name: /worker/ }));
  const log = await screen.findByRole('log');
  expect(await within(log).findByText('hello')).toBeVisible();

  await user.click(await screen.findByRole('combobox', { name: '会话' }));
  await user.click(await screen.findByRole('option', { name: /^2 · / }));
  expect(await within(log).findByText('演示消息，请确认收到。')).toBeVisible();
  expect(within(log).queryByText('hello')).not.toBeInTheDocument();

  const composer = screen.getByLabelText('给 worker 的消息');
  await user.type(composer, 'browser hello');
  await user.click(screen.getByRole('button', { name: '发送' }));
  expect(await within(log).findByText('browser hello')).toBeVisible();
  expect(composer).toHaveValue('');
  expect(screen.queryByText(/发送结果未知/)).not.toBeInTheDocument();
});

it('reads the body-omitted example and keeps its reason', async () => {
  const bootstrap = await connectContractMock({ scenario: 'success', cases: { 'conversations.history': 'body-omitted' } });
  const api = createMessageReadApiFromClient(bootstrap.connection.client);
  const page = await api.history(CONVERSATION_ONE_DOCUMENT);
  expect(page.entries.map(entry => [entry.messageId, entry.message, entry.bodyOmitted])).toEqual([
    ['message-1', '演示消息，请确认收到。', undefined],
    ['message-2', '', 'redacted'],
  ]);
});

it('a first send names no conversation and the answered id resolves to the default document', async () => {
  const presets: PresetDocument = {
    version: 1,
    presets: {
      'first-send': {
        scenario: 'success', methodScenarios: { 'conversations.list': 'empty', 'message.query': 'empty' },
        cases: { 'message.send': 'accepted-send-without-conversation-id' },
      },
      success: { scenario: 'success' },
    },
    transitions: [{ preset: 'first-send', method: 'message.send', next: 'success' }],
    inputGuards: [],
  };
  const table = withDigest(projectContract(contractFile('openapi.json'), contractFile('http-envelope.schema.json')));
  const mock = createContractMock(table, { preset: 'first-send' }, { presets });
  const replies: Array<{ method: string; params: unknown; result: unknown }> = [];
  const handle = mock.handle;
  mock.handle = (method, params) => {
    const reply = handle(method, params);
    replies.push({ method, params, result: reply.payload.result });
    return reply;
  };
  const memory = new Map<string, string>();
  const bootstrap = await connectBrowserBackend({
    fetch: createContractMockFetch(mock, { principal: { owner: 'alice', node: 'node', actor: 'user:alice' } }),
    storage: {
      getItem: key => memory.get(key) ?? null,
      setItem: (key, value) => { memory.set(key, value); },
      removeItem: key => { memory.delete(key); },
    },
    location: { origin: MOCK_ORIGIN, hash: '#grant=contract-mock-launch', pathname: '/', search: '' },
    history: { replaceState() {} },
  });
  const capabilities = createConnectedGuiCapabilities(bootstrap.connection);
  const read = capabilities.messageRead!;
  const controller = new ConnectedChatController({
    targets: capabilities.chatTargets!,
    query: view => read.query(view),
    conversations: cursor => read.conversations(cursor),
    history: (documentId, cursor, mode) => read.history(documentId, cursor, mode),
    send: capabilities.chatSend,
    identity: capabilities.chatSession?.actor,
  });

  await controller.start();
  expect(controller.getSnapshot().sessions).toEqual([]);
  controller.select(WORKER);
  expect(controller.getSnapshot().entries[WORKER].conversationId).toBeNull();
  await controller.send(WORKER, 'hello');

  const sends = replies.filter(reply => reply.method === 'message.send');
  expect(sends).toHaveLength(1);
  expect(sends[0].params).not.toHaveProperty('conversationId');
  const answered = sends[0].result as { conversationId: string; messageId: string; deliveries: Array<{ conversationId: string }> };
  expect(answered.conversationId).toBe(DEFAULT_CONVERSATION);
  expect(answered.deliveries.map(delivery => delivery.conversationId)).toEqual([DEFAULT_CONVERSATION]);
  expect(controller.getSnapshot().entries[WORKER].operation?.status).toBe('success');
  expect(controller.getSnapshot().entries[WORKER].conversationId).toBe(DEFAULT_CONVERSATION);
  await waitFor(() => {
    const entry = controller.getSnapshot().entries[WORKER];
    expect(entry.documentId).toBe(DEFAULT_DOCUMENT);
    expect(entry.historyStatus).toBe('ready');
  });

  const directory = await read.conversations();
  const head = directory.entries.find(row => row.peer === WORKER && row.conversationId === answered.conversationId);
  expect(head?.documentId).toBe(DEFAULT_DOCUMENT);
  const page = await read.history(DEFAULT_DOCUMENT, undefined, 'tail');
  expect(page.entries.map(entry => [entry.messageId, entry.conversationId, entry.from, entry.message]))
    .toEqual([[answered.messageId, DEFAULT_CONVERSATION, 'user:alice', 'hello']]);
  expect(replies.some(reply => reply.method === 'message.history')).toBe(false);
});
