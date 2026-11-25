/**
 * Decision record (chat reads the conversation document, slice G1, 2026-11-18)
- These tests pin the G1 projection: history is `conversations.history` on the
  `c.` document a `conversations.list` head names; the send id is the head's
  `legacyConversationId` and each history entry's `conversationId`; a first
  message names no conversation and adopts the id the daemon returns; system
  entries stay folded; an entry carrying `bodyOmitted` is shown as not recorded and
  an empty body without it is not;
  `recordAvailable: false` changes nothing; key lookup settles a send that
  named no conversation.
- They are jsdom/API evidence with hand-written ports (D11). They do not prove
  a real daemon, the Contract Mock world or a browser.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { MethodRefusedError } from '../../src/client';
import { createGeneratedClient } from '../../src/client/generated';
import {
  ConnectedChatController,
  createChatSendApi,
  createMessageReadApiFromClient,
  type ChatSendParams,
  type ChatSendStorage,
  type HistoryPage,
  type MessageReadApi,
} from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const OWNER = 'user:owner';
const TARGET = 'agent:owner:node:worker';
const DEFAULT = 'dc.bbbbbbbbbbbbbbbbbbbbbbbbbb';
const DEFAULT_DOCUMENT = 'c.bbbbbbbbbbbbbbbbbbbbbbbbbb';
const RANDOM = '0b6f3c1e-1d2a-4c55-9f00-5a1e2d3c4b5a';
const RANDOM_DOCUMENT = 'c.aaaaaaaaaaaaaaaaaaaaaaaaaa';

type Entry = HistoryPage['entries'][number];

function message(id: string, conversationId: string, overrides: Partial<Entry> = {}): Entry {
  return {
    kind: 'message', messageId: id, conversationId, from: OWNER, to: TARGET, intent: 'request',
    createdAtMs: 1000, message: id, origin: {}, messageClass: 'interaction', ...overrides,
  };
}

function head(documentId: string, conversationId: string, at: number) {
  return { documentId, conversationId, peer: TARGET, lastMessageAtMs: at };
}

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map<string, string>(Object.entries(initial));
  const storage: ChatSendStorage = {
    getItem: name => values.get(name) ?? null,
    setItem(name, value) { values.set(name, value); },
    removeItem(name) { values.delete(name); },
  };
  return { storage, values };
}

function settle() {
  return new Promise(resolve => { setTimeout(resolve, 0); });
}

function targetRow() {
  return { targetKind: 'agent', targetUri: TARGET, deliverable: true, actor: TARGET, status: 'online' };
}

it('reads history from conversations.history by document id and maps the three ids of a head', async () => {
  const calls: Array<{ method: string; params: unknown }> = [];
  const client = createGeneratedClient({ invoke: async <T,>(method: string, params: unknown): Promise<T> => {
    calls.push({ method, params });
    if (method === 'conversations.list') {
      return { ok: true, participant: OWNER, nextCursor: null, recordAvailable: false, conversations: [{
        conversationId: RANDOM_DOCUMENT, legacyConversationId: RANDOM, threadId: null, kind: 'agent', peer: TARGET,
        lastMessageAtMs: 5, lastMessageId: 'm-1', messages: 1, messageClass: 'interaction',
      }] } as T;
    }
    return { ok: true, conversationId: RANDOM_DOCUMENT, legacyConversationId: RANDOM, threadId: null,
      entries: [message('m-1', RANDOM, { message: '' })], nextCursor: null, headCursor: 'head', recordAvailable: false } as T;
  } });
  const api = createMessageReadApiFromClient(client);

  expect(await api.conversations()).toEqual({
    entries: [{ documentId: RANDOM_DOCUMENT, conversationId: RANDOM, peer: TARGET, lastMessageAtMs: 5 }],
    nextCursor: null, recordAvailable: false,
  });
  const page = await api.history(RANDOM_DOCUMENT, undefined, 'tail');
  expect(page).toMatchObject({ entries: [{ messageId: 'm-1', conversationId: RANDOM, message: '' }], recordAvailable: false });
  expect(calls.map(call => call.method)).toEqual(['conversations.list', 'conversations.history']);
  expect(calls[1].params).toEqual({ conversationId: RANDOM_DOCUMENT, mode: 'tail' });
  expect(calls.some(call => call.method === 'message.history')).toBe(false);
});

it('rejects a history entry whose message class is not declared', async () => {
  const client = createGeneratedClient({ invoke: async <T,>(): Promise<T> => ({
    ok: true, conversationId: RANDOM_DOCUMENT, legacyConversationId: RANDOM, threadId: null, nextCursor: null,
    entries: [{ ...message('m-1', RANDOM), messageClass: 'other' }],
  }) as T });
  await expect(createMessageReadApiFromClient(client).history(RANDOM_DOCUMENT)).rejects.toThrow('历史消息格式不正确');
});

it('rejects a history entry whose bodyOmitted is not a non-empty string', async () => {
  for (const bodyOmitted of ['', 7, null]) {
    const client = createGeneratedClient({ invoke: async <T,>(): Promise<T> => ({
      ok: true, conversationId: RANDOM_DOCUMENT, legacyConversationId: RANDOM, threadId: null, nextCursor: null,
      entries: [{ ...message('m-1', RANDOM, { message: '' }), bodyOmitted }],
    }) as T });
    await expect(createMessageReadApiFromClient(client).history(RANDOM_DOCUMENT)).rejects.toThrow('历史消息格式不正确');
  }
});

describe('a send that names no conversation', () => {
  function sendWith(answer: (params: ChatSendParams) => unknown) {
    const intents: ChatSendParams[] = [];
    const api = createChatSendApi({
      async send(params) { intents.push(params); return answer(params); },
      async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
    }, { storage: memoryStorage().storage, randomBytes: () => new Uint8Array(32).fill(5) });
    return { api, intents };
  }

  it('omits conversationId and accepts the id the daemon stored', async () => {
    const { api, intents } = sendWith(params => ({ ok: true, operationId: params.idempotencyKey, conversationId: DEFAULT,
      messageId: 'sent-1', deliveries: [{ target: TARGET, messageId: 'sent-1', conversationId: DEFAULT }] }));
    const outcome = await api.send({ to: [TARGET], message: 'hello' });
    expect(intents[0]).not.toHaveProperty('conversationId');
    expect(outcome).toMatchObject({ status: 'accepted', conversationId: DEFAULT, messageId: 'sent-1' });
  });

  it('treats a delivery that names another conversation as an unknown result', async () => {
    const { api } = sendWith(params => ({ ok: true, operationId: params.idempotencyKey, conversationId: DEFAULT,
      messageId: 'sent-1', deliveries: [{ target: TARGET, messageId: 'sent-1', conversationId: RANDOM }] }));
    expect(await api.send({ to: [TARGET], message: 'hello' })).toMatchObject({ status: 'unknown', reason: 'response-mismatch' });
  });

  it('still requires the daemon to echo a conversation the caller named', async () => {
    const { api, intents } = sendWith(params => ({ ok: true, operationId: params.idempotencyKey, conversationId: DEFAULT,
      messageId: 'sent-1', deliveries: [{ target: TARGET, messageId: 'sent-1' }] }));
    expect(await api.send({ to: [TARGET], message: 'hello', conversationId: RANDOM }))
      .toMatchObject({ status: 'unknown', reason: 'response-mismatch' });
    expect(intents[0]).toMatchObject({ conversationId: RANDOM });
  });
});

/** One owner and one agent; the directory lists the default conversation once it holds a message. */
function world() {
  const heads: Array<ReturnType<typeof head>> = [];
  const documents = new Map<string, Entry[]>();
  const historyCalls: Array<[string, string | undefined, string | undefined]> = [];
  const listCalls: number[] = [];
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) {
      intents.push(params);
      const conversationId = params.conversationId ?? DEFAULT;
      const id = `sent-${intents.length}`;
      if (conversationId === DEFAULT && !heads.some(row => row.conversationId === DEFAULT)) {
        heads.unshift(head(DEFAULT_DOCUMENT, DEFAULT, 3000));
      }
      const document = conversationId === DEFAULT ? DEFAULT_DOCUMENT : RANDOM_DOCUMENT;
      documents.set(document, [...(documents.get(document) ?? []),
        message(id, conversationId, { message: params.message, createdAtMs: 3000 + intents.length * 10 })]);
      return { ok: true, operationId: params.idempotencyKey, conversationId, messageId: id,
        deliveries: [{ target: TARGET, messageId: id, conversationId }] };
    },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  }, { storage: memoryStorage().storage, randomBytes: () => new Uint8Array(32).fill(8) });
  const read = {
    conversations: async () => { listCalls.push(heads.length); return { entries: [...heads], nextCursor: null }; },
    history: async (documentId: string, cursor?: string, mode?: string): Promise<HistoryPage> => {
      historyCalls.push([documentId, cursor, mode]);
      return { entries: documents.get(documentId) ?? [], nextCursor: null, headCursor: null };
    },
  };
  return { heads, documents, historyCalls, listCalls, intents, send, read };
}

