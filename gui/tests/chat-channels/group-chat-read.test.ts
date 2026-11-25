/**
 * Decision record (group chat read controller tests, slice GC5-1, 2026-11-21):
 * - Pins the one-action-one-request budget, polling rate cap (<= 1 + 5 in 10s),
 *   visibility pause, mutual exclusion with 1:1 polling, byte-for-byte
 *   preservation of `conversations.list` without a group port, `groupsUnknown`
 *   on `recordAvailable: false`, authoritative id merge, 3-tier member name
 *   resolution, and group error/refusal handling.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HttpError, MethodRefusedError } from '../../src/client';
import {
  ConnectedChatController,
  GroupChatController,
  resolveGroupMemberName,
  useConnectedChat,
  useGroupChat,
  type GroupChatReadApi,
  type GroupHead,
  type HistoryPage,
} from '../../src/features/chat-channels';
import { createWebGuiCapabilities } from '../../src/app/capabilities';

const OWNER = 'user:alice';
const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const REVIEWER_ID = 'a.revieweraaaaaaaaaaaaaaaaaa';
const TARGET_URI = 'agent:alice:node:worker';

const groupHead: GroupHead = {
  conversationId: GROUP_ID,
  title: 'release',
  memberCount: 3,
  lastMessageAtMs: 3000,
};

function makeEntry(id: string, overrides: Partial<HistoryPage['entries'][number]> = {}): HistoryPage['entries'][number] {
  return {
    kind: 'message',
    messageId: id,
    conversationId: GROUP_ID,
    from: OWNER,
    to: GROUP_ID,
    intent: 'request',
    createdAtMs: 1000,
    message: id,
    origin: {},
    messageClass: 'interaction',
    ...overrides,
  };
}

describe('GroupChatController read budget and state', () => {
  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  it('issues exactly one port call per public action and zero on deselect', async () => {
    const historyCalls: Array<[string, string | undefined, string | undefined]> = [];
    let directoryRefreshes = 0;
    const read: GroupChatReadApi = {
      history: async (groupId, cursor, mode) => {
        historyCalls.push([groupId, cursor, mode]);
        if (mode === 'older') {
          return { entries: [makeEntry('m-0', { createdAtMs: 500 })], nextCursor: null };
        }
        if (mode === 'after') {
          return { entries: [makeEntry('m-2', { createdAtMs: 2000 })], nextCursor: null, headCursor: 'h-2' };
        }
        return {
          entries: [makeEntry('m-1', { createdAtMs: 1000 })],
          nextCursor: 'older-1',
          nextOlderCursor: 'older-1',
          headCursor: 'h-1',
        };
      },
    };
    const controller = new GroupChatController({
      read,
      refreshDirectory: async () => { directoryRefreshes += 1; },
      identity: OWNER,
    });
    controller.setContext([groupHead], [{ agentId: WORKER_ID, displayName: 'worker' }]);

    await controller.select(GROUP_ID);
    expect(historyCalls).toHaveLength(1);
    expect(historyCalls[0]).toEqual([GROUP_ID, undefined, 'tail']);

    await controller.refresh();
    expect(historyCalls).toHaveLength(2);
    expect(historyCalls[1]).toEqual([GROUP_ID, 'h-1', 'after']);

    await controller.loadMore(GROUP_ID);
    expect(historyCalls).toHaveLength(3);
    expect(historyCalls[2]).toEqual([GROUP_ID, 'older-1', 'older']);

    await controller.refreshList();
    expect(directoryRefreshes).toBe(1);
    expect(historyCalls).toHaveLength(3);

    controller.deselect();
    expect(controller.getSnapshot().activeGroupId).toBeNull();
    expect(historyCalls).toHaveLength(3);
    expect(directoryRefreshes).toBe(1);
  });

  it('caps group polling at 1 + 5 calls in 10s, silences 1:1 polling while a group is open, and pauses when hidden', async () => {
    vi.useFakeTimers();
    const groupHistoryCalls: string[] = [];
    const directHistoryCalls: string[] = [];
    const capabilities = {
      ...createWebGuiCapabilities(),
      chatTargets: async () => [{ targetKind: 'agent', targetUri: TARGET_URI, deliverable: true, actor: TARGET_URI }],
      messageRead: {
        query: async () => [],
        conversations: async () => ({
          entries: [{ documentId: 'c.1', conversationId: 'conv-1', peer: TARGET_URI, lastMessageAtMs: 1000 }],
          groups: [groupHead],
          nextCursor: null,
        }),
        history: async (docId: string) => {
          directHistoryCalls.push(docId);
          return { entries: [], nextCursor: null };
        },
        lookup: async () => ({ status: 'unknown' as const, reason: 'not-found' as const }),
      },
      groupChatRead: {
        history: async (groupId: string) => {
          groupHistoryCalls.push(groupId);
          return { entries: [makeEntry('m-1')], nextCursor: null, headCursor: 'h-1' };
        },
      },
      chatSession: { actor: OWNER, generation: 'g-1' },
    };

    const { result } = renderHook(() => {
      const direct = useConnectedChat(capabilities, {
        chatTargets: capabilities.chatTargets,
        messageRead: capabilities.messageRead,
      });
      const group = useGroupChat(
        { read: capabilities.groupChatRead, identity: OWNER },
        [groupHead],
        [{ agentId: WORKER_ID, displayName: 'worker' }],
      );
      return { direct, group };
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => {
      result.current.direct.select(TARGET_URI);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(directHistoryCalls).toEqual(['c.1']);

    await act(async () => {
      result.current.direct.deselect();
      await result.current.group!.select(GROUP_ID);
    });
    expect(groupHistoryCalls).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(groupHistoryCalls.length).toBeLessThanOrEqual(6);
    expect(groupHistoryCalls.length).toBeGreaterThanOrEqual(2);
    expect(directHistoryCalls).toEqual(['c.1']);

    const countBeforeHidden = groupHistoryCalls.length;
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });
    expect(groupHistoryCalls).toHaveLength(countBeforeHidden);
  });

  it('merges incremental entries by message id without duplication', async () => {
    let phase = 0;
    const controller = new GroupChatController({
      read: {
        history: async () => {
          phase += 1;
          if (phase === 1) {
            return { entries: [makeEntry('m-1', { message: 'first' })], nextCursor: null, headCursor: 'h-1' };
          }
          return {
            entries: [
              makeEntry('m-1', { message: 'first' }),
              makeEntry('m-2', { from: WORKER_ID, message: 'reply', createdAtMs: 2000 }),
            ],
            nextCursor: null,
            headCursor: 'h-2',
          };
        },
      },
      identity: OWNER,
    });
    controller.setContext([groupHead], [{ agentId: WORKER_ID, displayName: 'worker' }]);

    await controller.select(GROUP_ID);
    await controller.refresh();
    const messages = controller.getSnapshot().entries[GROUP_ID].messages;
    expect(messages.map(m => [m.id, m.author, m.authorName, m.text])).toEqual([
      ['m-1', 'user', undefined, 'first'],
      ['m-2', 'assistant', 'worker', 'reply'],
    ]);
  });

  it('resolves member names in the 3-tier order: identity -> members -> targets -> raw id', () => {
    const members = [{ memberId: WORKER_ID, displayName: 'member-worker' }];
    const targets = [
      { agentId: WORKER_ID, displayName: 'target-worker' },
      { agentId: REVIEWER_ID, displayName: 'target-reviewer' },
    ];
    expect(resolveGroupMemberName(OWNER, { identity: OWNER, members, targets })).toBe('你');
    expect(resolveGroupMemberName(WORKER_ID, {
      identity: OWNER,
      entryNames: { [WORKER_ID]: 'entry-worker' },
      members,
      targets,
    })).toBe('entry-worker');
    expect(resolveGroupMemberName(WORKER_ID, { identity: OWNER, members, targets })).toBe('member-worker');
    expect(resolveGroupMemberName(REVIEWER_ID, { identity: OWNER, members, targets })).toBe('target-reviewer');
    expect(resolveGroupMemberName('a.deletedagentaaaaaaaaaaaa', { identity: OWNER, members, targets }))
      .toBe('a.deletedagentaaaaaaaaaaaa');
  });

  it('maps membership events, CONVERSATION_NOT_MEMBER, CONVERSATION_NOT_FOUND, DAEMON_UNAVAILABLE and MethodRefusedError', async () => {
    const reports: unknown[] = [];
    let mode: 'ok' | 'not-member' | 'not-found' | 'unavailable' | 'refused' = 'ok';
    const controller = new GroupChatController({
      read: {
        history: async () => {
          if (mode === 'not-member') {
            throw new HttpError('CONVERSATION_NOT_MEMBER', 'not member', 403, {
              method: 'conversations.history', requestId: 'r-1', status: 403, dispatch: 'dispatched',
              catalogDigest: 'd', attempts: 1, startedAt: 1, durationMs: 1,
            });
          }
          if (mode === 'not-found') {
            throw new HttpError('CONVERSATION_NOT_FOUND', 'not found', 404, {
              method: 'conversations.history', requestId: 'r-2', status: 404, dispatch: 'dispatched',
              catalogDigest: 'd', attempts: 1, startedAt: 1, durationMs: 1,
            });
          }
          if (mode === 'unavailable') {
            throw new HttpError('DAEMON_UNAVAILABLE', 'unavailable', 503, {
              method: 'conversations.history', requestId: 'r-3', status: 503, dispatch: 'dispatched',
              catalogDigest: 'd', attempts: 1, startedAt: 1, durationMs: 1,
            });
          }
          if (mode === 'refused') throw new MethodRefusedError('conversations.history', 'METHOD_NOT_FOUND');
          return {
            entries: [makeEntry('m-sys', {
              messageClass: 'system',
              message: 'alice created the group "release"',
              system: { event: 'created', memberId: OWNER },
            })],
            nextCursor: null,
          };
        },
      },
      reportUnavailable: (subject, detail) => reports.push([subject, detail]),
      identity: OWNER,
    });
    controller.setContext([groupHead], []);

    await controller.select(GROUP_ID);
    expect(controller.getSnapshot().entries[GROUP_ID].messages[0]).toMatchObject({
      author: 'system',
      membershipEvent: true,
      text: '你创建了群聊「release」',
    });

    mode = 'unavailable';
    await controller.select(GROUP_ID);
    expect(controller.getSnapshot().entries[GROUP_ID]).toMatchObject({
      historyStatus: 'error',
      stale: true,
    });
    expect(controller.getSnapshot().entries[GROUP_ID].messages).toHaveLength(1);

    mode = 'refused';
    await controller.select(GROUP_ID);
    expect(reports).toEqual([['读取消息', { code: 'METHOD_NOT_FOUND', source: 'conversations.history' }]]);
    expect(controller.getSnapshot().entries[GROUP_ID].messages).toHaveLength(1);

    mode = 'not-member';
    await controller.select(GROUP_ID);
    expect(controller.getSnapshot().entries[GROUP_ID]).toMatchObject({
      historyStatus: 'error',
      membershipStatus: 'not-member',
      historyError: '你已不在这个群里。',
    });

    mode = 'not-found';
    await controller.select(GROUP_ID);
    expect(controller.getSnapshot().entries[GROUP_ID]).toMatchObject({
      historyStatus: 'error',
      membershipStatus: 'not-found',
      historyError: '这个群已不存在。',
    });
  });

  it('omits kinds on conversations.list when group port is absent and marks groupsUnknown on recordAvailable: false', async () => {
    const directCalls: Array<[string | undefined, readonly string[] | undefined]> = [];
    const withoutGroups = new ConnectedChatController({
      targets: async () => [],
      query: async () => [],
      conversations: async (cursor, kinds) => {
        directCalls.push([cursor, kinds]);
        return { entries: [], nextCursor: null };
      },
    });
    await withoutGroups.start();
    expect(directCalls).toEqual([[undefined, undefined]]);

    const groupCalls: Array<[string | undefined, readonly string[] | undefined]> = [];
    const withGroups = new ConnectedChatController({
      targets: async () => [],
      query: async () => [],
      includeGroups: true,
      conversations: async (cursor, kinds) => {
        groupCalls.push([cursor, kinds]);
        return { entries: [], groups: [], nextCursor: null, recordAvailable: false };
      },
    });
    await withGroups.start();
    expect(groupCalls).toEqual([[undefined, ['direct', 'group']]]);
    expect(withGroups.getSnapshot().groupsUnknown).toBe(true);
    expect(withGroups.getSnapshot().groups).toEqual([]);
  });

  it('resolves names from history memberNames including departed members while preserving 你 priority', async () => {
    const FORMER_ID = 'a.formeraaaaaaaaaaaaaaaaaaaa';
    const controller = new GroupChatController({
      read: {
        history: async () => ({
          entries: [
            makeEntry('m-1', {
              from: FORMER_ID,
              createdAtMs: 1000,
              message: 'last note before leaving',
              replyTo: { messageId: 'm-0', from: OWNER, excerpt: 'hi' },
              mentions: [FORMER_ID],
            }),
            makeEntry('m-2', {
              from: FORMER_ID,
              createdAtMs: 2000,
              message: 'former left',
              messageClass: 'system',
              system: { event: 'member-left', memberId: FORMER_ID },
            }),
          ],
          nextCursor: null,
          memberNames: {
            [OWNER]: 'Alice From Server',
            [FORMER_ID]: 'departed-bot',
          },
        }),
      },
      identity: OWNER,
    });
    controller.setContext([groupHead], []);

    await controller.select(GROUP_ID);
    const messages = controller.getSnapshot().entries[GROUP_ID].messages;
    expect(messages[0]).toMatchObject({
      author: 'assistant',
      authorName: 'departed-bot',
      replyTo: { messageId: 'm-0', authorName: '你', excerpt: 'hi' },
      mentions: ['departed-bot'],
    });
    expect(messages[1]).toMatchObject({
      author: 'system',
      membershipEvent: true,
      text: 'departed-bot 退出了群聊',
    });
  });
});
