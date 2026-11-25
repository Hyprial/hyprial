/**
 * Decision record (group members panel tests, slice GC5-3, 2026-11-21):
 * - Verifies ConnectedChatPage + GroupMembersPanel: `conversations.get` is
 *   triggered only when clicking 「成员」 in the group header, renders
 *   `displayName` and `kind`, hides management controls when `canManageMembers`
 *   is false, never renders a remove button on the owner/manager row, requires
 *   Dialog confirmation before removal, updates members from mutation answers
 *   without re-reading `conversations.get`, and supports same-key retry after
 *   unknown outcomes.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { DomainError, TransportError } from '../../src/client';
import type { AgentsApi } from '../../src/features/agents';
import type { GroupAdminApi, GroupDetail } from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
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

function makeDetail(canManageMembers = true): GroupDetail {
  return {
    conversationId: GROUP_ID,
    title: 'release',
    createdBy: OWNER,
    createdAtMs: 1000,
    self: OWNER,
    canManageMembers,
    members: [
      { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 1000, addedBy: OWNER },
      { memberId: WORKER_ID, kind: 'agent', displayName: 'worker', manager: false, joinedAtMs: 1001, addedBy: OWNER },
    ],
  };
}

function makeCapabilities(overrides: Partial<GroupAdminApi> = {}): GuiCapabilities {
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
    groupAdmin: {
      createGroup: async () => makeDetail(),
      getGroup: overrides.getGroup ?? (async () => makeDetail()),
      addMember: overrides.addMember ?? (async ({ conversationId }) => ({
        conversationId,
        entryId: 'e-add',
        members: [
          ...makeDetail().members,
          { memberId: REVIEWER_ID, kind: 'agent', displayName: 'reviewer', manager: false, joinedAtMs: 3000, addedBy: OWNER },
        ],
      })),
      removeMember: overrides.removeMember ?? (async ({ conversationId }) => ({
        conversationId,
        entryId: 'e-rem',
        members: [makeDetail().members[0]],
      })),
    },
    chatSession: { actor: OWNER, generation: 'g-1' },
  };
}

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

describe('GroupMembersPanel on ConnectedChatPage', () => {
  it('loads members only on clicking 成员, omits remove on owner row, confirms remove in Dialog, and updates without re-reading getGroup', async () => {
    const user = userEvent.setup();
    const getGroupSpy = vi.fn(async () => makeDetail(true));
    const removeSpy = vi.fn<GroupAdminApi['removeMember']>(async ({ conversationId }) => ({
      conversationId,
      entryId: 'e-rem',
      members: [makeDetail().members[0]],
    }));
    const addSpy = vi.fn<GroupAdminApi['addMember']>(async ({ conversationId }) => ({
      conversationId,
      entryId: 'e-add',
      members: [
        ...makeDetail().members,
        { memberId: REVIEWER_ID, kind: 'agent', displayName: 'reviewer', manager: false, joinedAtMs: 3000, addedBy: OWNER },
      ],
    }));

    render(
      <GuiCapabilitiesProvider value={makeCapabilities({ getGroup: getGroupSpy, removeMember: removeSpy, addMember: addSpy })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    await user.click(within(groupRegion).getByRole('button', { name: /release/ }));
    expect(getGroupSpy).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '成员' }));
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    expect(getGroupSpy).toHaveBeenCalledWith(GROUP_ID);

    const panel = await screen.findByRole('region', { name: '群成员管理' });
    expect(await within(panel).findByText('成员名单（2）')).toBeVisible();
    expect(within(panel).getByText('alice')).toBeVisible();
    expect(within(panel).getByText('用户 · 所有者')).toBeVisible();
    expect(within(panel).getByText('worker')).toBeVisible();
    expect(within(panel).getByText('Agent')).toBeVisible();
    expect(within(panel).queryByRole('button', { name: '移除 alice' })).not.toBeInTheDocument();

    await user.click(within(panel).getByRole('button', { name: '移除 worker' }));
    const confirmDialog = screen.getByRole('dialog', { name: '移除成员？' });
    await user.click(within(confirmDialog).getByRole('button', { name: '取消' }));
    expect(removeSpy).not.toHaveBeenCalled();

    await user.click(within(panel).getByRole('button', { name: '移除 worker' }));
    await user.click(within(screen.getByRole('dialog', { name: '移除成员？' })).getByRole('button', { name: '确认移除' }));
    expect(removeSpy).toHaveBeenCalledTimes(1);
    expect(removeSpy).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: GROUP_ID,
      memberId: WORKER_ID,
    }));
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(within(panel).getByText('成员名单（1）')).toBeVisible());
    expect(within(panel).queryByText('worker')).not.toBeInTheDocument();

    await user.click(within(panel).getByRole('combobox', { name: '添加成员' }));
    await user.click(screen.getByRole('option', { name: 'reviewer' }));
    await user.click(within(panel).getByRole('button', { name: '添加' }));
    expect(addSpy).toHaveBeenCalledTimes(1);
    expect(addSpy).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: GROUP_ID,
      member: REVIEWER_ID,
    }));
    expect(getGroupSpy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(within(panel).getByText('成员名单（3）')).toBeVisible());
    expect(within(panel).getByText('reviewer')).toBeVisible();
  });

  it('hides add and remove controls when canManageMembers is false', async () => {
    const user = userEvent.setup();
    render(
      <GuiCapabilitiesProvider value={makeCapabilities({ getGroup: async () => makeDetail(false) })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    await user.click(within(groupRegion).getByRole('button', { name: /release/ }));
    await user.click(screen.getByRole('button', { name: '成员' }));

    const panel = await screen.findByRole('region', { name: '群成员管理' });
    expect(await within(panel).findByText('alice')).toBeVisible();
    expect(within(panel).queryByRole('button', { name: /移除/ })).not.toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: '添加' })).not.toBeInTheDocument();
    expect(within(panel).queryByRole('combobox', { name: '添加成员' })).not.toBeInTheDocument();
  });

  it('shows unknown + same-key retry and rejection Notice for add/remove mutations', async () => {
    const user = userEvent.setup();
    const removeKeys: string[] = [];
    let removeMode: 'unknown' | 'rejected' | 'ok' = 'unknown';

    render(
      <GuiCapabilitiesProvider value={makeCapabilities({
        addMember: async () => {
          throw new DomainError('AGENT_NOT_FOUND', 'missing', {}, {
            method: 'conversations.members.add',
            requestId: 'r-1',
            status: 404,
            dispatch: 'dispatched',
            catalogDigest: 'd',
            attempts: 1,
            startedAt: 1,
            durationMs: 1,
          });
        },
        removeMember: async ({ conversationId, idempotencyKey }) => {
          removeKeys.push(idempotencyKey);
          if (removeMode === 'unknown') {
            throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timeout', {
              method: 'conversations.members.remove',
              requestId: null,
              status: null,
              dispatch: 'unknown',
              catalogDigest: null,
              attempts: 1,
              startedAt: 1,
              durationMs: 1,
            });
          }
          if (removeMode === 'rejected') {
            throw new DomainError('INVALID_ARGUMENT', 'invalid', {}, {
              method: 'conversations.members.remove',
              requestId: 'r-2',
              status: 400,
              dispatch: 'dispatched',
              catalogDigest: 'd',
              attempts: 1,
              startedAt: 1,
              durationMs: 1,
            });
          }
          return { conversationId, entryId: 'e-rem', members: [makeDetail().members[0]] };
        },
      })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    fireEvent.click(within(groupRegion).getByRole('button', { name: /release/ }));
    fireEvent.click(screen.getByRole('button', { name: '成员' }));
    const panel = await screen.findByRole('region', { name: '群成员管理' });
    await within(panel).findByText('worker');

    await user.click(within(panel).getByRole('combobox', { name: '添加成员' }));
    await user.click(screen.getByRole('option', { name: 'reviewer' }));
    await user.click(within(panel).getByRole('button', { name: '添加' }));
    expect(await within(panel).findByText(/找不到这个 Agent/)).toBeVisible();

    await user.click(within(panel).getByRole('button', { name: '移除 worker' }));
    const dialog = screen.getByRole('dialog', { name: '移除成员？' });
    await user.click(within(dialog).getByRole('button', { name: '确认移除' }));
    expect(await within(dialog).findByText('结果未知。')).toBeVisible();

    removeMode = 'ok';
    await user.click(within(dialog).getByRole('button', { name: '重试' }));
    expect(removeKeys).toHaveLength(2);
    expect(removeKeys[1]).toBe(removeKeys[0]);
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '移除成员？' })).not.toBeInTheDocument());
  });
});