it('starts one conversation per agent: the first send adopts the default id and the agent reply appears there', async () => {
  const { documents, historyCalls, intents, send, read } = world();
  const controller = new ConnectedChatController({
    targets: async () => [targetRow()], query: async () => [], ...read, send, identity: OWNER,
  });
  await controller.start();
  controller.select(TARGET);
  await controller.send(TARGET, 'first');

  expect(intents[0]).not.toHaveProperty('conversationId');
  await waitFor(() => expect(historyCalls).toEqual([[DEFAULT_DOCUMENT, undefined, 'tail']]));
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ conversationId: DEFAULT, documentId: DEFAULT_DOCUMENT });
  expect(controller.getSnapshot().sessions.map(row => row.conversationId)).toEqual([DEFAULT]);

  documents.set(DEFAULT_DOCUMENT, [...documents.get(DEFAULT_DOCUMENT)!,
    message('reply-1', DEFAULT, { from: TARGET, to: OWNER, intent: 'reply', message: '收到', createdAtMs: 3015 })]);
  await controller.refresh();
  await controller.send(TARGET, 'second');
  await controller.refresh();

  expect(intents[1]).toMatchObject({ conversationId: DEFAULT, message: 'second' });
  expect(controller.getSnapshot().entries[TARGET].messages.map(row => [row.id, row.author, row.text])).toEqual([
    ['sent-1', 'user', 'first'], ['reply-1', 'assistant', '收到'], ['sent-2', 'user', 'second'],
  ]);
});

