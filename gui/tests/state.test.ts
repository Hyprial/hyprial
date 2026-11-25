import { appReducer } from '../src/state/appReducer';
import { createFixtureState } from '../src/dev/fixtures/fixture-state';
import type { ConversationMessage } from '../src/state/gui-view-model';
import type { AppState } from '../src/state/types';
import {
  mergeMessagesById,
  mergeReconnectSnapshot,
  reconcileLocalMessage,
} from '../src/state/messages';
import {
  getActiveDraft,
  getActiveThread,
  getFocusIntent,
  getSelectedMessage,
  getVisibleAgents,
} from '../src/state/selectors';

function stateWithAvailableSquire() {
  const state = createFixtureState();
  return {
    ...state,
    targets: state.targets.map((target) =>
      target.id === 'agent-squire'
        ? { ...target, availability: 'available' as const }
        : target,
    ),
  };
}

function stateWithSeedMessages(): AppState {
  return {
    ...createFixtureState(),
    threads: {
      'agent-squire': [
        {
          id: 'server-message-squire-1',
          targetId: 'agent-squire',
          author: 'assistant' as const,
          text: '已确认消息',
        },
      ],
      'agent-researcher': [
        {
          id: 'server-message-researcher-1',
          targetId: 'agent-researcher',
          author: 'assistant' as const,
          text: '已确认研究消息',
        },
      ],
    },
  };
}

