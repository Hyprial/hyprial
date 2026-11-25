/**
 * Decision record (group chat send & reply page tests, slice GC5-2, 2026-11-21):
 * - Verifies `ConnectedChatPage` + `GroupSendController`:
 *   1. All four send states (`in-flight`, `accepted`, `definitive-failure`,
 *      `unknown`) with draft preservation, revision-to-resend after refusal,
 *      no retry button on `unknown`, and dismiss-unknown button.
 *   2. Immediate display of accepted message with「已发送」and deduplicated
 *      replacement when the next history refresh returns the same `messageId`.
 *   3. Reply button on ordinary messages only (never on membership events),
 *      「正在回复 <名字>：<摘要>」context row above the composer, cancel reply,
 *      and `replyTo` sent in `conversations.post`.
 *   4. One click = one request (rapid repeat clicks blocked while in-flight),
 *      plus controller unit checks for zero extra calls on non-send actions.
 *   5. A group send in `unknown` never blocks 1:1 `message.send`.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useReducer, type ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { HttpError, MethodRefusedError, type MethodAccess } from '../../src/client';
import { createGeneratedClient } from '../../src/client/generated';
import { NotificationsProvider } from '../../src/components/notifications';
import { Rail } from '../../src/components/Rail';
import { ToastHost } from '../../src/components/ToastHost';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import {
  GROUP_POST_UNKNOWN_NOTICE,
  GroupSendController,
  createChatSendApi,
  createGroupPostApiFromClient,
  type ChatSendStorage,
  type GroupChatReadApi,
  type GroupPostApi,
  type HistoryPage,
  type MessageReadApi,
} from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';
import { appReducer } from '../../src/state/appReducer';
import { DEMO_BASELINE } from '../support/demo-baseline';
import { expectUnavailableToast } from '../support/notify-harness';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const WORKER_URI = 'agent:alice:node:worker';

const groupHead = {
  conversationId: GROUP_ID,
  title: 'release',
  memberCount: 2,
  lastMessageAtMs: 5000,
};

const baseEntries: HistoryPage['entries'] = [
  {
    kind: 'message',
    messageId: 'm-sys-1',
    conversationId: GROUP_ID,
    from: OWNER,
    to: GROUP_ID,
    intent: 'request',
    createdAtMs: 1000,
    message: 'alice created the group "release"',
    origin: {},
    messageClass: 'system',
    system: { event: 'created', memberId: OWNER },
  },
  {
    kind: 'message',
    messageId: 'm-worker-1',
    conversationId: GROUP_ID,
    from: WORKER_ID,
    to: GROUP_ID,
    intent: 'request',
    createdAtMs: 2000,
    message: 'ready to tag release',
    origin: {},
    messageClass: 'interaction',
  },
];

function memoryStorage(): ChatSendStorage {
  const values = new Map<string, string>();
  return {
    getItem: name => values.get(name) ?? null,
    setItem(name, value) { values.set(name, value); },
    removeItem(name) { values.delete(name); },
  };
}

function NotifyShell({ children }: { readonly children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
  return <NotificationsProvider dispatch={dispatch}>
    {children}
    <ToastHost notices={state.notices} dispatch={dispatch} />
    <Rail page="chat" accountMenuOpen={false} dispatch={dispatch}
      notifications={{ open: state.notificationCenterOpen, unread: state.centerUnread, items: state.centerNotices }} />
  </NotificationsProvider>;
}

function makeCapabilities(options: {
  postInvoke?: (params: Record<string, unknown>) => Promise<unknown>;
  groupHistory?: GroupChatReadApi['history'];
  directSend?: (params: unknown) => Promise<unknown>;
  access?: MethodAccess;
  groupPost?: GroupPostApi;
} = {}) {
  const postCalls: Array<Record<string, unknown>> = [];
  const directCalls: unknown[] = [];
  const client = createGeneratedClient({
    invoke: async <T,>(method: string, params: unknown = {}): Promise<T> => {
      const record = params as Record<string, unknown>;
      if (method === 'conversations.post') {
        postCalls.push(record);
        if (options.postInvoke) return options.postInvoke(record) as Promise<T>;
        return {
          ok: true,
          actingPrincipal: OWNER,
          operationId: record.idempotencyKey,
          conversationId: GROUP_ID,
          messageId: 'm-sent-1',
          deliveries: [{ accepted: true, conversationId: GROUP_ID, messageId: 'm-sent-1', queued: false, target: GROUP_ID }],
        } as T;
      }
      throw new Error(`unexpected method: ${method}`);
    },
  });

  const messageRead: MessageReadApi = {
    query: async () => [],
    conversations: async () => ({
      entries: [{ documentId: 'c.1', conversationId: 'conv-1', peer: WORKER_URI, lastMessageAtMs: 100 }],
      groups: [groupHead],
      nextCursor: null,
      recordAvailable: true,
    }),
    history: async () => ({ entries: [], nextCursor: null }),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };

  const chatSend = createChatSendApi({
    send: async (params) => {
      directCalls.push(params);
      if (options.directSend) return options.directSend(params);
      return {
        ok: true,
        actingPrincipal: OWNER,
        operationId: params.idempotencyKey,
        conversationId: 'conv-1',
        messageId: 'd-sent-1',
        deliveries: [{ target: WORKER_URI, messageId: 'd-sent-1', accepted: true, queued: false }],
      };
    },
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage: memoryStorage(), randomBytes: () => new Uint8Array(32).fill(5) });

  const capabilities: GuiCapabilities = {
    ...createWebGuiCapabilities(),
    chatTargets: async () => [{
      targetKind: 'agent',
      targetUri: WORKER_URI,
      actor: WORKER_URI,
      deliverable: true,
      agentId: WORKER_ID,
      displayName: 'worker',
      status: 'online',
    }],
    messageRead,
    groupChatRead: {
      history: options.groupHistory ?? (async () => ({ entries: baseEntries, nextCursor: null })),
    },
    groupPost: options.groupPost ?? createGroupPostApiFromClient(client, {
      access: options.access,
      randomBytes: () => new Uint8Array(32).fill(3),
    }),
    chatSend,
    chatSession: { actor: OWNER, generation: 'g-1' },
  };

  return { capabilities, postCalls, directCalls };
}

describe('GroupSendController request budget', () => {
  it('issues 1 port call for send and 0 for draft, reply target, clear, and dismiss', async () => {
    const postSpy = vi.fn(async () => ({
      status: 'unknown' as const,
      operationKey: 'k'.repeat(43),
      reason: 'dispatch-unknown' as const,
      notice: GROUP_POST_UNKNOWN_NOTICE,
    }));
    const controller = new GroupSendController({ post: { post: postSpy }, identity: OWNER });

    controller.setDraft(GROUP_ID, 'hello');
    controller.setReplyTarget(GROUP_ID, {
      id: 'm-1',
      targetId: GROUP_ID,
      author: 'assistant',
      authorName: 'worker',
      text: 'ready',
    });
    controller.clearReplyTarget(GROUP_ID);
    expect(postSpy).toHaveBeenCalledTimes(0);

    await controller.send(GROUP_ID, 'hello');
    expect(postSpy).toHaveBeenCalledTimes(1);

    await controller.send(GROUP_ID, 'blocked while unknown');
    expect(postSpy).toHaveBeenCalledTimes(1);

    controller.dismissUnknown(GROUP_ID);
    expect(postSpy).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().drafts[GROUP_ID]).toBe('hello');
  });
});

describe('ConnectedChatPage group send and reply', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('handles in-flight (one click one request), accepted optimistic row, and id-based replacement on refresh', async () => {
    let resolvePost!: (value: unknown) => void;
    let historyPhase = 0;
    const historySpy = vi.fn(async (_id: string, _cursor?: string, mode?: string) => {
      historyPhase += 1;
      const sentEntry: HistoryPage['entries'][number] = {
        kind: 'message',
        messageId: 'm-sent-1',
        conversationId: GROUP_ID,
        from: OWNER,
        to: GROUP_ID,
        intent: 'request',
        createdAtMs: 6000,
        message: 'ship v1.0',
        origin: {},
        messageClass: 'interaction',
      };
      if (historyPhase >= 2) {
        return {
          entries: mode === 'after' ? [sentEntry] : [...baseEntries, sentEntry],
          nextCursor: null,
          headCursor: 'h-2',
        };
      }
      return { entries: baseEntries, nextCursor: null, headCursor: 'h-1' };
    });

    const { capabilities, postCalls } = makeCapabilities({
      groupHistory: historySpy,
      postInvoke: () => new Promise(resolve => { resolvePost = resolve; }),
    });

    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    const log = await screen.findByRole('log', { name: '群聊 release 的消息' });
    await within(log).findByText('ready to tag release');

    const input = screen.getByLabelText('给 release 的消息');
    fireEvent.change(input, { target: { value: 'ship v1.0' } });

    const sendButton = screen.getByRole('button', { name: '发送' });
    fireEvent.click(sendButton);
    fireEvent.click(sendButton);
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(postCalls).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveTextContent('发送中，请等待结果。');
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(historySpy).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePost({
        ok: true,
        actingPrincipal: OWNER,
        operationId: postCalls[0].idempotencyKey,
        conversationId: GROUP_ID,
        messageId: 'm-sent-1',
        deliveries: [{ accepted: true, conversationId: GROUP_ID, messageId: 'm-sent-1', queued: false, target: GROUP_ID }],
      });
    });

    expect(await within(log).findByText('ship v1.0')).toBeVisible();
    expect(within(log).getByText('已发送')).toBeVisible();
    expect(input).toHaveValue('');
    expect(historySpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: '刷新消息' }));
    await waitFor(() => expect(historySpy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(log).getAllByText('ship v1.0')).toHaveLength(1));
    expect(within(log).queryByText('已发送')).not.toBeInTheDocument();
  });

  it('supports replying only to ordinary messages and sends replyTo in conversations.post', async () => {
    const { capabilities, postCalls } = makeCapabilities();
    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);

    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    const log = await screen.findByRole('log', { name: '群聊 release 的消息' });
    await within(log).findByText('ready to tag release');

    const sysArticle = within(log).getByText('你创建了群聊「release」').closest('article')!;
    expect(within(sysArticle).queryByRole('button', { name: '回复' })).not.toBeInTheDocument();

    const workerArticle = within(log).getByText('ready to tag release').closest('article')!;
    const replyButton = within(workerArticle).getByRole('button', { name: '回复' });
    fireEvent.click(replyButton);
    expect(postCalls).toHaveLength(0);

    expect(screen.getByText('正在回复 worker：ready to tag release')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '取消回复' }));
    expect(screen.queryByText(/正在回复/)).not.toBeInTheDocument();

    fireEvent.click(replyButton);
    expect(screen.getByText('正在回复 worker：ready to tag release')).toBeVisible();

    fireEvent.change(screen.getByLabelText('给 release 的消息'), { target: { value: 'go ahead' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(postCalls[0].replyTo).toBe('m-worker-1');
    expect(screen.queryByText(/正在回复/)).not.toBeInTheDocument();
    expect(within(log).getByText('回复 worker：ready to tag release')).toBeVisible();
  });

  it('preserves draft on refusal, allows revising to resend, and hides composer when not-member', async () => {
    let fail = true;
    const { capabilities, postCalls } = makeCapabilities({
      postInvoke: async (params) => {
        if (fail) {
          throw new HttpError('INVALID_ARGUMENT', 'bad summary', 400, {
            method: 'conversations.post',
            requestId: 'r-bad',
            status: 400,
            dispatch: 'dispatched',
            catalogDigest: 'd',
            attempts: 1,
            startedAt: 1,
            durationMs: 1,
          });
        }
        return {
          ok: true,
          actingPrincipal: OWNER,
          operationId: params.idempotencyKey,
          conversationId: GROUP_ID,
          messageId: 'm-retry-ok',
          deliveries: [{ accepted: true, conversationId: GROUP_ID, messageId: 'm-retry-ok', queued: false, target: GROUP_ID }],
        };
      },
    });

    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    const input = await screen.findByLabelText('给 release 的消息');
    fireEvent.change(input, { target: { value: 'initial draft' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('发送失败：发送参数无效，本次未发送。');
    expect(input).toHaveValue('initial draft');

    fail = false;
    fireEvent.change(input, { target: { value: 'revised draft' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(postCalls).toHaveLength(2));
    expect(input).toHaveValue('');
  });

  it('shows unknown notice without retry button, preserves draft, allows dismissing, and keeps 1:1 send working', async () => {
    const { capabilities, postCalls, directCalls } = makeCapabilities({
      postInvoke: async () => { throw new Error('socket closed'); },
    });

    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    const groupInput = await screen.findByLabelText('给 release 的消息');
    fireEvent.change(groupInput, { target: { value: 'ambiguous group msg' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText(GROUP_POST_UNKNOWN_NOTICE)).toBeVisible();
    expect(groupInput).toHaveValue('ambiguous group msg');
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(postCalls).toHaveLength(1);

    const agentRegion = screen.getByRole('group', { name: '其他可联系' });
    fireEvent.click(within(agentRegion).getByRole('button', { name: /worker/ }));
    const directInput = await screen.findByLabelText('给 worker 的消息');
    fireEvent.change(directInput, { target: { value: 'direct ping' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(directCalls).toHaveLength(1));
    expect(await screen.findByText('direct ping')).toBeVisible();

    const groupRegion = screen.getByRole('group', { name: '群聊' });
    fireEvent.click(within(groupRegion).getByRole('button', { name: /release/ }));
    expect(await screen.findByText(GROUP_POST_UNKNOWN_NOTICE)).toBeVisible();
    expect(screen.getByLabelText('给 release 的消息')).toHaveValue('ambiguous group msg');

    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    expect(screen.queryByText(GROUP_POST_UNKNOWN_NOTICE)).not.toBeInTheDocument();
    expect(screen.getByLabelText('给 release 的消息')).toHaveValue('ambiguous group msg');
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    expect(postCalls).toHaveLength(1);
  });

  it('hides composer on CONVERSATION_NOT_MEMBER history and notifies when conversations.post is refused', async () => {
    const refused = makeCapabilities({
      access: (method) => method === 'conversations.post'
        ? { available: false, code: 'METHOD_NOT_FOUND' }
        : { available: true },
    });

    const view = render(<NotifyShell>
      <GuiCapabilitiesProvider value={refused.capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>
    </NotifyShell>);

    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    const input = await screen.findByLabelText('给 release 的消息');
    fireEvent.change(input, { target: { value: 'try refused post' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expectUnavailableToast('发送消息暂不可用。'));
    expect(refused.postCalls).toHaveLength(0);
    expect(input).toHaveValue('try refused post');
    view.unmount();

    const notMember = makeCapabilities({
      groupHistory: async () => {
        throw new HttpError('CONVERSATION_NOT_MEMBER', 'removed', 403, {
          method: 'conversations.history',
          requestId: 'r-nm',
          status: 403,
          dispatch: 'dispatched',
          catalogDigest: 'd',
          attempts: 1,
          startedAt: 1,
          durationMs: 1,
        });
      },
    });
    render(<GuiCapabilitiesProvider value={notMember.capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    expect(await screen.findByText('你已不在这个群里。')).toBeVisible();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('throws MethodRefusedError check safely when post port itself rejects before dispatch', async () => {
    const { capabilities } = makeCapabilities({
      postInvoke: async () => { throw new MethodRefusedError('conversations.post', 'METHOD_NOT_CALLABLE'); },
    });
    render(<NotifyShell>
      <GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>
    </NotifyShell>);
    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    const input = await screen.findByLabelText('给 release 的消息');
    fireEvent.change(input, { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expectUnavailableToast('发送消息暂不可用。'));
    expect(input).toHaveValue('hello');
  });
});