it('keeps a first send unread and stops re-reading when the directory re-read is refused, without a notification', async () => {
  const reports: unknown[] = [];
  const { send, read } = world();
  let refuse = false;
  let refused = 0;
  const controller = new ConnectedChatController({
    targets: async () => [targetRow()], query: async () => [],
    conversations: async () => {
      if (refuse) { refused += 1; throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND'); }
      return read.conversations();
    },
    history: read.history, send, identity: OWNER,
    reportUnavailable: (subject, detail) => { reports.push([subject, detail]); },
  });
  await controller.start();
  refuse = true;
  controller.select(TARGET);
  await controller.send(TARGET, 'first');
  await settle();
  await controller.refresh();
  await controller.refresh();

  expect(reports).toEqual([]);
  expect(refused).toBe(1);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    conversationId: DEFAULT, documentId: null, historyStatus: 'unread', historyError: undefined,
  });
  expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first']);
});

it('reads each peer its own document when one send id names a cell per peer', async () => {
  const OTHER = 'agent:owner:node:other';
  const OTHER_DOCUMENT = 'c.cccccccccccccccccccccccccc';
  const history = vi.fn(async (documentId: string): Promise<HistoryPage> => ({
    entries: [message(documentId === OTHER_DOCUMENT ? 'to-other' : 'to-worker', RANDOM)], nextCursor: null,
  }));
  const controller = new ConnectedChatController({
    targets: async () => [targetRow(), { ...targetRow(), targetUri: OTHER, actor: OTHER }],
    query: async () => [],
    conversations: async () => ({ entries: [
      { documentId: OTHER_DOCUMENT, conversationId: RANDOM, peer: OTHER, lastMessageAtMs: 2000 },
      head(RANDOM_DOCUMENT, RANDOM, 1000),
    ], nextCursor: null }),
    history, identity: OWNER,
  });
  await controller.start();
  controller.select(TARGET);
  await waitFor(() => expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.id)).toEqual(['to-worker']));
  controller.select(OTHER);
  await waitFor(() => expect(controller.getSnapshot().entries[OTHER].messages.map(row => row.id)).toEqual(['to-other']));
  expect(history.mock.calls.map(call => call[0])).toEqual([RANDOM_DOCUMENT, OTHER_DOCUMENT]);
});

it('keeps the first sent message, unread, when the target is reopened before its document is listed', async () => {
  const { send, read, historyCalls } = world();
  const controller = new ConnectedChatController({
    targets: async () => [targetRow()], query: async () => [],
    conversations: async () => ({ entries: [], nextCursor: null }),
    history: read.history, send, identity: OWNER,
  });
  await controller.start();
  controller.select(TARGET);
  await controller.send(TARGET, 'first');
  await settle();
  controller.select(TARGET);
  await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('unread'));

  expect(historyCalls).toEqual([]);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ conversationId: DEFAULT, documentId: null });
  expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first']);
});

it('keeps older random-id conversations listed, readable and sendable', async () => {
  const { heads, documents, historyCalls, intents, send, read } = world();
  heads.push(head(DEFAULT_DOCUMENT, DEFAULT, 3000), head(RANDOM_DOCUMENT, RANDOM, 1000));
  documents.set(DEFAULT_DOCUMENT, [message('new-1', DEFAULT)]);
  documents.set(RANDOM_DOCUMENT, [message('old-1', RANDOM)]);
  const controller = new ConnectedChatController({
    targets: async () => [targetRow()], query: async () => [], ...read, send, identity: OWNER,
  });
  await controller.start();

  expect(controller.getSnapshot().sessions.map(row => row.conversationId)).toEqual([DEFAULT, RANDOM]);
  controller.select(TARGET);
  await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
  controller.selectConversation(TARGET, RANDOM);
  await waitFor(() => expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.id)).toEqual(['old-1']));
  expect(historyCalls.map(call => call[0])).toEqual([DEFAULT_DOCUMENT, RANDOM_DOCUMENT]);

  await controller.send(TARGET, 'into the old one');
  expect(intents[0]).toMatchObject({ conversationId: RANDOM });
});

