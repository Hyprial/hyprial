/**
 * Decision record (group create dialog tests, slice GC5-3, 2026-11-21):
 * - Covers ConnectedChatPage + GroupCreateDialog: 0 requests when opening the
 *   dialog, candidate filtering (targets with `agentId` intersected with local
 *   agents), submitting `a.<id>` members, validation, keyboard focus entry /
 *   Tab trap / Escape + trigger focus restore, every backend refusal code,
 *   unknown outcome + same-key retry, and D4 behavior (new group placed at top
 *   with highlight, not auto-opened until clicked).
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { DomainError, TransportError } from '../../src/client';
import type { AgentsApi } from '../../src/features/agents';
import type { GroupAdminApi, GroupChatReadApi, GroupCreateMutationState } from '../../src/features/chat-channels';
import { GroupCreateDialog } from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const OWNER = 'user:alice';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const REVIEWER_ID = 'a.revieweraaaaaaaaaaaaaaaaaa';
const REMOTE_ID = 'a.remoteaaaaaaaaaaaaaaaaaaaa';
const WORKER_URI = 'agent:alice:node:worker';
const REVIEWER_URI = 'agent:alice:node:reviewer';
const LEGACY_URI = 'agent:alice:node:legacy';
const REMOTE_URI = 'agent:bob:node:remote';

function makeAgentRow(uri: string, actor: string, agentId: string | null) {
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

function makePageCapabilities(overrides: {
  createGroup?: GroupAdminApi['createGroup'];
  groupHistory?: GroupChatReadApi['history'];
  existingGroups?: Array<{ conversationId: string; title: string; memberCount: number; lastMessageAtMs: number }>;
} = {}): GuiCapabilities {
  return {
    ...createWebGuiCapabilities(),
    agents: {
      list: async () => [
        makeAgentRow(WORKER_URI, 'worker', WORKER_ID),
        makeAgentRow(REVIEWER_URI, 'reviewer', REVIEWER_ID),
        makeAgentRow(LEGACY_URI, 'legacy', null),
      ],
    } as AgentsApi,
    chatTargets: async () => [
      { targetKind: 'agent', targetUri: WORKER_URI, actor: WORKER_URI, deliverable: true, agentId: WORKER_ID, displayName: 'worker' },
      { targetKind: 'agent', targetUri: REVIEWER_URI, actor: REVIEWER_URI, deliverable: true, agentId: REVIEWER_ID, displayName: 'reviewer' },
      { targetKind: 'agent', targetUri: LEGACY_URI, actor: LEGACY_URI, deliverable: true, agentId: null, displayName: 'legacy' },
      { targetKind: 'agent', targetUri: REMOTE_URI, actor: REMOTE_URI, deliverable: true, agentId: REMOTE_ID, displayName: 'remote' },
    ],
    messageRead: {
      query: async () => [],
      conversations: async () => ({
        entries: [{ documentId: 'c.1', conversationId: 'conv-1', peer: WORKER_URI, lastMessageAtMs: 100 }],
        groups: overrides.existingGroups ?? [],
        nextCursor: null,
        recordAvailable: true,
      }),
      history: async () => ({ entries: [], nextCursor: null }),
      lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
    },
    groupChatRead: {
      history: overrides.groupHistory ?? (async () => ({ entries: [], nextCursor: null })),
    },
    groupAdmin: {
      createGroup: overrides.createGroup ?? (async ({ title, members }) => ({
        conversationId: 'g.newgroupaaaaaaaaaaaaaaaaaa',
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
      getGroup: async (conversationId) => ({
        conversationId,
        title: 'release',
        createdBy: OWNER,
        createdAtMs: 9000,
        members: [],
        self: OWNER,
        canManageMembers: true,
      }),
      addMember: async ({ conversationId }) => ({ conversationId, entryId: null, members: [] }),
      removeMember: async ({ conversationId }) => ({ conversationId, entryId: null, members: [] }),
    },
    chatSession: { actor: OWNER, generation: 'g-1' },
  };
}

describe('GroupCreateDialog and ConnectedChatPage group creation', () => {
  it('opens with zero requests, filters candidates to local agents with agentId, handles keyboard/focus, and validates title', async () => {
    const user = userEvent.setup();
    const createSpy = vi.fn<GroupAdminApi['createGroup']>();
    render(<GuiCapabilitiesProvider value={makePageCapabilities({ createGroup: createSpy })}><ConnectedChatPage /></GuiCapabilitiesProvider>);

    const newGroupButton = await screen.findByRole('button', { name: '新建群聊' });
    await screen.findByRole('button', { name: /worker/ });

    await user.click(newGroupButton);
    const dialog = screen.getByRole('dialog', { name: '新建群聊' });
    expect(createSpy).not.toHaveBeenCalled();

    const titleInput = within(dialog).getByLabelText(/群聊名称/);
    expect(titleInput).toHaveFocus();
    expect(await within(dialog).findByText('worker')).toBeVisible();
    expect(within(dialog).getByText('reviewer')).toBeVisible();
    expect(within(dialog).queryByText('legacy')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('remote')).not.toBeInTheDocument();

    const submitButton = within(dialog).getByRole('button', { name: '创建' });
    await user.click(submitButton);
    expect(within(dialog).getByText('请输入群聊名称。')).toBeVisible();
    expect(titleInput).toHaveFocus();
    expect(createSpy).not.toHaveBeenCalled();

    submitButton.focus();
    await user.tab();
    const dialogFocusable = Array.from(dialog.querySelectorAll<HTMLElement>('button, input'));
    expect(dialogFocusable).toContain(document.activeElement);

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: '新建群聊' })).not.toBeInTheDocument();
    expect(newGroupButton).toHaveFocus();
  });

  it('retries with the same idempotencyKey after unknown outcome, prepends and highlights new group without auto-opening (D4)', async () => {
    const user = userEvent.setup();
    const seenKeys: string[] = [];
    let shouldFailUnknown = true;
    const historySpy = vi.fn<GroupChatReadApi['history']>(async () => ({ entries: [], nextCursor: null }));
    const createGroup: GroupAdminApi['createGroup'] = async ({ title, members, idempotencyKey }) => {
      seenKeys.push(idempotencyKey);
      if (shouldFailUnknown) {
        throw new TransportError('network', 'NETWORK_ERROR', 'lost', {
          method: 'conversations.create',
          requestId: null,
          status: null,
          dispatch: 'unknown',
          catalogDigest: null,
          attempts: 1,
          startedAt: 1,
          durationMs: 1,
        });
      }
      return {
        conversationId: 'g.newgroupaaaaaaaaaaaaaaaaaa',
        title,
        createdBy: OWNER,
        createdAtMs: 9000,
        members: [
          { memberId: OWNER, kind: 'user', displayName: 'alice', manager: true, joinedAtMs: 9000, addedBy: OWNER },
          ...members.map((id, index) => ({
            memberId: id,
            kind: 'agent' as const,
            displayName: 'worker',
            manager: false,
            joinedAtMs: 9001 + index,
            addedBy: OWNER,
          })),
        ],
      };
    };

    render(
      <GuiCapabilitiesProvider value={makePageCapabilities({
        createGroup,
        groupHistory: historySpy,
        existingGroups: [{ conversationId: 'g.old', title: 'older-group', memberCount: 2, lastMessageAtMs: 1000 }],
      })}>
        <ConnectedChatPage />
      </GuiCapabilitiesProvider>,
    );

    await user.click(await screen.findByRole('button', { name: '新建群聊' }));
    const dialog = screen.getByRole('dialog', { name: '新建群聊' });
    await within(dialog).findByText('worker');
    await user.type(within(dialog).getByLabelText(/群聊名称/), 'release-v2');
    await user.click(within(dialog).getByRole('checkbox', { name: /worker/ }));
    await user.click(within(dialog).getByRole('button', { name: '创建' }));

    expect(await within(dialog).findByText('结果未知。')).toBeVisible();
    expect(seenKeys).toHaveLength(1);

    shouldFailUnknown = false;
    await user.click(within(dialog).getByRole('button', { name: '重试' }));
    expect(seenKeys).toHaveLength(2);
    expect(seenKeys[1]).toBe(seenKeys[0]);

    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建群聊' })).not.toBeInTheDocument());
    expect(historySpy).not.toHaveBeenCalled();
    expect(screen.getByText('请选择聊天对象')).toBeVisible();

    const groupRegion = screen.getByRole('group', { name: '群聊' });
    const groupButtons = within(groupRegion).getAllByRole('button').filter((btn) => btn.hasAttribute('data-target-id'));
    expect(groupButtons.map((btn) => btn.getAttribute('data-target-id'))).toEqual([
      'g.newgroupaaaaaaaaaaaaaaaaaa',
      'g.old',
    ]);
    expect(groupButtons[0]).toHaveAttribute('data-highlighted', 'true');
    expect(within(groupButtons[0]).getByText('2 位成员')).toBeVisible();

    await user.click(groupButtons[0]);
    expect(historySpy).toHaveBeenCalledTimes(1);
    expect(historySpy).toHaveBeenCalledWith('g.newgroupaaaaaaaaaaaaaaaaaa', undefined, 'tail');
    expect(groupButtons[0]).not.toHaveAttribute('data-highlighted', 'true');
  });

  it.each([
    ['AGENT_NOT_FOUND', '找不到这个 Agent'],
    ['UNSUPPORTED_TARGET', '不能将其他所有者的对象加入群聊'],
    ['INVALID_ARGUMENT', '请求参数无效'],
    ['CALLER_NOT_AUTHORIZED', '当前身份无权执行此群聊操作'],
    ['MOCK_EXAMPLE_MISSING', '当前预览示例未覆盖这组参数'],
  ])('shows in-place Notice when creation is rejected with %s', async (code, expectedCopy) => {
    const createGroup: GroupAdminApi['createGroup'] = async () => {
      throw new DomainError(code, 'rejected', {}, {
        method: 'conversations.create',
        requestId: 'req-refuse',
        status: 400,
        dispatch: 'dispatched',
        catalogDigest: 'd',
        attempts: 1,
        startedAt: 1,
        durationMs: 1,
      });
    };

    render(<GuiCapabilitiesProvider value={makePageCapabilities({ createGroup })}><ConnectedChatPage /></GuiCapabilitiesProvider>);
    fireEvent.click(await screen.findByRole('button', { name: '新建群聊' }));
    const dialog = screen.getByRole('dialog', { name: '新建群聊' });
    fireEvent.change(within(dialog).getByLabelText(/群聊名称/), { target: { value: 'release' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '创建' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(expectedCopy);
  });

  it('rejects titles exceeding 200 characters locally without invoking onSubmit', () => {
    const onSubmit = vi.fn();
    const state: GroupCreateMutationState = { status: 'idle' };
    render(
      <GroupCreateDialog
        open
        candidates={[]}
        state={state}
        onSubmit={onSubmit}
        onRetry={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/群聊名称/), { target: { value: 'x'.repeat(201) } });
    fireEvent.click(screen.getByRole('button', { name: '创建' }));
    expect(screen.getByText('群聊名称最多 200 个字符。')).toBeVisible();
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
