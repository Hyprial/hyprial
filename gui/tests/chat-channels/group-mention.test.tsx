/**
 * Decision record (group mention picker tests, slice GC5-4, 2026-11-21):
 * - Covers `ConnectedChatPage` + `GroupMentionPicker` + `GroupSendController` +
 *   `GroupAdminController`:
 *   1. Clicking `@` issues at most 1 `conversations.get` when members are not yet
 *      cached, and 0 when already loaded via 「成员」, previous `@`, or group
 *      creation.
 *   2. Multi-selecting and deselecting agent members (excluding self/user rows),
 *      displaying removable chips in the composer context row alongside
 *      「正在回复 …」, sending `mentions` in `conversations.post`, and clearing
 *      selected mentions on acceptance.
 *   3. Pre-selecting the replied-to agent when replying to an agent message
 *      (visible and removable), while pre-selecting no one on own messages or
 *      membership events.
 *   4. Keyboard operation: Tab to `@` button, Enter to open, Escape to close
 *      and restore focus to the `@` button.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import type { AgentsApi } from '../../src/features/agents';
import {
  GroupSendController,
  type GroupAdminApi,
  type GroupDetail,
  type GroupPostApi,
  type HistoryPage,
} from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const NEW_GROUP_ID = 'g.newgroupaaaaaaaaaaaaaaaaaa';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const REVIEWER_ID = 'a.revieweraaaaaaaaaaaaaaaaaa';
const WORKER_URI = 'agent:alice:node:worker';
const REVIEWER_URI = 'agent:alice:node:reviewer';

function makeAgentRow(uri: string, actor: string, agentId: string) {
  return {
    actor,
    agentId,
    uri,
    displayName: actor,
    owner: 'alice',
    host: 'node',
    provider: null,
    model: null,
    harness: null,
    preferredHarness: null,
    capabilities: [],
    runState: { runtime: 'online' as const },
    cwd: null,
    homeMtimeMs: null,
    createdAtMs: null,
    hasLastSessionId: null,
    lastActiveAtMs: null,
  };
}

function makeDetail(conversationId = GROUP_ID): GroupDetail {
  return {
    conversationId,
    title: 'release',
    createdBy: OWNER,
    createdAtMs: 1000,
    self: OWNER,
    canManageMembers: true,
    members: [
      { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 1000, addedBy: OWNER },
      { memberId: WORKER_ID, kind: 'agent', displayName: 'worker', manager: false, joinedAtMs: 1001, addedBy: OWNER },
      { memberId: REVIEWER_ID, kind: 'agent', displayName: 'reviewer', manager: false, joinedAtMs: 1002, addedBy: OWNER },
    ],
  };
}

const sampleEntries: HistoryPage['entries'] = [
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
    messageId: 'm-owner-1',
    conversationId: GROUP_ID,
    from: OWNER,
    to: GROUP_ID,
    intent: 'request',
    createdAtMs: 1500,
    message: 'please check release',
    origin: {},
    messageClass: 'interaction',
  },
  {
    kind: 'message',
    messageId: 'm-worker-1',
    conversationId: GROUP_ID,
    from: WORKER_ID,
    to: GROUP_ID,
    intent: 'reply',
    createdAtMs: 2000,
    message: 'ready to tag',
    origin: {},
    messageClass: 'interaction',
  },
];

function makeCapabilities(options: {
  getGroup?: GroupAdminApi['getGroup'];
  createGroup?: GroupAdminApi['createGroup'];
  post?: GroupPostApi['post'];
  entries?: HistoryPage['entries'];
} = {}): GuiCapabilities {
  return {
    ...createWebGuiCapabilities(),
    agents: {
      list: async () => [
        makeAgentRow(WORKER_URI, 'worker', WORKER_ID),
        makeAgentRow(REVIEWER_URI, 'reviewer', REVIEWER_ID),
      ],
    } as AgentsApi,
    chatTargets: async () => [
      { targetKind: 'agent', targetUri: WORKER_URI, actor: WORKER_URI, deliverable: true, agentId: WORKER_ID, displayName: 'worker' },
      { targetKind: 'agent', targetUri: REVIEWER_URI, actor: REVIEWER_URI, deliverable: true, agentId: REVIEWER_ID, displayName: 'reviewer' },
    ],
    messageRead: {
      query: async () => [],
      conversations: async () => ({
        entries: [],
        groups: [{ conversationId: GROUP_ID, title: 'release', memberCount: 3, lastMessageAtMs: 3000 }],
        nextCursor: null,
        recordAvailable: true,
      }),
      history: async () => ({ entries: [], nextCursor: null }),
      lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    },
    groupChatRead: {
      history: async () => ({
        entries: options.entries ?? sampleEntries,
        nextCursor: null,
        memberNames: {
          [OWNER]: 'alice',
          [WORKER_ID]: 'worker',
          [REVIEWER_ID]: 'reviewer',
        },
      }),
    },
    groupPost: {
      post: options.post ?? (async ({ conversationId }) => ({
        status: 'accepted',
        operationKey: 'k'.repeat(43),
        conversationId,
        messageId: 'm-sent-1',
        duplicate: false,
        notice: '消息已提交。',
      })),
    },
    groupAdmin: {
      createGroup: options.createGroup ?? (async ({ title, members }) => ({
        conversationId: NEW_GROUP_ID,
        title,
        createdBy: OWNER,
        createdAtMs: 9000,
        members: [
          { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 9000, addedBy: OWNER },
          ...members.map((id, index) => ({
            memberId: id,
            kind: 'agent' as const,
            displayName: id === WORKER_ID ? 'worker' : 'reviewer',
            manager: false,
            joinedAtMs: 9001 + index,
            addedBy: OWNER,
          })),
        ],
      })),
      getGroup: options.getGroup ?? (async (conversationId) => makeDetail(conversationId)),
      addMember: async ({ conversationId }) => ({ conversationId, entryId: null, members: makeDetail(conversationId).members }),
      removeMember: async ({ conversationId }) => ({ conversationId, entryId: null, members: makeDetail(conversationId).members }),
    },
    chatSession: { actor: OWNER, generation: 'g-1' },
  };
}

describe('GroupSendController mentions budget and pre-selection', () => {
  it('issues 0 port calls on mention toggle/remove/reply and sends non-empty mentions', async () => {
    const postSpy = vi.fn<GroupPostApi['post']>(async ({ conversationId }) => ({
      status: 'accepted',
      operationKey: 'k'.repeat(43),
      conversationId,
      messageId: 'm-1',
      duplicate: false,
      notice: '消息已提交。',
    }));
    const controller = new GroupSendController({ post: { post: postSpy }, identity: OWNER });

    controller.setReplyTarget(GROUP_ID, {
      id: 'm-sys',
      targetId: GROUP_ID,
      author: 'system',
      text: 'alice created',
      membershipEvent: true,
    });
    expect(controller.getSnapshot().mentions[GROUP_ID]).toBeUndefined();

    controller.setReplyTarget(GROUP_ID, {
      id: 'm-own',
      targetId: GROUP_ID,
      author: 'user',
      text: 'self message',
    });
    expect(controller.getSnapshot().mentions[GROUP_ID]).toBeUndefined();

    controller.setReplyTarget(GROUP_ID, {
      id: 'm-agent',
      targetId: GROUP_ID,
      author: 'assistant',
      authorId: WORKER_ID,
      authorName: 'worker',
      text: 'ready',
    });
    expect(controller.getSnapshot().mentions[GROUP_ID]).toEqual([WORKER_ID]);

    controller.toggleMention(GROUP_ID, REVIEWER_ID);
    expect(controller.getSnapshot().mentions[GROUP_ID]).toEqual([WORKER_ID, REVIEWER_ID]);

    controller.removeMention(GROUP_ID, WORKER_ID);
    expect(controller.getSnapshot().mentions[GROUP_ID]).toEqual([REVIEWER_ID]);
    expect(postSpy).not.toHaveBeenCalled();

    await controller.send(GROUP_ID, 'please review');
    expect(postSpy).toHaveBeenCalledTimes(1);
    expect(postSpy).toHaveBeenCalledWith({
      conversationId: GROUP_ID,
      message: 'please review',
      replyTo: 'm-agent',
      mentions: [REVIEWER_ID],
    });
    expect(controller.getSnapshot().mentions[GROUP_ID]).toEqual([]);
  });
});

describe('GroupMentionPicker on ConnectedChatPage', () => {
  it('fetches conversations.get at most once on @, supports multi-select, remove, send, and clears on success', async () => {
    const user = userEvent.setup();
    const getGroupSpy = vi.fn<GroupAdminApi['getGroup']>(async (id) => makeDetail(id));
    const postSpy = vi.fn<GroupPostApi['post']>(async ({ conversationId }) => ({
      status: 'accepted',
      operationKey: 'k'.repeat(43),
      conversationId,
      messageId: 'm-sent-1',
      duplicate: false,
      notice: '消息已提交。',
    }));

    render(
      <GuiCapabilitiesProvider value={makeCapabilities({ getGroup: getGroupSpy, post: postSpy })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    await user.click(within(groupRegion).getByRole('button', { name: /release/ }));
    expect(getGroupSpy).not.toHaveBeenCalled();

    const mentionButton = await screen.findByRole('button', { name: '@ 成员' });
    await user.click(mentionButton);
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    expect(getGroupSpy).toHaveBeenCalledWith(GROUP_ID);

    const popover = await screen.findByRole('dialog', { name: '选择 @ 成员' });
    expect(within(popover).queryByRole('checkbox', { name: /alice/ })).not.toBeInTheDocument();
    const workerBox = within(popover).getByRole('checkbox', { name: /worker/ });
    const reviewerBox = within(popover).getByRole('checkbox', { name: /reviewer/ });

    await user.click(workerBox);
    await user.click(reviewerBox);
    expect(workerBox).toBeChecked();
    expect(reviewerBox).toBeChecked();

    const selectedGroup = screen.getByRole('group', { name: '已选 @ 成员' });
    expect(within(selectedGroup).getByText('@worker')).toBeVisible();
    expect(within(selectedGroup).getByText('@reviewer')).toBeVisible();

    await user.click(reviewerBox);
    expect(reviewerBox).not.toBeChecked();
    expect(within(selectedGroup).queryByText('@reviewer')).not.toBeInTheDocument();

    await user.click(reviewerBox);
    expect(within(selectedGroup).getByText('@reviewer')).toBeVisible();

    await user.click(within(selectedGroup).getByRole('button', { name: '移除 @worker' }));
    expect(within(selectedGroup).queryByText('@worker')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '选择 @ 成员' })).not.toBeInTheDocument();

    await user.click(mentionButton);
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    expect(within(screen.getByRole('dialog', { name: '选择 @ 成员' })).getByRole('checkbox', { name: /worker/ })).not.toBeChecked();

    await user.click(mentionButton);
    expect(screen.queryByRole('dialog', { name: '选择 @ 成员' })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText('给 release 的消息'), 'please review now');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(postSpy).toHaveBeenCalledTimes(1));
    expect(postSpy).toHaveBeenCalledWith({
      conversationId: GROUP_ID,
      message: 'please review now',
      mentions: [REVIEWER_ID],
    });
    expect(screen.queryByRole('group', { name: '已选 @ 成员' })).not.toBeInTheDocument();
  });

  it('skips conversations.get on @ when members were already loaded via 成员 or group creation', async () => {
    const user = userEvent.setup();
    const getGroupSpy = vi.fn<GroupAdminApi['getGroup']>(async (id) => makeDetail(id));
    const createSpy = vi.fn<GroupAdminApi['createGroup']>(async ({ title, members }) => ({
      conversationId: NEW_GROUP_ID,
      title,
      createdBy: OWNER,
      createdAtMs: 9000,
      members: [
        { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 9000, addedBy: OWNER },
        ...members.map((id, index) => ({
          memberId: id,
          kind: 'agent' as const,
          displayName: id === WORKER_ID ? 'worker' : 'reviewer',
          manager: false,
          joinedAtMs: 9001 + index,
          addedBy: OWNER,
        })),
      ],
    }));

    render(
      <GuiCapabilitiesProvider value={makeCapabilities({ getGroup: getGroupSpy, createGroup: createSpy })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    await user.click(within(groupRegion).getByRole('button', { name: /release/ }));
    await user.click(await screen.findByRole('button', { name: '成员' }));
    expect(getGroupSpy).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: '@ 成员' }));
    const popover = await screen.findByRole('dialog', { name: '选择 @ 成员' });
    expect(within(popover).getByRole('checkbox', { name: /worker/ })).toBeVisible();
    expect(getGroupSpy).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: '新建群聊' }));
    const createDialog = screen.getByRole('dialog', { name: '新建群聊' });
    await within(createDialog).findByText('worker');
    await user.type(within(createDialog).getByLabelText(/群聊名称/), 'fresh-group');
    await user.click(within(createDialog).getByRole('checkbox', { name: /worker/ }));
    await user.click(within(createDialog).getByRole('button', { name: '创建' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建群聊' })).not.toBeInTheDocument());

    await user.click(within(groupRegion).getByRole('button', { name: /fresh-group/ }));
    await user.click(await screen.findByRole('button', { name: '@ 成员' }));
    const newPopover = await screen.findByRole('dialog', { name: '选择 @ 成员' });
    expect(within(newPopover).getByRole('checkbox', { name: /worker/ })).toBeVisible();
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
  });

  it('pre-selects replied agent with removal support, ignores own/system messages, and works via keyboard', async () => {
    const user = userEvent.setup();
    const postSpy = vi.fn<GroupPostApi['post']>(async ({ conversationId }) => ({
      status: 'accepted',
      operationKey: 'k'.repeat(43),
      conversationId,
      messageId: 'm-sent-2',
      duplicate: false,
      notice: '消息已提交。',
    }));

    render(
      <GuiCapabilitiesProvider value={makeCapabilities({ post: postSpy })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    await user.click(within(groupRegion).getByRole('button', { name: /release/ }));
    const log = await screen.findByRole('log', { name: '群聊 release 的消息' });
    await within(log).findByText('ready to tag');

    const sysArticle = within(log).getByText('你创建了群聊「release」').closest('article')!;
    expect(within(sysArticle).queryByRole('button', { name: '回复' })).not.toBeInTheDocument();

    const ownArticle = within(log).getByText('please check release').closest('article')!;
    await user.click(within(ownArticle).getByRole('button', { name: '回复' }));
    expect(screen.getByText('正在回复 你：please check release')).toBeVisible();
    expect(screen.queryByRole('group', { name: '已选 @ 成员' })).not.toBeInTheDocument();

    const workerArticle = within(log).getByText('ready to tag').closest('article')!;
    await user.click(within(workerArticle).getByRole('button', { name: '回复' }));
    expect(screen.getByText('正在回复 worker：ready to tag')).toBeVisible();
    const selectedGroup = screen.getByRole('group', { name: '已选 @ 成员' });
    expect(within(selectedGroup).getByText('@worker')).toBeVisible();

    await user.click(within(selectedGroup).getByRole('button', { name: '移除 @worker' }));
    expect(screen.queryByRole('group', { name: '已选 @ 成员' })).not.toBeInTheDocument();
    expect(screen.getByText('正在回复 worker：ready to tag')).toBeVisible();

    await user.click(within(workerArticle).getByRole('button', { name: '回复' }));
    expect(within(screen.getByRole('group', { name: '已选 @ 成员' })).getByText('@worker')).toBeVisible();

    const input = screen.getByLabelText('给 release 的消息');
    input.focus();
    expect(input).toHaveFocus();
    await user.tab();
    const mentionButton = screen.getByRole('button', { name: '@ 成员' });
    expect(mentionButton).toHaveFocus();

    await user.keyboard('{Enter}');
    const popover = await screen.findByRole('dialog', { name: '选择 @ 成员' });
    expect(popover).toBeVisible();
    expect(within(popover).getByRole('checkbox', { name: /worker/ })).toBeChecked();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: '选择 @ 成员' })).not.toBeInTheDocument();
    expect(mentionButton).toHaveFocus();
  });
});