it('shows a conversation without a listed document as unread and still sends into it', async () => {
  const { historyCalls, intents, send, read } = world();
  const controller = new ConnectedChatController({
    targets: async () => [targetRow()],
    query: async view => view === 'outbox'
      ? [{ messageId: 'q-1', conversationId: RANDOM, from: OWNER, to: TARGET, createdAtMs: 1 }] : [],
    ...read, send, identity: OWNER,
  });
  await controller.start();
  controller.select(TARGET);
  await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('unread'));

  expect(historyCalls).toEqual([]);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ conversationId: RANDOM, messages: [] });
  await controller.send(TARGET, 'hello');
  expect(intents[0]).toMatchObject({ conversationId: RANDOM });
});

it('settles a lost first send through key lookup and opens the conversation the daemon stored', async () => {
  const { storage, values } = memoryStorage();
  const intents: ChatSendParams[] = [];
  const lookups: string[] = [];
  let stored = false;
  const send = createChatSendApi({
    async send(params) { intents.push(params); stored = true; throw new Error('lost response'); },
    async keyLookup(params) {
      lookups.push(params.operationKey);
      return { status: 'accepted', commandId: params.operationKey, conversationId: DEFAULT, messageId: 'sent-1' };
    },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(6) });
  const history = vi.fn(async (): Promise<HistoryPage> => ({ entries: [message('sent-1', DEFAULT, { message: 'hello' })], nextCursor: null }));
  const controller = new ConnectedChatController({
    targets: async () => [targetRow()], query: async () => [],
    conversations: async () => ({ entries: stored ? [head(DEFAULT_DOCUMENT, DEFAULT, 3000)] : [], nextCursor: null }),
    history, send, identity: OWNER,
  });
  await controller.start();
  controller.select(TARGET);
  await controller.send(TARGET, 'hello');
  expect(intents[0]).not.toHaveProperty('conversationId');
  expect(controller.getSnapshot().entries[TARGET].operation?.status).toBe('unknown');
  expect(values.size).toBe(1);

  await controller.recover();
  await waitFor(() => expect(history).toHaveBeenCalledWith(DEFAULT_DOCUMENT, undefined, 'tail'));

  expect(lookups).toHaveLength(1);
  expect(intents).toHaveLength(1);
  expect(values.size).toBe(0);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    conversationId: DEFAULT, documentId: DEFAULT_DOCUMENT, operation: { status: 'success' },
  });
  await waitFor(() => expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.id)).toEqual(['sent-1']));
});

describe('the connected page renders the document', () => {
  function page(entries: Entry[], recordAvailable: boolean) {
    const read: MessageReadApi = {
      query: async () => [],
      conversations: async () => ({ entries: [head(DEFAULT_DOCUMENT, DEFAULT, 3000)], nextCursor: null, recordAvailable }),
      history: async () => ({ entries, nextCursor: null, recordAvailable }),
      lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    };
    const capabilities: GuiCapabilities = {
      ...createWebGuiCapabilities(), messageRead: read,
      chatTargets: async () => [targetRow()],
      chatSession: { actor: OWNER, generation: 'one' },
    };
    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  }

  it('folds system entries, explains an unrecorded body and keeps working without the record', async () => {
    page([
      message('m-1', DEFAULT, { message: '你好' }),
      message('m-2', DEFAULT, { from: TARGET, to: OWNER, intent: 'reply', message: '', bodyOmitted: 'too-large', createdAtMs: 2000 }),
      message('m-4', DEFAULT, { from: TARGET, to: OWNER, intent: 'reply', message: '', createdAtMs: 2500 }),
      message('m-3', DEFAULT, { from: TARGET, to: OWNER, messageClass: 'system', message: 'PAC dispatch', createdAtMs: 3000 }),
      message('m-5', DEFAULT, { from: TARGET, to: OWNER, messageClass: 'system', message: '', bodyOmitted: 'redacted', createdAtMs: 4000 }),
    ], false);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    const log = await screen.findByRole('log');
    expect(await within(log).findByText('你好')).toBeVisible();

    const blank = log.querySelector('[data-message-id="m-2"]')!;
    expect(blank.getAttribute('data-author')).toBe('assistant');
    expect(within(blank as HTMLElement).getByText('这条消息的正文没有记录在会话中。')).toBeInTheDocument();
    const plain = log.querySelector('[data-message-id="m-4"]')!;
    expect(plain.getAttribute('data-author')).toBe('assistant');
    expect(within(plain as HTMLElement).queryByText('这条消息的正文没有记录在会话中。')).not.toBeInTheDocument();

    const system = log.querySelector('[data-message-id="m-3"]')!;
    expect(system.getAttribute('data-author')).toBe('system');
    const fold = within(system as HTMLElement).getByText('任务与系统消息').closest('details')!;
    expect(fold.open).toBe(false);
    expect(within(fold).getByText('PAC dispatch')).toBeInTheDocument();
    const omittedSystem = log.querySelector('[data-message-id="m-5"]')!;
    expect(omittedSystem.getAttribute('data-author')).toBe('system');
    const omittedFold = within(omittedSystem as HTMLElement).getByText('任务与系统消息').closest('details')!;
    expect(omittedFold.open).toBe(false);
    expect(within(omittedFold).getByText('这条消息的正文没有记录在会话中。')).toBeInTheDocument();

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(document.querySelector('.chat-workspace__history-state')!.textContent).toBe('');
  });
});

