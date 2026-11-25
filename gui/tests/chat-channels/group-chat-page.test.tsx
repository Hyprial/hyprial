/**
 * Decision record (group chat page rendering tests, slice GC5-1, 2026-11-21):
 * - Verifies ConnectedChatPage + AgentChatSection rendering for Slice 1:
 *   top "群聊" group, no availability badge on groups, hiding the group section
 *   when empty and confirmed, "群聊列表状态未确认。" when unknown, inline
 *   membership events without folding while keeping 1:1 system folds intact,
 *   sender names, reply references, mentions, absent composer in groups,
 *   not-member/not-found states, and DAEMON_UNAVAILABLE retained messages.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { HttpError } from '../../src/client';
import type { GroupChatReadApi, HistoryPage, MessageReadApi } from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const REVIEWER_ID = 'a.revieweraaaaaaaaaaaaaaaaaa';
const WORKER_URI = 'agent:alice:node:worker';

const groupHead = {
  conversationId: GROUP_ID,
  title: 'release',
  memberCount: 3,
  lastMessageAtMs: 5000,
};

const groupEntries: HistoryPage['entries'] = [
  {
    kind: 'message', messageId: 'm-1', conversationId: GROUP_ID, from: OWNER, to: GROUP_ID,
    intent: 'request', createdAtMs: 1000, message: 'alice created the group "release"', origin: {},
    messageClass: 'system', system: { event: 'created', memberId: OWNER },
  },
  {
    kind: 'message', messageId: 'm-2', conversationId: GROUP_ID, from: OWNER, to: GROUP_ID,
    intent: 'request', createdAtMs: 1001, message: 'alice added worker', origin: {},
    messageClass: 'system', system: { event: 'member-added', memberId: WORKER_ID },
  },
  {
    kind: 'message', messageId: 'm-3', conversationId: GROUP_ID, from: OWNER, to: GROUP_ID,
    intent: 'request', createdAtMs: 1002, message: 'alice removed reviewer', origin: {},
    messageClass: 'system', system: { event: 'member-removed', memberId: REVIEWER_ID },
  },
  {
    kind: 'message', messageId: 'm-4', conversationId: GROUP_ID, from: REVIEWER_ID, to: GROUP_ID,
    intent: 'request', createdAtMs: 1003, message: 'reviewer left', origin: {},
    messageClass: 'system', system: { event: 'member-left', memberId: REVIEWER_ID },
  },
  {
    kind: 'message', messageId: 'm-5', conversationId: GROUP_ID, from: OWNER, to: GROUP_ID,
    intent: 'request', createdAtMs: 2000, message: 'please cut the release', origin: {},
    messageClass: 'interaction',
  },
  {
    kind: 'message', messageId: 'm-6', conversationId: GROUP_ID, from: WORKER_ID, to: GROUP_ID,
    intent: 'reply', createdAtMs: 3000, message: 'tag is pushed', origin: {},
    messageClass: 'interaction',
    replyTo: { messageId: 'm-5', from: OWNER, excerpt: 'please cut the release' },
    mentions: [OWNER, 'a.deletedaaaaaaaaaaaaaaaaaaa'],
  },
];

function makeCapabilities(overrides: {
  conversations?: MessageReadApi['conversations'];
  groupHistory?: GroupChatReadApi['history'];
  directHistory?: MessageReadApi['history'];
  withGroupPort?: boolean;
} = {}): GuiCapabilities {
  const read: MessageReadApi = {
    query: async () => [],
    conversations: overrides.conversations ?? (async () => ({
      entries: [{ documentId: 'c.1', conversationId: 'conv-1', peer: WORKER_URI, lastMessageAtMs: 100 }],
      groups: [groupHead],
      nextCursor: null,
      recordAvailable: true,
    })),
    history: overrides.directHistory ?? (async () => ({
      entries: [{
        kind: 'message', messageId: 'd-1', conversationId: 'conv-1', from: WORKER_URI, to: OWNER,
        intent: 'request', createdAtMs: 100, message: 'PAC dispatch', origin: {}, messageClass: 'system',
      }],
      nextCursor: null,
    })),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  return {
    ...createWebGuiCapabilities(),
    chatTargets: async () => [{
      targetKind: 'agent', targetUri: WORKER_URI, actor: WORKER_URI, deliverable: true,
      agentId: WORKER_ID, displayName: 'worker', status: 'online',
    }],
    messageRead: read,
    ...(overrides.withGroupPort === false ? {} : {
      groupChatRead: {
        history: overrides.groupHistory ?? (async () => ({ entries: groupEntries, nextCursor: null })),
      },
    }),
    chatSession: { actor: OWNER, generation: 'g-1' },
  };
}

describe('ConnectedChatPage group chat read', () => {
  it('lists groups at the top, renders messages/replies/mentions/Chinese membership rows without a composer, and preserves 1:1 system fold', async () => {
    render(<GuiCapabilitiesProvider value={makeCapabilities()}><ConnectedChatPage /></GuiCapabilitiesProvider>);

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    const groupButton = within(groupRegion).getByRole('button', { name: /release/ });
    expect(within(groupButton).getByText('3 位成员')).toBeVisible();
    expect(groupButton.querySelector('.badge')).toBeNull();

    fireEvent.click(groupButton);
    const log = await screen.findByRole('log', { name: '群聊 release 的消息' });
    expect(await within(log).findByText('你创建了群聊「release」')).toBeVisible();
    expect(within(log).getByText('你添加了 worker')).toBeVisible();
    expect(within(log).getByText(`你移除了 ${REVIEWER_ID}`)).toBeVisible();
    expect(within(log).getByText(`${REVIEWER_ID} 退出了群聊`)).toBeVisible();
    expect(within(log).queryByText('任务与系统消息')).not.toBeInTheDocument();
    expect(within(log).getByText('please cut the release')).toBeVisible();
    expect(within(log).getByText('tag is pushed')).toBeVisible();
    expect(within(log).getByText('回复 你：please cut the release')).toBeVisible();
    expect(within(log).getByText('@你 @a.deletedaaaaaaaaaaaaaaaaaaa')).toBeVisible();
    expect(within(log).getByText('worker')).toBeVisible();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '信息' }));
    const info = screen.getByRole('complementary', { name: '群聊信息' });
    expect(within(info).getByText('群聊')).toBeVisible();
    expect(within(info).queryByText('主机')).not.toBeInTheDocument();
    expect(within(info).queryByText('所有者')).not.toBeInTheDocument();
    expect(within(info).queryByText('可见组织')).not.toBeInTheDocument();

    const agentRegion = screen.getByRole('group', { name: '其他可联系' });
    fireEvent.click(within(agentRegion).getByRole('button', { name: /worker/ }));
    expect(await screen.findByText('任务与系统消息')).toBeInTheDocument();
  });

  it('hides the group section when there are no groups and recordAvailable is true', async () => {
    render(<GuiCapabilitiesProvider value={makeCapabilities({
      conversations: async () => ({ entries: [{ documentId: 'c.1', conversationId: 'conv-1', peer: WORKER_URI, lastMessageAtMs: 100 }], groups: [], nextCursor: null, recordAvailable: true }),
    })}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    await screen.findByRole('button', { name: /worker/ });
    expect(screen.queryByRole('group', { name: '群聊' })).not.toBeInTheDocument();
  });

  it('shows unconfirmed group list notice when recordAvailable is false and refreshes on click', async () => {
    let calls = 0;
    render(<GuiCapabilitiesProvider value={makeCapabilities({
      conversations: async () => {
        calls += 1;
        return calls === 1
          ? { entries: [], groups: [], nextCursor: null, recordAvailable: false }
          : { entries: [], groups: [groupHead], nextCursor: null, recordAvailable: true };
      },
    })}><ConnectedChatPage /></GuiCapabilitiesProvider>);

    expect(await screen.findByText('群聊列表状态未确认。')).toBeVisible();
    expect(screen.queryByText('暂无聊天对象')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新列表' }));
    expect(await screen.findByRole('button', { name: /release/ })).toBeVisible();
    expect(screen.queryByText('群聊列表状态未确认。')).not.toBeInTheDocument();
  });

  it('supports loadMore, CONVERSATION_NOT_MEMBER, and DAEMON_UNAVAILABLE with retained messages', async () => {
    let mode: 'ok' | 'not-member' | 'unavailable' = 'ok';
    const historyCalls = vi.fn(async (_id: string, cursor?: string) => {
      if (mode === 'not-member') {
        throw new HttpError('CONVERSATION_NOT_MEMBER', 'removed', 403, {
          method: 'conversations.history', requestId: 'r-1', status: 403, dispatch: 'dispatched',
          catalogDigest: 'd', attempts: 1, startedAt: 1, durationMs: 1,
        });
      }
      if (mode === 'unavailable') {
        throw new HttpError('DAEMON_UNAVAILABLE', 'unavailable', 503, {
          method: 'conversations.history', requestId: 'r-2', status: 503, dispatch: 'dispatched',
          catalogDigest: 'd', attempts: 1, startedAt: 1, durationMs: 1,
        });
      }
      if (cursor === 'older-1') {
        return {
          entries: [{ ...groupEntries[4], messageId: 'm-0', message: 'earlier request', createdAtMs: 500 }],
          nextCursor: null,
        };
      }
      return { entries: [groupEntries[4]], nextCursor: 'older-1', nextOlderCursor: 'older-1' };
    });

    render(<GuiCapabilitiesProvider value={makeCapabilities({ groupHistory: historyCalls })}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: /release/ }));
    expect(await screen.findByText('please cut the release')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '加载更多消息' }));
    expect(await screen.findByText('earlier request')).toBeVisible();

    mode = 'unavailable';
    fireEvent.click(screen.getByRole('button', { name: '刷新消息' }));
    expect(await screen.findByText('当前显示上次取得的消息。')).toBeVisible();
    expect(screen.getByText('please cut the release')).toBeVisible();

    mode = 'not-member';
    fireEvent.click(screen.getByRole('button', { name: '刷新消息' }));
    expect(await screen.findByText('你已不在这个群里。')).toBeVisible();
    const headingActions = document.querySelector('.chat-workspace__heading-actions') as HTMLElement;
    await waitFor(() => expect(within(headingActions).getByRole('button', { name: '刷新列表' })).toBeVisible());
  });
});
