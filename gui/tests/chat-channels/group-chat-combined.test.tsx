/**
 * Decision record (combined group chat send + admin page test, slices GC5-2 + GC5-3, 2026-11-21):
 * - Verifies that on ConnectedChatPage with both `groupPost` and `groupAdmin`
 *   wired, opening a group renders both the composer and the 「成员」 button.
 * - Clicking 「成员」 issues exactly 1 `conversations.get` call and 0
 *   `conversations.post` calls; sending a message issues exactly 1
 *   `conversations.post` call and 0 `conversations.get` calls.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import type { GroupAdminApi, GroupPostApi } from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';

describe('ConnectedChatPage combined group send and admin', () => {
  it('shows both composer and 成员 button in a group and keeps conversations.get and conversations.post isolated 1:1', async () => {
    const user = userEvent.setup();
    const getGroupSpy = vi.fn<GroupAdminApi['getGroup']>(async (conversationId) => ({
      conversationId,
      title: 'release',
      createdBy: OWNER,
      createdAtMs: 1000,
      self: OWNER,
      canManageMembers: true,
      members: [
        { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 1000, addedBy: OWNER },
        { memberId: WORKER_ID, kind: 'agent', displayName: 'worker', manager: false, joinedAtMs: 1001, addedBy: OWNER },
      ],
    }));
    const postSpy = vi.fn<GroupPostApi['post']>(async ({ conversationId }) => ({
      status: 'accepted',
      operationKey: 'k'.repeat(43),
      conversationId,
      messageId: 'm-new-1',
      duplicate: false,
      notice: '消息已提交。',
    }));

    const capabilities: GuiCapabilities = {
      ...createWebGuiCapabilities(),
      chatTargets: async () => [],
      messageRead: {
        query: async () => [],
        conversations: async () => ({
          entries: [],
          groups: [{ conversationId: GROUP_ID, title: 'release', memberCount: 2, lastMessageAtMs: 2000 }],
          nextCursor: null,
          recordAvailable: true,
        }),
        history: async () => ({ entries: [], nextCursor: null }),
        lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
      },
      groupChatRead: {
        history: async () => ({ entries: [], nextCursor: null }),
      },
      groupPost: {
        post: postSpy,
      },
      groupAdmin: {
        createGroup: async () => { throw new Error('unexpected createGroup'); },
        getGroup: getGroupSpy,
        addMember: async () => { throw new Error('unexpected addMember'); },
        removeMember: async () => { throw new Error('unexpected removeMember'); },
      },
      chatSession: { actor: OWNER, generation: 'g-1' },
    };

    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    await user.click(within(groupRegion).getByRole('button', { name: /release/ }));

    const membersButton = await screen.findByRole('button', { name: '成员' });
    const composerInput = screen.getByLabelText('给 release 的消息');
    expect(membersButton).toBeVisible();
    expect(composerInput).toBeVisible();
    expect(getGroupSpy).not.toHaveBeenCalled();
    expect(postSpy).not.toHaveBeenCalled();

    await user.click(membersButton);
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    expect(getGroupSpy).toHaveBeenCalledWith(GROUP_ID);
    expect(postSpy).not.toHaveBeenCalled();
    expect(await screen.findByRole('region', { name: '群成员管理' })).toBeVisible();

    await user.type(composerInput, 'hello merged group');
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(postSpy).toHaveBeenCalledTimes(1);
    expect(postSpy).toHaveBeenCalledWith({
      conversationId: GROUP_ID,
      message: 'hello merged group',
    });
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('hello merged group')).toBeVisible();
  });
});