describe('review findings on PR 2936', () => {
  function queryOnly(view: 'inbox' | 'outbox') {
    return Promise.resolve(view === 'outbox'
      ? [{ messageId: 'q-1', conversationId: RANDOM, from: OWNER, to: TARGET, createdAtMs: 1 }] : []);
  }

  it('shows a failed directory read on open as an error, not as an empty chat', async () => {
    let fail = false;
    const reports: unknown[] = [];
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: queryOnly,
      conversations: async () => { if (fail) throw new Error('offline'); return { entries: [], nextCursor: null }; },
      history: async () => ({ entries: [], nextCursor: null }), identity: OWNER,
      reportUnavailable: (subject, detail) => { reports.push([subject, detail]); },
    });
    await controller.start();
    fail = true;
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('error'));
    expect(controller.getSnapshot().entries[TARGET].historyError).toBe('会话目录读取失败：offline');
    expect(reports).toEqual([]);
  });

  it('reports a refused directory read on open through reportUnavailable and claims no history', async () => {
    let refuse = false;
    const reports: unknown[] = [];
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: queryOnly,
      conversations: async () => {
        if (refuse) throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND');
        return { entries: [], nextCursor: null };
      },
      history: async () => ({ entries: [], nextCursor: null }), identity: OWNER,
      reportUnavailable: (subject, detail) => { reports.push([subject, detail]); },
    });
    await controller.start();
    refuse = true;
    controller.select(TARGET);
    await waitFor(() => expect(reports).toEqual([['读取消息', { code: 'METHOD_NOT_FOUND', source: 'conversations.list' }]]));
    expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ conversationId: RANDOM, historyStatus: 'unread', messages: [] });
  });

  it('renders the failed open as a load failure on the page', async () => {
    let fail = false;
    const read: MessageReadApi = {
      query: queryOnly,
      conversations: async () => { if (fail) throw new Error('offline'); return { entries: [], nextCursor: null }; },
      history: async () => ({ entries: [], nextCursor: null }),
      lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    };
    render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead: read,
      chatTargets: async () => [targetRow()], chatSession: { actor: OWNER, generation: 'one' } }}>
      <ConnectedChatPage /></GuiCapabilitiesProvider>);
    const button = await screen.findByRole('button', { name: /worker/ });
    fail = true;
    fireEvent.click(button);
    expect(await screen.findByText('历史消息加载失败。')).toBeInTheDocument();
    expect(screen.getByText('会话目录读取失败：offline')).toBeInTheDocument();
    expect(screen.queryByText('暂无消息。')).not.toBeInTheDocument();
  });

  it('does not retry a failed directory read in a polling loop on the page', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    try {
      let fail = false;
      let calls = 0;
      const read: MessageReadApi = {
        query: queryOnly,
        conversations: async () => {
          calls += 1;
          if (fail) throw new Error('offline');
          return { entries: [], nextCursor: null };
        },
        history: async () => ({ entries: [], nextCursor: null }),
        lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
      };
      const view = render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead: read,
        chatTargets: async () => [targetRow()], chatSession: { actor: OWNER, generation: 'one' } }}>
        <ConnectedChatPage /></GuiCapabilitiesProvider>);
      try {
        await act(() => vi.advanceTimersByTimeAsync(0));
        const button = screen.getByRole('button', { name: /worker/ });
        const beforeOpen = calls;
        fail = true;
        fireEvent.click(button);
        await act(() => vi.advanceTimersByTimeAsync(0));
        expect(screen.getByText('历史消息加载失败。')).toBeInTheDocument();
        expect(screen.getByText('会话目录读取失败：offline')).toBeInTheDocument();
        expect(screen.queryByText('暂无消息。')).not.toBeInTheDocument();
        expect(calls).toBe(beforeOpen + 1);
        for (let elapsed = 0; elapsed < 10_000; elapsed += 2_000) {
          await act(() => vi.advanceTimersByTimeAsync(2_000));
        }
        expect(calls).toBe(beforeOpen + 1);
      } finally {
        view.unmount();
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps a first send unread until its document is attached, then reads it', async () => {
    const { heads, send, read } = world();
    let listed = false;
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => listed ? read.conversations() : { entries: [], nextCursor: null },
      history: read.history, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    await controller.refresh();

    expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
      conversationId: DEFAULT, documentId: null, historyStatus: 'unread', nextCursor: null,
    });
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first']);

    listed = true;
    expect(heads.map(row => row.documentId)).toEqual([DEFAULT_DOCUMENT]);
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(Date.now() + 60_000);
    await controller.refresh();
    vi.useRealTimers();
    expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
      conversationId: DEFAULT, documentId: DEFAULT_DOCUMENT, historyStatus: 'ready',
    });
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => [row.id, row.text])).toEqual([['sent-1', 'first']]);
  });

  it('shows an unattached first send without claiming a complete conversation', async () => {
    const { send, read } = world();
    const messageRead: MessageReadApi = {
      query: async () => [], conversations: async () => ({ entries: [], nextCursor: null }),
      history: read.history, lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    };
    render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead, chatSend: send,
      chatTargets: async () => [targetRow()], chatSession: { actor: OWNER, generation: 'one' } }}>
      <ConnectedChatPage openedTargetIds={[TARGET]} /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    fireEvent.change(await screen.findByRole('textbox', { name: /的消息$/ }), { target: { value: 'first' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    const log = await screen.findByRole('log');
    expect(await within(log).findByText('first')).toBeInTheDocument();
    expect(screen.getByText('历史尚未读取，这里只显示你刚发送的消息。')).toBeInTheDocument();
    expect(screen.queryByText('当前显示上次取得的消息。')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无消息。')).not.toBeInTheDocument();
  });

  it('reads the newest document when one peer lists two heads under one send id', async () => {
    const NEWER = 'c.dddddddddddddddddddddddddd';
    for (const order of ['newest-first', 'oldest-first'] as const) {
      const heads = [
        { documentId: NEWER, conversationId: RANDOM, peer: TARGET, lastMessageAtMs: 2000 },
        head(RANDOM_DOCUMENT, RANDOM, 1000),
      ];
      const history = vi.fn(async (): Promise<HistoryPage> => ({ entries: [], nextCursor: null }));
      const controller = new ConnectedChatController({
        targets: async () => [targetRow()], query: async () => [],
        conversations: async () => ({ entries: order === 'newest-first' ? heads : [...heads].reverse(), nextCursor: null }),
        history, identity: OWNER,
      });
      await controller.start();
      expect(controller.getSnapshot().sessions.map(row => row.documentId)).toEqual([NEWER]);
      controller.select(TARGET);
      await waitFor(() => expect(history).toHaveBeenCalledWith(NEWER, undefined, 'tail'));
      expect(history).toHaveBeenCalledTimes(1);
    }
  });
});