describe('fixture reducer', () => {
  it('starts without successful Agent state or preloaded conversations', () => {
    const state = createFixtureState();

    expect(state.targets.some((target) => target.availability === 'available')).toBe(false);
    expect(Object.values(state.threads).flat()).toHaveLength(0);
    expect(state.connections.every((connection) => connection.status !== 'connected')).toBe(true);
  });

  it('opens the requested chat target directly, including a session-only target', () => {
    const external = 'agent:owner:node:a.external';
    const state = appReducer(createFixtureState(), {
      type: 'open-agent-chat',
      targetId: external,
    });

    expect(state.page).toBe('chat');
    expect(state.activeTargetId).toBe(external);
    expect(state.chatIntentTargetId).toBe(external);
    expect(state.lastChatTargetId).toBe(external);
    expect(state.openedChatTargetIds).toEqual([external]);
    const reopened = appReducer(state, { type: 'open-agent-chat', targetId: external });
    expect(reopened.openedChatTargetIds).toEqual([external]);
    expect(appReducer(reopened, { type: 'confirm-logout' }).openedChatTargetIds).toEqual([]);
  });

  it('queues one pending user message and never fabricates the reply on send', () => {
    const drafted = appReducer(stateWithAvailableSquire(), {
      type: 'set-draft',
      value: '本地测试消息',
    });
    const sent = appReducer(drafted, {
      type: 'chat-send',
      targetId: 'agent-squire',
      text: getActiveDraft(drafted),
    });
    const thread = getActiveThread(sent);

    expect(thread).toHaveLength(1);
    expect(thread[thread.length - 1]).toMatchObject({
      author: 'user',
      text: '本地测试消息',
      targetId: 'agent-squire',
      delivery: 'pending',
    });
    expect(sent.drafts['agent-squire']).toBe('');
    expect(sent.notice).toBeNull();
  });

  it('adds a reply only through an explicit resolved outcome', () => {
    const state = { ...stateWithAvailableSquire(), chatReplyText: '收到。' };
    const pending = appReducer(
      appReducer(state, { type: 'set-draft', value: '请整理今天的安排。' }),
      { type: 'chat-send', targetId: 'agent-squire', text: '请整理今天的安排。' },
    );
    const [localMessage] = getActiveThread(pending);

    expect(getActiveThread(pending)).toHaveLength(1);

    const resolved = appReducer(pending, {
      type: 'chat-resolve',
      targetId: 'agent-squire',
      messageId: localMessage.id,
      outcome: 'sent',
    });
    const thread = getActiveThread(resolved);

    expect(thread).toHaveLength(2);
    expect(thread[0]).toMatchObject({ delivery: 'sent', author: 'user' });
    expect(thread[1]).toMatchObject({ author: 'assistant', text: '收到。' });
  });

  it('keeps a failed send marked and restores the draft for a retry', () => {
    const pending = appReducer(
      appReducer(stateWithAvailableSquire(), {
        type: 'set-draft',
        value: '发不出去',
      }),
      { type: 'chat-send', targetId: 'agent-squire', text: '发不出去' },
    );
    const [localMessage] = getActiveThread(pending);

    const failed = appReducer(pending, {
      type: 'chat-resolve',
      targetId: 'agent-squire',
      messageId: localMessage.id,
      outcome: 'failed',
    });

    expect(getActiveThread(failed)[0].delivery).toBe('failed');
    expect(getActiveDraft(failed)).toBe('发不出去');

    const retried = appReducer(failed, {
      type: 'chat-retry',
      targetId: 'agent-squire',
      messageId: localMessage.id,
    });

    expect(getActiveThread(retried)).toHaveLength(1);
    expect(getActiveThread(retried)[0].delivery).toBe('pending');
    expect(getActiveDraft(retried)).toBe('');
  });

  it('refuses to send to an agent that needs a connection', () => {
    const selected = appReducer(createFixtureState(), {
      type: 'select-target',
      targetId: 'agent-writer',
    });
    const drafted = appReducer(selected, {
      type: 'set-draft',
      value: '不能发送',
    });
    const sent = appReducer(drafted, {
      type: 'chat-send',
      targetId: 'agent-writer',
      text: '不能发送',
    });

    expect(sent.threads['agent-writer']).toHaveLength(0);
    expect(sent.drafts['agent-writer']).toBe('不能发送');
  });

  it('filters agents by availability and search text', () => {
    const available = appReducer(createFixtureState(), {
      type: 'set-agent-filter',
      filter: 'available',
    });
    expect(getVisibleAgents(available)).toHaveLength(0);

    const needsConnection = appReducer(createFixtureState(), {
      type: 'set-agent-filter',
      filter: 'needs-connection',
    });
    const searched = appReducer(needsConnection, {
      type: 'set-agent-query',
      value: '研究',
    });
    expect(getVisibleAgents(searched).map((agent) => agent.id)).toEqual([
      'agent-researcher',
    ]);
  });

  it('resets to a cleared product state on confirmed logout without faking auth', () => {
    const dirty = appReducer(
      appReducer(createFixtureState(), {
        type: 'set-draft',
        value: '临时内容',
      }),
      { type: 'request-logout' },
    );
    const loggedOut = appReducer(dirty, { type: 'confirm-logout' });

    expect(loggedOut.page).toBe('start');
    expect(loggedOut.drafts).toEqual({});
    expect(loggedOut.logoutDialogOpen).toBe(false);
    expect(loggedOut.notice).toBe('已清空当前会话。账号登出暂不可用。');
    expect(loggedOut.firstRun.status).toBe('complete');
    expect(loggedOut.targets).toEqual([]);
    expect(loggedOut.connections).toEqual([]);
    expect(loggedOut.threads).toEqual({});
  });

  it('keeps drafts per target and preserves panel state when switching', () => {
    let state = appReducer(stateWithAvailableSquire(), {
      type: 'set-draft',
      value: '给 Squire 的草稿',
    });
    state = appReducer(state, {
      type: 'set-info-collapsed',
      collapsed: true,
    });
    state = appReducer(state, {
      type: 'select-target',
      targetId: 'agent-researcher',
    });
    state = appReducer(state, {
      type: 'set-draft',
      value: '给研究者的草稿',
    });

    expect(getActiveDraft(state)).toBe('给研究者的草稿');
    expect(state.infoCollapsed).toBe(true);

    state = appReducer(state, {
      type: 'select-target',
      targetId: 'agent-squire',
    });
    expect(getActiveDraft(state)).toBe('给 Squire 的草稿');
    expect(state.infoCollapsed).toBe(true);

    const sent = appReducer(state, {
      type: 'chat-send',
      targetId: 'agent-squire',
      text: '给 Squire 的草稿',
    });
    expect(getActiveDraft(sent)).toBe('');
    expect(sent.drafts['agent-researcher']).toBe('给研究者的草稿');
  });

  it('deduplicates replayed messages by authoritative id', () => {
    const original: ConversationMessage = {
      id: 'server-message-1',
      targetId: 'agent-squire',
      author: 'assistant',
      text: '第一次到达',
      delivery: 'pending',
    };
    const replay: ConversationMessage = {
      ...original,
      text: '更新后到达',
      delivery: 'sent',
    };
    const other: ConversationMessage = {
      id: 'server-message-2',
      targetId: 'agent-squire',
      author: 'assistant',
      text: '另一条消息',
    };

    const merged = mergeMessagesById([original, other, replay]);

    expect(merged).toHaveLength(2);
    expect(merged[0]).toMatchObject({
      id: 'server-message-1',
      text: '更新后到达',
      delivery: 'sent',
    });
    expect(merged[1].id).toBe('server-message-2');
  });
});

