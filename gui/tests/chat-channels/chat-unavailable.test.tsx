/*
 * Decision record (unavailable copy to notifications, PR 1f/F3 chat, 2026-11-14):
 * - A chat function this composition has no port for is not rendered: no send
 *   banner, no composer, no read-only form, no disabled manage button, no
 *   directory placeholder.
 * - A port whose method is refused stays operable; the refusal is reported by
 *   the notification mechanism (`${subject}暂不可用。`, warning, source
 *   `method · CODE`) when the user operates it, and nothing stays inline.
 * - The deleted symbols must not reappear in src.
 * - Slice G1 (2026-11-18): history is `conversations.history` on the document a
 *   `conversations.list` head names, so the shared read port lists the `c1`
 *   head (document `c.c1`) and the refusals name `conversations.history`.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useReducer, type ReactNode } from 'react';
import { PageAvailabilityProvider } from '../../src/app/page-availability';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { MethodRefusedError, type MethodAccess } from '../../src/client';
import { NotificationsProvider } from '../../src/components/notifications';
import { Rail } from '../../src/components/Rail';
import { ToastHost } from '../../src/components/ToastHost';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import { ConnectedChatController, MessageSessionsSection, SingleAgentChatSend, createChatSendApi, type ChatSendStorage, type MessageReadApi } from '../../src/features/chat-channels';
import type { AgentDirectoryRow, AgentsApi } from '../../src/features/agents/impl/api';
import { LiveChatPage, type ChatPageRuntime } from '../../src/pages/live/LiveChatPage';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';
import { ChatPage } from '../../src/pages/ChatPage';
import { ChatRefusalHint } from '../../src/features/org';
import { OrgDomainProvider } from '../../src/features/org/impl/domain/context';
import { buildOrgDevPreview } from '../../src/dev/org/scenarios';
import type { GuiPage } from '../../src/state/gui-view-model';
import { appReducer } from '../../src/state/appReducer';
import { DEMO_BASELINE } from '../support/demo-baseline';
import { expectUnavailableToast } from '../support/notify-harness';

const TARGET = 'agent:owner:node:worker';
const SRC = resolve(import.meta.dirname, '../../src');

function NotifyShell({ children }: { readonly children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
  return <NotificationsProvider dispatch={dispatch}>
    {children}
    <ToastHost notices={state.notices} dispatch={dispatch} />
    <Rail page="chat" accountMenuOpen={false} dispatch={dispatch}
      notifications={{ open: state.notificationCenterOpen, unread: state.centerUnread, items: state.centerNotices }} />
  </NotificationsProvider>;
}

const refusing = (code = 'METHOD_NOT_FOUND'): MethodAccess => () => ({ available: false, code, reason: '目录没有声明该方法。' });

function memoryStorage(): ChatSendStorage {
  const values = new Map<string, string>();
  return {
    getItem: name => values.get(name) ?? null,
    setItem(name, value) { values.set(name, value); },
    removeItem(name) { values.delete(name); },
  };
}

function sendPort() {
  const calls: unknown[] = [];
  return { calls, port: {
    async send(params: unknown) { calls.push(params); return {}; },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  } };
}

const read: MessageReadApi = {
  query: async view => view === 'outbox'
    ? [{ messageId: 'm1', conversationId: 'c1', sender: 'user:owner', recipient: TARGET }] : [],
  history: async () => ({ entries: [], nextCursor: null }),
  lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  conversations: async () => ({ entries: [{ documentId: 'c.c1', conversationId: 'c1', peer: TARGET, lastMessageAtMs: 1 }], nextCursor: null }),
};

function connected(overrides: Partial<GuiCapabilities>): GuiCapabilities {
  return {
    ...createWebGuiCapabilities(), messageRead: read,
    chatTargets: async () => [{ targetKind: 'agent', targetUri: TARGET, deliverable: true, actor: TARGET }],
    ...overrides,
  } as GuiCapabilities;
}

describe('connected chat page without a send port', () => {
  it('renders no composer, no banner and no unavailable copy', async () => {
    render(<GuiCapabilitiesProvider value={connected({})}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    expect(await screen.findByText('暂无消息。')).toBeInTheDocument();
    expect(screen.queryByLabelText(`给 ${TARGET} 的消息`)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
    expect(document.querySelector('.chat-page-status')).toBeNull();
    expect(document.body.textContent).not.toMatch(/暂不可用|只读/);
  });

  it('renders no composer when the trusted session actor is absent', async () => {
    render(<GuiCapabilitiesProvider value={connected({ chatSend: createChatSendApi(sendPort().port, { storage: memoryStorage() }) })}>
      <ConnectedChatPage />
    </GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    expect(await screen.findByText('暂无消息。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
    expect(document.querySelector('.chat-page-status')).toBeNull();
  });

  it('renders nothing when the target directory or the message read port is absent', () => {
    const noTargets = render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    expect(noTargets.container).toBeEmptyDOMElement();
    noTargets.unmount();
    const noRead = render(<GuiCapabilitiesProvider value={connected({ messageRead: undefined })}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    expect(noRead.container).toBeEmptyDOMElement();
  });
});

describe('refused message.send', () => {
  it('keeps the composer operable and reports through a notification on the connected page', async () => {
    const { calls, port } = sendPort();
    const chatSend = createChatSendApi(port, { storage: memoryStorage(), access: refusing() });
    render(<NotifyShell>
      <GuiCapabilitiesProvider value={connected({ chatSend, chatSession: { actor: 'user:owner', generation: 'one' } })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>
    </NotifyShell>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    const input = await screen.findByLabelText(`给 ${TARGET} 的消息`);
    fireEvent.change(input, { target: { value: 'hello' } });
    const sendButton = screen.getByRole('button', { name: '发送' });
    expect(sendButton).toBeEnabled();
    fireEvent.click(sendButton);

    await waitFor(() => expectUnavailableToast('发送消息暂不可用。'));
    expect(calls).toEqual([]);
    expect(screen.getByLabelText(`给 ${TARGET} 的消息`)).toHaveValue('hello');
    expect(screen.getByLabelText(`给 ${TARGET} 的消息`)).toBeEnabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(document.querySelector('.chat-workspace__notice')).toBeNull();
    expect(document.querySelector('.chat-composer__status')!.textContent).not.toMatch(/失败|暂不可用/);

    fireEvent.click(screen.getByRole('button', { name: /通知中心/ }));
    const panel = screen.getByRole('dialog', { name: '通知中心' });
    expect(within(panel).getByText('发送消息暂不可用。')).toBeInTheDocument();
    expect(within(panel).getByText('来源 · message.send · METHOD_NOT_FOUND')).toBeInTheDocument();
  });

  it('reports through a notification in the single-agent send panel', async () => {
    const { calls, port } = sendPort();
    const api = createChatSendApi(port, { storage: memoryStorage(), access: refusing('WRITE_METHOD_UNVERIFIED') });
    render(<NotifyShell>
      <SingleAgentChatSend send={api} identity="user:owner" target={{ uri: TARGET, displayName: 'worker' }} conversationId="c1" />
    </NotifyShell>);
    await screen.findByText('没有可恢复的发送操作键。');
    fireEvent.change(screen.getByLabelText('给 worker 的消息'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expectUnavailableToast('发送消息暂不可用。'));
    expect(calls).toEqual([]);
    expect(screen.getByLabelText('给 worker 的消息')).toHaveValue('hello');
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /通知中心/ }));
    expect(within(screen.getByRole('dialog', { name: '通知中心' })).getByText('来源 · message.send · WRITE_METHOD_UNVERIFIED')).toBeInTheDocument();
  });

  it('classifies a transport refusal before dispatch the same way', async () => {
    const api = createChatSendApi({
      async send() { throw new MethodRefusedError('message.send', 'METHOD_NOT_CALLABLE'); },
      async keyLookup() { return {}; },
    }, { storage: memoryStorage() });
    await expect(api.send({ to: [TARGET], message: 'hello', conversationId: 'c1' })).resolves.toEqual({
      status: 'not-started', reason: 'method-refused', code: 'METHOD_NOT_CALLABLE', source: 'message.send',
    });
    expect(api.pendingOperationKey()).toBeNull();
  });
});

describe('single-agent send without a send port', () => {
  it('renders nothing', () => {
    const view = render(<SingleAgentChatSend />);
    expect(view.container).toBeEmptyDOMElement();
  });
});

const agent = (uri: string): AgentDirectoryRow => ({
  actor: uri.split(':').at(-1)!, uri, displayName: uri, owner: '', host: '', provider: null,
  model: null, harness: null, preferredHarness: null, capabilities: [], runState: { runtime: 'online' },
  cwd: null, homeMtimeMs: null, createdAtMs: null, hasLastSessionId: null, lastActiveAtMs: null,
});
const agentsApi = { list: async () => [agent(TARGET)] } as unknown as AgentsApi;
const historyEntry = { kind: 'message' as const, messageId: 'msg-1', conversationId: 'c1', from: 'user:owner', to: TARGET,
  intent: 'request' as const, createdAtMs: 1000, message: 'hello', origin: {} };
const sessionsApi = (overrides: Partial<MessageReadApi> = {}): MessageReadApi => ({
  ...read,
  conversations: async () => ({ entries: [{ documentId: 'c.c1', conversationId: 'c1', peer: TARGET, lastMessageAtMs: 1 }], nextCursor: null }),
  history: async () => ({ entries: [historyEntry], nextCursor: null }),
  ...overrides,
});

describe('message sessions section', () => {
  it('renders no form, no disabled control, no read-only label and no manage button', async () => {
    render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsApi }}>
      <MessageSessionsSection api={sessionsApi()} />
    </GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: TARGET }));
    expect(await screen.findByText(/hello/)).toBeInTheDocument();
    const root = document.querySelector('.message-sessions')!;
    expect(root.querySelector('form')).toBeNull();
    expect(root.querySelector('input')).toBeNull();
    expect(root.querySelectorAll('[disabled]')).toHaveLength(0);
    expect(screen.queryByRole('button', { name: '管理 Agent' })).not.toBeInTheDocument();
    expect(root.textContent).not.toMatch(/暂不可用|暂不可管理|只读$/);
    expect(root.querySelector('.chat-role')).toBeNull();
    expect(root.querySelector('.chat-readonly-note')).toBeNull();
  });

  it('renders no directory placeholder without an agents port', () => {
    render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}><MessageSessionsSection api={sessionsApi()} /></GuiCapabilitiesProvider>);
    expect(document.querySelector('.chat-list-note')).toBeNull();
    expect(document.body.textContent).not.toContain('Agent 目录暂不可用');
  });

  it('renders no conversation-directory block when its read is refused', async () => {
    render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsApi }}>
      <MessageSessionsSection api={sessionsApi({ conversations: async () => { throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND'); } })} />
    </GuiCapabilitiesProvider>);
    await screen.findByRole('button', { name: TARGET });
    expect(screen.queryByText(/历史会话目录/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无历史会话')).not.toBeInTheDocument();
  });

  it('reports a refused load-more read through a notification and keeps the control', async () => {
    const refused = new MethodRefusedError('conversations.history', 'METHOD_NOT_FOUND');
    render(<NotifyShell>
      <GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsApi }}>
        <MessageSessionsSection api={sessionsApi({
          history: async (_id, cursor) => { if (cursor) throw refused; return { entries: [historyEntry], nextCursor: 'next' }; },
        })} />
      </GuiCapabilitiesProvider>
    </NotifyShell>);
    fireEvent.click(await screen.findByRole('button', { name: TARGET }));
    fireEvent.click(await screen.findByRole('button', { name: '加载更多' }));
    await waitFor(() => expectUnavailableToast('读取消息暂不可用。'));
    expect(screen.getByText(/hello/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '加载更多' })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /通知中心/ }));
    expect(within(screen.getByRole('dialog', { name: '通知中心' })).getByText('来源 · conversations.history · METHOD_NOT_FOUND')).toBeInTheDocument();
  });

  it('is not mounted by the live chat page without a message read port', () => {
    const snapshot = { status: 'ready', targets: [], activeTargetId: null, drafts: {}, entries: {} };
    const live = { status: 'ready', chat: { subscribe: () => () => {}, getSnapshot: () => snapshot } } as unknown as ChatPageRuntime;
    render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}><LiveChatPage live={live} /></GuiCapabilitiesProvider>);
    expect(screen.queryByRole('region', { name: '历史会话目录' })).not.toBeInTheDocument();
    expect(document.querySelector('.message-sessions')).toBeNull();
  });
});

describe('connected chat controller refusals', () => {
  const row = { targetKind: 'agent', targetUri: TARGET, deliverable: true, actor: TARGET, status: 'online' };

  it('reports a refused history read through reportUnavailable and leaves the previous state untouched', async () => {
    const reports: unknown[] = [];
    const refused = new MethodRefusedError('conversations.history', 'METHOD_NOT_FOUND');
    let refuse = false;
    const controller = new ConnectedChatController({
      targets: async () => [row],
      query: async () => [{ messageId: 'm', conversationId: 'c1', from: 'user:owner', to: TARGET, createdAtMs: 1 }],
      conversations: read.conversations,
      history: async () => {
        if (refuse) throw refused;
        return { entries: [historyEntry], nextCursor: 'next' };
      },
      identity: 'user:owner',
      reportUnavailable: (subject, detail) => { reports.push([subject, detail]); },
    });
    await controller.start();
    controller.select(TARGET);
    await act(async () => { await Promise.resolve(); });
    const loaded = controller.getSnapshot().entries[TARGET];
    expect(loaded).toMatchObject({ conversationId: 'c1', historyStatus: 'ready', nextCursor: 'next' });
    expect(loaded.messages).toHaveLength(1);
    expect(reports).toEqual([]);

    refuse = true;
    controller.select(TARGET);
    await act(async () => { await Promise.resolve(); });
    expect(reports).toEqual([['读取消息', { code: 'METHOD_NOT_FOUND', source: 'conversations.history' }]]);
    expect(controller.getSnapshot().entries[TARGET]).toEqual(loaded);
  });

  it('marks a first history read that is refused as unread without a message list or an error', async () => {
    const reports: unknown[] = [];
    const controller = new ConnectedChatController({
      targets: async () => [row],
      query: async () => [{ messageId: 'm', conversationId: 'c1', from: 'user:owner', to: TARGET, createdAtMs: 1 }],
      conversations: read.conversations,
      history: async () => { throw new MethodRefusedError('conversations.history', 'METHOD_NOT_FOUND'); },
      identity: 'user:owner',
      reportUnavailable: (subject, detail) => { reports.push([subject, detail]); },
    });
    await controller.start();
    controller.select(TARGET);
    await act(async () => { await Promise.resolve(); });
    expect(reports).toHaveLength(1);
    expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
      conversationId: 'c1', historyStatus: 'unread', messages: [], historyError: undefined,
    });
  });

  it('does not write a notice for refused auto discovery reads', async () => {
    const controller = new ConnectedChatController({
      targets: async () => [row],
      query: async () => { throw new MethodRefusedError('message.query', 'METHOD_NOT_FOUND'); },
      conversations: async () => { throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND'); },
      identity: 'user:owner',
    });
    await controller.start();
    expect(controller.getSnapshot()).toMatchObject({ status: 'ready', notice: undefined, conversationsUnknown: true });
  });
});

describe('refused history read on the connected page', () => {
  const refused = new MethodRefusedError('conversations.history', 'METHOD_NOT_FOUND');
  const query = async (view: 'inbox' | 'outbox') => view === 'outbox'
    ? [{ messageId: 'm1', conversationId: 'c1', from: 'user:owner', to: TARGET, createdAtMs: 1 }] : [];

  function expectNoInlineClaim() {
    const root = document.querySelector('[data-page="chat"]')!;
    expect(root.querySelector('.chat-page-status')).toBeNull();
    expect(root.querySelector('.chat-workspace__history-state')!.textContent).toBe('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(root.textContent).not.toMatch(/尚未确认|暂无消息|还没有会话|当前显示上次|读取失败|暂不可用/);
  }

  it('raises one notification, keeps the loaded messages and shows no inline line on refresh', async () => {
    let refuse = false;
    const messageRead = { ...read, query, history: async () => {
      if (refuse) throw refused;
      return { entries: [historyEntry], nextCursor: null };
    } };
    render(<NotifyShell>
      <GuiCapabilitiesProvider value={connected({ messageRead })}><ConnectedChatPage /></GuiCapabilitiesProvider>
    </NotifyShell>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    expect(await screen.findByText('hello')).toBeInTheDocument();
    expect(document.querySelector('.toast-host .toast')).toBeNull();

    refuse = true;
    fireEvent.click(screen.getByRole('button', { name: '刷新消息' }));
    await waitFor(() => expectUnavailableToast('读取消息暂不可用。'));
    expect(screen.getByText('hello')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新消息' })).toBeEnabled();
    expectNoInlineClaim();
  });

  it('raises one notification and claims neither an empty list nor a missing conversation on a first read', async () => {
    const messageRead = { ...read, query, history: async () => { throw refused; } };
    render(<NotifyShell>
      <GuiCapabilitiesProvider value={connected({ messageRead, chatSend: createChatSendApi(sendPort().port, { storage: memoryStorage() }),
        chatSession: { actor: 'user:owner', generation: 'one' } })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>
    </NotifyShell>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
    await waitFor(() => expectUnavailableToast('读取消息暂不可用。'));
    expectNoInlineClaim();
    expect(screen.getByLabelText(`给 ${TARGET} 的消息`)).toBeEnabled();
  });
});

describe('refused and failed start-up reads on the connected page', () => {
  const invitation = '还没有会话；发送第一条消息后会开始新会话。';
  const sendable = (): Partial<GuiCapabilities> => ({
    chatSend: createChatSendApi(sendPort().port, { storage: memoryStorage() }),
    chatSession: { actor: 'user:owner', generation: 'one' },
  });
  const empty: MessageReadApi = { ...read, query: async () => [], conversations: async () => ({ entries: [], nextCursor: null }) };

  async function openTarget(value: GuiCapabilities) {
    render(<GuiCapabilitiesProvider value={value}><ConnectedChatPage openedTargetIds={[TARGET]} /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
  }

  it('invites a first message when discovery succeeded and found nothing', async () => {
    await openTarget(connected({ ...sendable(), messageRead: empty }));
    expect(await screen.findByText(invitation)).toBeInTheDocument();
  });

  it.each([
    ['message.query', { query: async () => { throw new MethodRefusedError('message.query', 'METHOD_NOT_FOUND'); } }],
    ['conversations.list', { conversations: async () => { throw new MethodRefusedError('conversations.list', 'METHOD_NOT_FOUND'); } }],
  ])('shows the target without a conversation claim when %s is refused', async (_method, override) => {
    await openTarget(connected({ ...sendable(), messageRead: { ...empty, ...override } }));
    await screen.findByLabelText(`给 ${TARGET} 的消息`);
    const root = document.querySelector('[data-page="chat"]')!;
    expect(root.textContent).not.toContain(invitation);
    expect(root.textContent).not.toContain('发送第一条消息后会开始新会话');
    expect(root.textContent).not.toMatch(/尚未确认|暂无消息|暂不可用/);
    expect(root.querySelector('.chat-workspace__history-state')!.textContent).toBe('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps a dispatched failure of the target read as a failure with a retry, not an empty directory', async () => {
    let calls = 0;
    render(<GuiCapabilitiesProvider value={connected({ chatTargets: async () => {
      calls += 1;
      if (calls === 1) throw new Error('offline');
      return [{ targetKind: 'agent', targetUri: TARGET, deliverable: true, actor: TARGET }];
    } })}>
      <ConnectedChatPage openedTargetIds={[TARGET]} />
    </GuiCapabilitiesProvider>);
    expect(await screen.findByText('聊天对象读取失败：offline')).toBeInTheDocument();
    expect(screen.queryByText('暂无聊天对象')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByRole('button', { name: /worker/ })).toBeInTheDocument();
    expect(screen.queryByText('聊天对象读取失败：offline')).not.toBeInTheDocument();
  });

  it('shows a refused target read that reached the controller as a failure, never as an empty directory', async () => {
    render(<GuiCapabilitiesProvider value={connected({ chatTargets: async () => {
      throw new MethodRefusedError('targets', 'METHOD_NOT_FOUND');
    } })}>
      <ConnectedChatPage />
    </GuiCapabilitiesProvider>);
    expect(await screen.findByText(/聊天对象读取失败/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
    expect(screen.queryByText('暂无聊天对象')).not.toBeInTheDocument();
  });
});

describe('chat entry points into hidden pages', () => {
  function fixturePage(hiddenPages?: ReadonlySet<GuiPage>) {
    const state = { ...createFixtureState(DEMO_BASELINE), infoCollapsed: false };
    return render(<PageAvailabilityProvider hiddenPages={hiddenPages ?? new Set<GuiPage>()}>
      <ChatPage state={state} dispatch={() => undefined} />
    </PageAvailabilityProvider>);
  }

  it('offers 管理 Agent only when the Agent page is available', () => {
    const shown = fixturePage();
    expect(screen.getByRole('button', { name: '管理 Agent' })).toBeEnabled();
    shown.unmount();
    fixturePage(new Set<GuiPage>(['contacts']));
    expect(screen.getByRole('complementary', { name: '对象信息' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '管理 Agent' })).not.toBeInTheDocument();
  });

  it('offers the refusal fixes only for pages that are available', () => {
    const preview = buildOrgDevPreview('owner', { latencyMs: 0 })!;
    const hint = (hiddenPages?: ReadonlySet<GuiPage>) => <PageAvailabilityProvider hiddenPages={hiddenPages ?? new Set<GuiPage>()}>
      <OrgDomainProvider source={preview.source}>
        <ChatRefusalHint messageId="org-chat-squire-2" targetId="agent-squire" dispatch={() => undefined} />
      </OrgDomainProvider>
    </PageAvailabilityProvider>;
    const all = render(hint());
    expect(document.querySelectorAll('.org-actions button').length).toBeGreaterThan(0);
    all.unmount();
    render(hint(new Set<GuiPage>(['contacts', 'org'])));
    expect(document.querySelector('.org-refusal')).not.toBeNull();
    expect(document.querySelectorAll('.org-actions button')).toHaveLength(0);
  });
});

describe('deleted chat copy and code', () => {
  function sources(dir: string): string[] {
    return readdirSync(dir).flatMap(name => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? sources(path) : /\.(ts|tsx|css)$/.test(name) ? [path] : [];
    });
  }
  it.each([
    'AdapterStatusSection',
    'createAdapterStatusApi',
    'CHANNEL_STATES',
    'adapterStatus?:',
    'CHAT_INTERFACE_UNAVAILABLE',
    'CONNECTED_CHAT_SEND_UNAVAILABLE',
    'CONNECTED_CHAT_SESSION_UNCONFIRMED',
    'sendUnavailableReason',
    'ConversationDirectoryUnavailableError',
    'chat-composer-readonly',
    'chat-readonly-note',
    'ChatMessages',
    'composer-wrap',
  ])('has no reference to %s in src', symbol => {
    const hits = sources(SRC).filter(path => readFileSync(path, 'utf8').includes(symbol));
    expect(hits).toEqual([]);
  });
});