describe('the directory re-read after a first send is bounded', () => {
  afterEach(() => { vi.useRealTimers(); });

  async function sendFirst(conversations: MessageReadApi['conversations']) {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    const { send, read } = world();
    const messageRead: MessageReadApi = {
      query: async () => [], conversations,
      history: read.history, lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    };
    render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead, chatSend: send,
      chatTargets: async () => [targetRow()], chatSession: { actor: OWNER, generation: 'one' } }}>
      <ConnectedChatPage openedTargetIds={[TARGET]} /></GuiCapabilitiesProvider>);
    await act(() => vi.advanceTimersByTimeAsync(0));
    fireEvent.click(screen.getByRole('button', { name: /worker/ }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    fireEvent.change(screen.getByRole('textbox', { name: /的消息$/ }), { target: { value: 'first' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await act(() => vi.advanceTimersByTimeAsync(0));
  }

  async function runFor(ms: number) {
    for (let elapsed = 0; elapsed < ms; elapsed += 1000) await act(() => vi.advanceTimersByTimeAsync(1000));
  }

  it('re-reads at most six times with growing gaps and then shows the failure', async () => {
    const reads: number[] = [];
    await sendFirst(async () => { reads.push(Date.now()); return { entries: [], nextCursor: null }; });
    await runFor(10 * 60_000);
    const start = reads[0];
    const attach = reads.slice(1).map(at => at - start);
    expect(attach).toHaveLength(6);
    const gaps = attach.slice(1).map((at, index) => at - attach[index]);
    gaps.forEach((gap, index) => expect(gap).toBeGreaterThanOrEqual(2000 * 2 ** index));
    expect(screen.getByText('历史消息加载失败。')).toBeInTheDocument();
    expect(screen.getByText('会话目录里还没有这段会话，暂时读不到历史消息。可点“刷新消息”重试。')).toBeInTheDocument();
    expect(within(screen.getByRole('log')).getByText('first')).toBeInTheDocument();
    const settled = reads.length;
    await runFor(10 * 60_000);
    expect(reads).toHaveLength(settled);
  });

  it('shows the last read failure after the bounded attempts', async () => {
    let calls = 0;
    await sendFirst(async () => {
      calls += 1;
      if (calls > 1) throw new Error('offline');
      return { entries: [], nextCursor: null };
    });
    await runFor(10 * 60_000);
    expect(calls).toBe(7);
    expect(screen.getByText('会话目录读取失败：offline')).toBeInTheDocument();
  });

  it('stops at once when the directory method is refused', async () => {
    let calls = 0;
    await sendFirst(async () => {
      calls += 1;
      if (calls > 1) throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND');
      return { entries: [], nextCursor: null };
    });
    await runFor(10 * 60_000);
    expect(calls).toBe(2);
    expect(screen.queryByText('历史消息加载失败。')).not.toBeInTheDocument();
  });

  it('makes no re-read when the directory was refused at start-up', async () => {
    let calls = 0;
    await sendFirst(async () => {
      calls += 1;
      throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND');
    });
    await runFor(10 * 60_000);
    expect(calls).toBe(1);
    expect(screen.queryByText('历史消息加载失败。')).not.toBeInTheDocument();
    expect(within(screen.getByRole('log')).getByText('first')).toBeInTheDocument();
  });
});

describe('follow-ups left from PR 2937', () => {
  afterEach(() => { vi.useRealTimers(); });

  function silentDirectory() {
    const state = { listed: false, calls: 0, hold: null as null | (() => void), holdNext: false };
    const { heads, send, read } = world();
    const conversations = async () => {
      state.calls += 1;
      if (state.holdNext) {
        state.holdNext = false;
        await new Promise<void>(resolve => { state.hold = resolve; });
        return { entries: [], nextCursor: null };
      }
      return state.listed ? read.conversations() : { entries: [], nextCursor: null };
    };
    return { state, heads, send, read, conversations };
  }

  async function advanceAndRefresh(controller: ConnectedChatController, ms = 70_000) {
    vi.setSystemTime(Date.now() + ms);
    await controller.refresh();
  }

  it('clears a directory error through a user-caused read once the head is listed', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    const { state, send, read, conversations } = silentDirectory();
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [], conversations,
      history: read.history, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    for (let i = 0; i < 8; i += 1) await advanceAndRefresh(controller);
    expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ historyStatus: 'error' });

    state.listed = true;
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
    const entry = controller.getSnapshot().entries[TARGET];
    expect(entry.historyError).toBeUndefined();
    expect(entry.messages.map(row => row.text)).toEqual(['first']);
  });

  it('keeps the locally sent message when reopening the conversation fails', async () => {
    const { heads, send } = world();
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [...heads], nextCursor: null }),
      history: async () => { throw new Error('offline'); }, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('error'));
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first']);
  });

  function sendWhileLoading(failTail: boolean) {
    const { heads, send, read } = world();
    const gate: { release: () => void } = { release: () => undefined };
    let blocked = false;
    const history = async (documentId: string, cursor?: string, mode?: string): Promise<HistoryPage> => {
      if (blocked) await new Promise<void>(resolve => { gate.release = resolve; });
      if (failTail) throw new Error('offline');
      return read.history(documentId, cursor, mode);
    };
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [...heads], nextCursor: null }),
      history, send, identity: OWNER,
    });
    return { controller, gate, block: () => { blocked = true; }, unblock: () => { blocked = false; } };
  }

  it('keeps a message sent while the reopen was loading when the tail read then fails', async () => {
    const { controller, gate, block, unblock } = sendWhileLoading(true);
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    block();
    controller.select(TARGET);
    await settle();
    expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('loading');
    unblock();
    await controller.send(TARGET, 'second');
    gate.release();
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('error'));
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first', 'second']);
  });

  it('keeps a message sent while the reopen was loading when the tail read succeeds', async () => {
    const { controller, gate, block, unblock } = sendWhileLoading(false);
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    block();
    controller.select(TARGET);
    await settle();
    unblock();
    await controller.send(TARGET, 'second');
    gate.release();
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first', 'second']);
  });

  it('keeps the read rows and a message sent while loading when the reopen is refused by method', async () => {
    const { heads, send, read } = world();
    const gate: { release: () => void } = { release: () => undefined };
    let hold = false;
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [...heads], nextCursor: null }),
      history: async (documentId, cursor, mode) => {
        if (!hold) return read.history(documentId, cursor, mode);
        await new Promise<void>(resolve => { gate.release = resolve; });
        throw new MethodRefusedError('conversations.history', 'METHOD_NOT_FOUND');
      },
      send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
    hold = true;
    controller.select(TARGET);
    await settle();
    expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('loading');
    await controller.send(TARGET, 'second');
    gate.release();
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first', 'second']);
  });

  it('keeps the locally sent message when the directory read fails on reopen', async () => {
    const { send, read } = world();
    let fail = false;
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => { if (fail) throw new Error('offline'); return { entries: [], nextCursor: null }; },
      history: read.history, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    fail = true;
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('error'));
    expect(controller.getSnapshot().entries[TARGET].historyError).toBe('会话目录读取失败：offline');
    expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.text)).toEqual(['first']);
  });

  it('says only the sent messages are shown when an error entry holds nothing else', async () => {
    const { send, read } = world();
    const messageRead: MessageReadApi = {
      query: async () => [], conversations: read.conversations,
      history: async () => { throw new Error('offline'); }, lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    };
    render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead, chatSend: send,
      chatTargets: async () => [targetRow()], chatSession: { actor: OWNER, generation: 'one' } }}>
      <ConnectedChatPage openedTargetIds={[TARGET]} /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    fireEvent.change(await screen.findByRole('textbox', { name: /的消息$/ }), { target: { value: 'first' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    const log = await screen.findByRole('log');
    expect(await within(log).findByText('first')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    expect(await screen.findByText('历史消息加载失败。')).toBeInTheDocument();
    expect(within(screen.getByRole('log')).getByText('first')).toBeInTheDocument();
    expect(screen.getByText('历史尚未读取，这里只显示你刚发送的消息。')).toBeInTheDocument();
    expect(screen.queryByText('当前显示上次取得的消息。')).not.toBeInTheDocument();
  });

  it('does not spend the last attempt on a read whose result was discarded', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    const { state, send, read, conversations } = silentDirectory();
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [], conversations,
      history: read.history, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    const base = state.calls;
    await controller.send(TARGET, 'first');
    while (state.calls - base < 5) await advanceAndRefresh(controller);
    state.holdNext = true;
    vi.setSystemTime(Date.now() + 70_000);
    const pending = controller.refresh();
    await controller.retry();
    state.hold?.();
    await pending;
    expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('unread');

    state.listed = true;
    await advanceAndRefresh(controller);
    expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ historyStatus: 'ready' });
  });
});