describe('authoritative message reconciliation', () => {
  const message = (
    id: string,
    text: string,
    delivery?: ConversationMessage['delivery'],
  ): ConversationMessage => ({
    id,
    targetId: 'agent-squire',
    author: 'assistant',
    text,
    delivery,
  });

  it('merges a reconnect snapshot with later incremental events by id', () => {
    const snapshot = [message('server-1', 'one'), message('server-2', 'two')];
    const events = [
      message('server-2', 'two updated', 'sent'),
      message('server-3', 'three'),
      message('server-2', 'two final', 'sent'),
    ];

    const merged = mergeReconnectSnapshot(snapshot, events);

    expect(merged.map((entry) => entry.id)).toEqual([
      'server-1',
      'server-2',
      'server-3',
    ]);
    expect(merged[1]).toMatchObject({
      id: 'server-2',
      text: 'two final',
      delivery: 'sent',
    });
    expect(snapshot[1].text).toBe('two');
    expect(events).toHaveLength(3);
  });

  it('replaces a local placeholder with canonical content without duplicating it', () => {
    const local = message('local-agent-squire-2', 'optimistic');
    const existing = message('server-1', 'old');
    const canonical = message('server-1', 'new');

    const merged = reconcileLocalMessage(
      [existing, local],
      local.id,
      canonical,
    );

    expect(merged.map((entry) => entry.id)).toEqual(['server-1']);
    expect(merged[0].text).toBe('new');
    expect(merged.some((entry) => entry.id === local.id)).toBe(false);
  });

  it('refuses to treat a local placeholder id as canonical', () => {
    const local = message('local-agent-squire-2', 'optimistic');

    expect(() =>
      reconcileLocalMessage([local], local.id, {
        ...local,
        text: 'pretend-server-message',
      }),
    ).toThrow(/authoritative message id/);
  });

  it('adds canonical content when the local placeholder is already gone', () => {
    const canonical = message('server-9', 'confirmed');

    const merged = reconcileLocalMessage(
      [message('server-1', 'one')],
      'local-agent-squire-9',
      canonical,
    );

    expect(merged.map((entry) => entry.id)).toEqual(['server-1', 'server-9']);
  });
});