describe('a send in the error state is a user-caused read', () => {
  function erroredChat() {
    const { heads, documents, send, read } = world();
    const state = { failing: true, tails: 0 };
    const history = async (documentId: string, cursor?: string, mode?: string): Promise<HistoryPage> => {
      state.tails += 1;
      if (state.failing) throw new Error('offline');
      return read.history(documentId, cursor, mode);
    };
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [...heads], nextCursor: null }),
      history, send, identity: OWNER,
    });
    return { controller, state, documents };
  }

  async function intoError(controller: ConnectedChatController) {
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('error'));
  }

  it('re-reads once and becomes ready with the document rows and the sent row, without a duplicate', async () => {
    const { controller, state } = erroredChat();
    await intoError(controller);
    state.failing = false;
    const before = state.tails;
    await controller.send(TARGET, 'second');
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
    const entry = controller.getSnapshot().entries[TARGET];
    expect(entry.historyError).toBeUndefined();
    expect(entry.messages.map(row => [row.id, row.text])).toEqual([['sent-1', 'first'], ['sent-2', 'second']]);
    expect(state.tails - before).toBe(1);
  });

  it('keeps the sent row and stays in error when the re-read fails', async () => {
    const { controller, state } = erroredChat();
    await intoError(controller);
    const before = state.tails;
    await controller.send(TARGET, 'second');
    await settle();
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('error'));
    const entry = controller.getSnapshot().entries[TARGET];
    expect(entry.operation?.status).toBe('success');
    expect(entry.messages.map(row => row.text)).toEqual(['first', 'second']);
    expect(state.tails - before).toBe(1);
  });

  it('does not read after a send that was not accepted', async () => {
    const { heads, read } = world();
    let tails = 0;
    const failing = createChatSendApi({
      async send() { throw new Error('offline'); },
      async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
    }, { storage: memoryStorage().storage, randomBytes: () => new Uint8Array(32).fill(8) });
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [...heads], nextCursor: null }),
      history: async (...args) => { tails += 1; if (tails === 1) throw new Error('offline'); return read.history(...args); },
      send: failing, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'x');
    expect(tails).toBe(0);
  });

  it('does not read on a send in the ready state', async () => {
    const { heads, send, read } = world();
    heads.unshift(head(DEFAULT_DOCUMENT, DEFAULT, 3000));
    let tails = 0;
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [...heads], nextCursor: null }),
      history: async (...args) => { tails += 1; return read.history(...args); }, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await waitFor(() => expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready'));
    const before = tails;
    await controller.send(TARGET, 'second');
    await settle();
    expect(tails).toBe(before);
    expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('ready');
  });

  it('does not read on a send in the unread state', async () => {
    const { send } = world();
    let tails = 0;
    const controller = new ConnectedChatController({
      targets: async () => [targetRow()], query: async () => [],
      conversations: async () => ({ entries: [], nextCursor: null }),
      history: async () => { tails += 1; return { entries: [], nextCursor: null }; }, send, identity: OWNER,
    });
    await controller.start();
    controller.select(TARGET);
    await controller.send(TARGET, 'first');
    await settle();
    expect(tails).toBe(0);
    expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('unread');
  });
});