describe('per-target selection and focus restoration intent', () => {
  it('keeps draft, selected message and focus intent across target and page changes', () => {
    let state = stateWithSeedMessages();
    state = appReducer(state, {
      type: 'set-draft',
      value: 'Squire 草稿',
    });
    state = appReducer(state, {
      type: 'select-message',
      targetId: 'agent-squire',
      messageId: 'server-message-squire-1',
    });
    state = appReducer(state, {
      type: 'set-focus-intent',
      targetId: 'agent-squire',
      intent: {
        kind: 'message',
        messageId: 'server-message-squire-1',
      },
    });
    state = appReducer(state, {
      type: 'select-target',
      targetId: 'agent-researcher',
    });
    state = appReducer(state, {
      type: 'select-message',
      targetId: 'agent-researcher',
      messageId: 'server-message-researcher-1',
    });
    state = appReducer(state, { type: 'navigate', page: 'start' });
    state = appReducer(state, { type: 'navigate', page: 'chat' });
    state = appReducer(state, {
      type: 'select-target',
      targetId: 'agent-squire',
    });

    expect(getSelectedMessage(state, 'agent-squire')?.id).toBe(
      'server-message-squire-1',
    );
    expect(getSelectedMessage(state, 'agent-researcher')?.id).toBe(
      'server-message-researcher-1',
    );
    expect(getFocusIntent(state, 'agent-squire')).toEqual({
      kind: 'message',
      messageId: 'server-message-squire-1',
    });
    expect(getActiveDraft(state)).toBe('Squire 草稿');
  });

  it('ignores missing, local and cross-target message ids instead of selecting a wrong message', () => {
    let state = createFixtureState();

    state = appReducer(state, {
      type: 'select-message',
      targetId: 'agent-squire',
      messageId: 'missing-message',
    });
    expect(getSelectedMessage(state, 'agent-squire')).toBeUndefined();

    state = appReducer(state, {
      type: 'select-message',
      targetId: 'agent-squire',
      messageId: 'server-message-researcher-1',
    });
    expect(getSelectedMessage(state, 'agent-squire')).toBeUndefined();

    state = appReducer(state, {
      type: 'select-message',
      targetId: 'agent-squire',
      messageId: 'local-agent-squire-2',
    });
    expect(getSelectedMessage(state, 'agent-squire')).toBeUndefined();

    state = appReducer(state, {
      type: 'set-focus-intent',
      targetId: 'agent-squire',
      intent: { kind: 'message', messageId: 'missing-message' },
    });
    expect(getFocusIntent(state, 'agent-squire')).toBeNull();
  });

  it('does not fall back to another message when the selected id is absent', () => {
    const state = {
      ...createFixtureState(),
      targetChat: {
        'agent-squire': {
          selectedMessageId: 'missing-message',
          focusIntent: null,
        },
      },
    };

    expect(getSelectedMessage(state, 'agent-squire')).toBeUndefined();
  });

  it('clears only the focus intent and keeps selection and draft', () => {
    let state = appReducer(stateWithSeedMessages(), {
      type: 'set-draft',
      value: '保留草稿',
    });
    state = appReducer(state, {
      type: 'select-message',
      targetId: 'agent-squire',
      messageId: 'server-message-squire-1',
    });
    state = appReducer(state, {
      type: 'set-focus-intent',
      targetId: 'agent-squire',
      intent: { kind: 'composer' },
    });
    state = appReducer(state, {
      type: 'clear-focus-intent',
      targetId: 'agent-squire',
    });

    expect(getFocusIntent(state, 'agent-squire')).toBeNull();
    expect(getSelectedMessage(state, 'agent-squire')?.id).toBe(
      'server-message-squire-1',
    );
    expect(getActiveDraft(state)).toBe('保留草稿');
  });
});


it('removes a deleted Agent from opened chats without erasing historical messages', () => {
  const state = { ...stateWithSeedMessages(), openedChatTargetIds: ['agent-squire', 'agent-researcher'],
    activeTargetId: 'agent-squire', lastChatTargetId: 'agent-squire', chatIntentTargetId: 'agent-squire' };
  const next = appReducer(state, { type: 'agent-deleted', targetId: 'agent-squire' });
  expect(next.openedChatTargetIds).toEqual(['agent-researcher']);
  expect(next.targets.some(target => target.id === 'agent-squire')).toBe(false);
  expect(next.activeTargetId).toBe('');
  expect(next.lastChatTargetId).toBeNull();
  expect(next.chatIntentTargetId).toBeNull();
  expect(next.threads).toBe(state.threads);
});
