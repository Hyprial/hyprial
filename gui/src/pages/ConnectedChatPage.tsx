/**
 * Decision record (ordinary connected chat page, chat layout increment 2026-11-05)
- The ordinary page renders the same AgentChatSection as the fixture and legacy
  live views: chat-object list from `targets`, conversation history from
  the conversation document (`conversations.history`, slice G1 2026-11-18;
  previously `message.history`), the shared composer, drafts, focus and
  empty/loading/failure states. The previous default (a send picker next to a conversation directory)
  is gone from the product page; it duplicated the layout and could present a
  target without a confirmed conversation as a chat.
- All reads, selection and sending live in ConnectedChatController, which is fed
  by the capabilities injected by common composition. This page never builds a
  transport, token, endpoint or second client, and it never derives a principal
  from a display name or from message rows.
- The explicit `chatSend` / `chatSendDirectory` props remain a frozen-contract
  injection seam for tests and compositions that already freeze one target and
  conversation. They are not used by the ordinary App and do not select a second
  presentation path for the product.
- Sending needs a directory target, trusted identity and send adapter; the first
  message can start a conversation. Target directory failures show
  the wire code and request id; an unknown send keeps the recovery key and its
  manual lookup button, and never resends.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useGuiCapabilities, type GuiCapabilities } from '../app/capabilities';
import { useNotifyUnavailable } from '../components/notifications';
import { Button, Icon, Select } from '../components/primitives';
import { AgentChatSection } from '../features/agent-chat';
import { useAgentDirectory } from '../features/agents';
import type { SessionTarget } from '../state/gui-view-model';
import { useChatTargetInfo } from './chat-target-info';
import {
  CONNECTED_CHAT_RECOVERY_PENDING_NOTICE,
  ConnectedChatSendPicker,
  EMPTY_GROUP_ADMIN_SNAPSHOT,
  EMPTY_GROUP_CHAT_SNAPSHOT,
  EMPTY_GROUP_SEND_SNAPSHOT,
  GROUP_POST_UNKNOWN_NOTICE,
  GroupCreateDialog,
  GroupMembersPanel,
  GroupMentionPicker,
  buildGroupMemberCandidates,
  type ChatSendApi,
  type ConnectedChatPort,
  type GroupAdminPort,
  type GroupChatPort,
  type GroupSendPort,
  type MessageReadApi,
  type SendDirectoryTarget,
  SingleAgentChatSend,
  type SingleAgentChatSendTarget,
  mergeGroupDisplayedMessages,
  useConnectedChat,
  useGroupAdmin,
  useGroupChat,
  useGroupSend,
} from '../features/chat-channels';

export interface ConnectedChatSendBinding {
  readonly api: ChatSendApi;
  readonly identity: string;
  readonly target: SingleAgentChatSendTarget;
  readonly conversationId: string;
  readonly onConversationRecovered?: (conversationId: string) => void;
}

/** Supplied only after common composition has a trusted identity and target directory. */
export interface ConnectedChatSendDirectoryBinding {
  readonly api: ChatSendApi;
  readonly identity: string;
  readonly generation: string;
  readonly targets: readonly SendDirectoryTarget[];
}

export type ConnectedChatReader = Pick<MessageReadApi, 'query' | 'history'>;

export function ConnectedChatPage({
  chatSend,
  chatSendDirectory,
  initialTargetId,
  openedTargetIds,
  onIntentConsumed,
}: {
  readonly chatSend?: ConnectedChatSendBinding;
  readonly chatSendDirectory?: ConnectedChatSendDirectoryBinding;
  readonly initialTargetId?: string | null;
  readonly openedTargetIds?: readonly string[];
  readonly onIntentConsumed?: () => void;
}) {
  const capabilities = useGuiCapabilities();
  if (chatSend) {
    return <section className="page page-chat" data-page="chat">
      <h1 className="sr-only">聊天</h1>
      <SingleAgentChatSend send={chatSend.api} identity={chatSend.identity} target={chatSend.target}
        conversationId={chatSend.conversationId}
        onConversationRecovered={chatSend.onConversationRecovered} />
    </section>;
  }
  if (chatSendDirectory && capabilities.messageRead) {
    return <section className="page page-chat" data-page="chat">
      <h1 className="sr-only">聊天</h1>
      <ConnectedChatSendPicker send={chatSendDirectory.api} read={capabilities.messageRead}
        identity={chatSendDirectory.identity} generation={chatSendDirectory.generation}
        targets={chatSendDirectory.targets} />
    </section>;
  }
  const { chatTargets, messageRead } = capabilities;
  if (!chatTargets || !messageRead) return null;
  return (
    <ConnectedChatWorkspace
      ports={{ chatTargets, messageRead }}
      initialTargetId={initialTargetId}
      openedTargetIds={openedTargetIds}
      onIntentConsumed={onIntentConsumed}
    />
  );
}

function ConnectedChatWorkspace({
  ports,
  initialTargetId,
  openedTargetIds = [],
  onIntentConsumed,
}: {
  readonly ports: { readonly chatTargets: NonNullable<GuiCapabilities['chatTargets']>; readonly messageRead: NonNullable<GuiCapabilities['messageRead']> };
  readonly initialTargetId?: string | null;
  readonly openedTargetIds?: readonly string[];
  readonly onIntentConsumed?: () => void;
}) {
  const capabilities = useGuiCapabilities();
  const notifyUnavailable = useNotifyUnavailable();
  const reporter = useRef(notifyUnavailable);
  reporter.current = notifyUnavailable;
  const reportUnavailable = useCallback<NonNullable<ConnectedChatPort['reportUnavailable']>>(
    (subject, detail) => reporter.current(subject, detail), []);
  const controller = useConnectedChat(capabilities, ports, reportUnavailable);
  const agentDirectory = useAgentDirectory(capabilities.agents);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const refreshDirectory = useCallback(() => controller.refreshDirectory(), [controller]);
  const groupPort = useMemo<GroupChatPort | undefined>(() => (
    capabilities.groupChatRead ? {
      read: capabilities.groupChatRead,
      refreshDirectory,
      reportUnavailable,
      identity: capabilities.chatSession?.actor,
    } : undefined
  ), [capabilities.groupChatRead, refreshDirectory, reportUnavailable, capabilities.chatSession?.actor]);
  const groupControllerRef = useRef<ReturnType<typeof useGroupChat>>(null);
  const groupSendControllerRef = useRef<ReturnType<typeof useGroupSend>>(null);
  const onGroupMembersUpdated = useCallback(
    (groupId: string, members: readonly { readonly memberId: string; readonly kind: 'user' | 'agent'; readonly displayName: string }[]) => {
      groupControllerRef.current?.setMembers(groupId, members);
      groupSendControllerRef.current?.pruneMentions(
        groupId,
        new Set(members.filter(m => m.kind === 'agent' && m.memberId !== capabilities.chatSession?.actor).map(m => m.memberId)),
      );
    },
    [capabilities.chatSession?.actor],
  );
  const groupAdminPort = useMemo<GroupAdminPort | undefined>(() => (
    capabilities.groupAdmin ? {
      api: capabilities.groupAdmin,
      reportUnavailable,
      onGroupMembersUpdated,
    } : undefined
  ), [capabilities.groupAdmin, reportUnavailable, onGroupMembersUpdated]);
  const groupAdmin = useGroupAdmin(groupAdminPort);
  const subscribeGroupAdmin = useCallback(
    (listener: () => void) => groupAdmin ? groupAdmin.subscribe(listener) : () => {},
    [groupAdmin],
  );
  const getGroupAdminSnapshot = useCallback(
    () => groupAdmin ? groupAdmin.getSnapshot() : EMPTY_GROUP_ADMIN_SNAPSHOT,
    [groupAdmin],
  );
  const groupAdminState = useSyncExternalStore(subscribeGroupAdmin, getGroupAdminSnapshot);
  const [createGroupOpen, setCreateGroupOpen] = useState(false);
  const mergedGroupHeads = useMemo(() => {
    const seen = new Set<string>();
    const merged = [];
    for (const group of groupAdminState.createdGroups) {
      if (seen.has(group.conversationId)) continue;
      seen.add(group.conversationId);
      const count = groupAdminState.memberCountOverrides[group.conversationId] ?? group.memberCount;
      merged.push({ ...group, memberCount: count });
    }
    for (const group of state.groups) {
      if (seen.has(group.conversationId)) continue;
      seen.add(group.conversationId);
      const count = groupAdminState.memberCountOverrides[group.conversationId] ?? group.memberCount;
      merged.push({ ...group, memberCount: count });
    }
    return merged;
  }, [groupAdminState.createdGroups, groupAdminState.memberCountOverrides, state.groups]);
  const groupController = useGroupChat(groupPort, mergedGroupHeads, state.targets);
  groupControllerRef.current = groupController;
  const subscribeGroup = useCallback(
    (listener: () => void) => groupController ? groupController.subscribe(listener) : () => {},
    [groupController],
  );
  const getGroupSnapshot = useCallback(
    () => groupController ? groupController.getSnapshot() : EMPTY_GROUP_CHAT_SNAPSHOT,
    [groupController],
  );
  const groupState = useSyncExternalStore(subscribeGroup, getGroupSnapshot);
  const groupSendPort = useMemo<GroupSendPort | undefined>(() => (
    capabilities.groupPost ? {
      post: capabilities.groupPost,
      reportUnavailable,
      identity: capabilities.chatSession?.actor,
      resolveMemberName: (groupId, memberId) => groupControllerRef.current?.resolveMemberName(groupId, memberId) ?? memberId,
    } : undefined
  ), [capabilities.groupPost, reportUnavailable, capabilities.chatSession?.actor]);
  const groupSendController = useGroupSend(groupSendPort);
  groupSendControllerRef.current = groupSendController;
  const subscribeGroupSend = useCallback(
    (listener: () => void) => groupSendController ? groupSendController.subscribe(listener) : () => {},
    [groupSendController],
  );
  const getGroupSendSnapshot = useCallback(
    () => groupSendController ? groupSendController.getSnapshot() : EMPTY_GROUP_SEND_SNAPSHOT,
    [groupSendController],
  );
  const groupSendState = useSyncExternalStore(subscribeGroupSend, getGroupSendSnapshot);
  const [infoExpanded, setInfoExpanded] = useState(false);
  const handledIntent = useRef<string | null>(null);

  useEffect(() => {
    if (!initialTargetId || state.status !== 'ready') return;
    if (handledIntent.current === initialTargetId) return;
    if (!state.targets.some((target) => target.id === initialTargetId)) return;
    handledIntent.current = initialTargetId;
    groupController?.deselect();
    controller.select(initialTargetId);
    onIntentConsumed?.();
  }, [controller, groupController, initialTargetId, onIntentConsumed, state.status, state.targets]);

  const groupTargets: readonly SessionTarget[] = mergedGroupHeads.map((group) => ({
    id: group.conversationId,
    kind: 'group',
    displayName: group.title,
    role: `${group.memberCount} 位成员`,
    availability: 'available',
    avatarTone: 'grey',
    capabilities: [],
  }));
  const activeGroup = groupState.activeGroupId
    ? groupTargets.find((group) => group.id === groupState.activeGroupId)
    : undefined;
  const activeGroupEntry = activeGroup ? groupState.entries[activeGroup.id] : undefined;
  const isGroup = activeGroup !== undefined;
  useEffect(() => {
    if (!groupSendController || !activeGroup || activeGroupEntry?.historyStatus !== 'ready') return;
    groupSendController.reconcileMessages(activeGroup.id, activeGroupEntry.messages);
  }, [groupSendController, activeGroup, activeGroupEntry?.historyStatus, activeGroupEntry?.messages]);

  const targetId = state.activeTargetId;
  const visibleTargetIds = new Set([...openedTargetIds, ...state.conversationTargetIds,
    ...Object.keys(state.entries), ...(targetId ? [targetId] : [])]);
  const visibleTargets = state.targets.filter(target => visibleTargetIds.has(target.id));
  const entry = targetId ? state.entries[targetId] : undefined;
  const active = state.targets.find(target => target.id === targetId);
  const targetInfo = useChatTargetInfo(active, agentDirectory.status === 'ready' ? agentDirectory.rows : []);
  const sessions = state.sessions.filter(session => session.peer === targetId);
  const confirmed = typeof entry?.conversationId === 'string' && entry.conversationId.length > 0;
  const sendable = capabilities.chatSend !== undefined
    && typeof capabilities.chatSession?.actor === 'string'
    && capabilities.chatSession.actor.length > 0;
  const capability = active === undefined ? 'unavailable'
    : confirmed && sendable ? 'ready'
      : !confirmed && sendable ? (state.conversationsUnknown ? 'ready' : 'new')
        : confirmed ? 'unavailable' : 'unconfirmed';
  const history: {
    targetId: string;
    status: 'loading' | 'ready' | 'error' | 'unconfirmed' | 'new' | 'unread';
    error?: string;
    stale?: boolean;
  } = !active ? { targetId: '', status: 'loading' }
    : !confirmed
      ? { targetId: active.id, status: state.conversationsUnknown ? 'unread' : sendable ? 'new' : 'unconfirmed' }
      : { targetId: active.id, status: entry?.historyStatus ?? 'loading', error: entry?.historyError };
  const notice = (entry?.notice ?? state.notice) || undefined;
  const sectionNotice = active ? notice : undefined;
  const pageNotice = active || isGroup ? undefined : notice;

  const localAgents = agentDirectory.status === 'ready' ? agentDirectory.rows : [];
  const groupMemberCandidates = useMemo(
    () => buildGroupMemberCandidates(state.targets, localAgents),
    [state.targets, localAgents],
  );
  const groupActions = groupAdmin
    ? <Button variant="ghost" size="sm" onClick={() => setCreateGroupOpen(true)}>新建群聊</Button>
    : undefined;
  const infoPanelContent = isGroup && groupAdmin
    ? <GroupMembersPanel
        groupId={activeGroup.id}
        entry={groupAdminState.groups[activeGroup.id]}
        candidates={groupMemberCandidates}
        addState={groupAdminState.addMember}
        removeState={groupAdminState.removeMember}
        onLoadMembers={() => groupAdmin.loadMembers(activeGroup.id)}
        onAddMember={(memberId) => groupAdmin.addMember(activeGroup.id, memberId)}
        onRetryAddMember={() => groupAdmin.retryAddMember()}
        onDismissAddState={() => groupAdmin.dismissAddMemberState()}
        onRemoveMember={(memberId) => groupAdmin.removeMember(activeGroup.id, memberId)}
        onRetryRemoveMember={() => groupAdmin.retryRemoveMember()}
        onDismissRemoveState={() => groupAdmin.dismissRemoveMemberState()}
      />
    : undefined;
  const recoveryNotice = entry?.operation?.error ?? state.notice ?? CONNECTED_CHAT_RECOVERY_PENDING_NOTICE;
  const groupUnavailable = activeGroupEntry?.membershipStatus === 'not-member'
    || activeGroupEntry?.membershipStatus === 'not-found';
  const groupSendable = capabilities.groupPost !== undefined
    && typeof capabilities.chatSession?.actor === 'string'
    && capabilities.chatSession.actor.length > 0;
  const activeGroupOperation = activeGroup ? groupSendState.operations[activeGroup.id] : undefined;
  const activeGroupReply = activeGroup ? groupSendState.replyTargets[activeGroup.id] : undefined;
  const activeGroupMentions = activeGroup ? (groupSendState.mentions[activeGroup.id] ?? []) : [];
  const activeGroupSent = activeGroup ? groupSendState.sentMessages[activeGroup.id] : undefined;
  const conversationActions = isGroup
    ? <>
      {groupAdmin && !groupUnavailable && (
        <Button variant="ghost" size="sm" leadingIcon={<Icon name="users" size="sm" />}
          onClick={() => {
            setInfoExpanded(true);
            void groupAdmin.loadMembers(activeGroup.id);
          }}>成员</Button>
      )}
      <Button variant="ghost" size="sm" leadingIcon={<Icon name="arrow-clockwise" size="sm" />}
        onClick={() => {
          if (groupUnavailable) void groupController?.refreshList();
          else void groupController?.select(activeGroup.id);
        }}>{groupUnavailable ? '刷新列表' : '刷新消息'}</Button>
    </>
    : active && confirmed
      ? <Button variant="ghost" size="sm" leadingIcon={<Icon name="arrow-clockwise" size="sm" />}
          onClick={() => controller.select(active.id)}>刷新消息</Button>
      : undefined;
  const conversationToolbar = isGroup
    ? (activeGroupEntry?.nextCursor
      ? <Button variant="ghost" size="sm" loading={activeGroupEntry.loadingMore}
          disabled={activeGroupEntry.loadingMore} onClick={() => { void groupController?.loadMore(activeGroup.id); }}>加载更多消息</Button>
      : undefined)
    : active && confirmed && (sessions.length > 1 || entry?.nextCursor) ? <>
      {sessions.length > 1 && <>
        <label htmlFor="chat-conversation">会话</label>
        <Select id="chat-conversation" value={entry.conversationId ?? ''}
          onValueChange={id => controller.selectConversation(active.id, id)}
          options={sessions.map((session, index) => ({ value: session.conversationId,
            label: `${index + 1} · ${new Date(session.lastMessageAtMs).toLocaleString()}` }))} />
      </>}
      {entry?.nextCursor && <Button variant="ghost" size="sm" loading={entry.loadingMore}
        disabled={entry.loadingMore} onClick={() => { void controller.loadMore(active.id); }}>加载更多消息</Button>}
    </> : undefined;

  const displayedActiveId = isGroup ? activeGroup.id : targetId;
  const displayedMessages = isGroup
    ? mergeGroupDisplayedMessages(activeGroupEntry?.messages ?? [], activeGroupSent)
    : (entry?.messages ?? []);
  const displayedHistory = isGroup
    ? {
      targetId: activeGroup.id,
      status: activeGroupEntry?.historyStatus ?? 'loading',
      error: activeGroupEntry?.historyError,
      stale: activeGroupEntry?.stale,
    }
    : history;

  function handleSelect(id: string): void {
    if (mergedGroupHeads.some(group => group.conversationId === id)) {
      controller.deselect();
      groupAdmin?.clearHighlight(id);
      void groupController?.select(id);
      return;
    }
    groupController?.deselect();
    controller.select(id);
  }

  function focusAfterRecovery(): void {
    requestAnimationFrame(() => {
      const root = document.querySelector<HTMLElement>('[data-page="chat"]');
      root?.querySelector<HTMLElement>('.chat-composer__input, .chat-workspace__target, .chat-workspace__info-toggle')?.focus();
    });
  }

  return <section className="page page-chat page-chat--stacked" data-page="chat">
    <h1 className="sr-only">聊天</h1>
    {!active && !isGroup && pageNotice && !state.recoveryPending &&
      <p role="status" className="chat-page-status">{pageNotice}</p>}
    {state.recoveryPending && <div className="live-status-row chat-page-status" role="status">
      <span>{recoveryNotice}</span>
      <Button onClick={() => { void controller.recover(); }}>查询原消息结果</Button>
      <Button variant="ghost" onClick={() => { controller.abandon(); focusAfterRecovery(); }}>放弃该操作键</Button>
    </div>}
    <AgentChatSection targets={visibleTargets} groups={groupTargets} groupsUnknown={state.groupsUnknown}
      highlightedGroupId={groupAdminState.highlightedGroupId}
      groupActions={groupActions}
      infoPanelContent={infoPanelContent}
      onRefreshGroups={groupController ? () => { void groupController.refreshList(); } : undefined}
      targetInfo={targetInfo} localAgentUris={agentDirectory.status === 'ready' ? agentDirectory.rows.map((row) => row.uri) : undefined}
      targetState={state.status} targetError={state.error}
      activeTargetId={displayedActiveId} messages={displayedMessages}
      conversationToolbar={conversationToolbar}
      conversationActions={conversationActions}
      renderAfterMessage={isGroup && groupSendable && !groupUnavailable
        ? (message) => (!message.membershipEvent && message.author !== 'system'
          ? <div className="chat-workspace__message-actions">
            <Button variant="ghost" size="compact" onClick={() => groupSendController?.setReplyTarget(activeGroup.id, message)}>回复</Button>
          </div>
          : undefined)
        : undefined}
      draft={isGroup ? (groupSendState.drafts[activeGroup.id] ?? '') : (displayedActiveId ? state.drafts[displayedActiveId] ?? '' : '')}
      capability={isGroup ? (groupSendable ? 'ready' : 'unavailable') : capability}
      composer={isGroup ? (groupSendable && !groupUnavailable) : sendable}
      composerContext={isGroup && (activeGroupReply || activeGroupMentions.length > 0)
        ? <>
          {activeGroupReply && (
            <div className="chat-composer__context-row">
              <span className="chat-composer__context-text">正在回复 {activeGroupReply.authorName}：{activeGroupReply.excerpt}</span>
              <Button variant="ghost" size="compact" aria-label="取消回复" className="chat-composer__context-clear"
                onClick={() => groupSendController?.clearReplyTarget(activeGroup.id)}>
                <Icon name="x" size="sm" />
              </Button>
            </div>
          )}
          {activeGroupMentions.length > 0 && (
            <div className="chat-composer__context-row group-mention-context" role="group" aria-label="已选 @ 成员">
              <span className="group-mention-context__label">@</span>
              <div className="group-mention-context__chips">
                {activeGroupMentions.map((memberId) => {
                  const label = groupController?.resolveMemberName(activeGroup.id, memberId) ?? memberId;
                  return (
                    <span key={memberId} className="group-mention-chip">
                      <span className="group-mention-chip__name">@{label}</span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="compact"
                        aria-label={`移除 @${label}`}
                        className="group-mention-chip__remove"
                        onClick={() => groupSendController?.removeMention(activeGroup.id, memberId)}
                      >
                        <Icon name="x" size="sm" />
                      </Button>
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </>
        : undefined}
      composerActions={isGroup && groupSendable && !groupUnavailable && groupAdmin
        ? <GroupMentionPicker
          groupId={activeGroup.id}
          identity={capabilities.chatSession?.actor}
          entry={groupAdminState.groups[activeGroup.id]}
          selectedMemberIds={activeGroupMentions}
          onEnsureMembers={() => groupAdmin.ensureMembersLoaded(activeGroup.id)}
          onToggleMember={(memberId) => groupSendController?.toggleMention(activeGroup.id, memberId)}
        />
        : undefined}
      unknownHint={isGroup ? GROUP_POST_UNKNOWN_NOTICE : undefined}
      onDismissUnknown={isGroup && activeGroupOperation?.status === 'unknown'
        ? () => groupSendController?.dismissUnknown(activeGroup.id)
        : undefined}
      operation={isGroup ? activeGroupOperation : entry?.operation}
      notice={state.recoveryPending ? undefined : sectionNotice}
      history={displayedHistory} infoExpanded={infoExpanded} onRetryTargets={() => { void controller.retry(); }}
      onInfoExpandedChange={setInfoExpanded}
      onSelectTarget={handleSelect}
      onDraftChange={(id, value) => {
        if (mergedGroupHeads.some(group => group.conversationId === id)) {
          groupSendController?.setDraft(id, value);
          return;
        }
        controller.setDraft(id, value);
      }}
      onSend={(id, text) => {
        if (mergedGroupHeads.some(group => group.conversationId === id)) {
          void groupSendController?.send(id, text);
          return;
        }
        void controller.send(id, text);
      }}
      onRetry={() => undefined} />
    {groupAdmin && (
      <GroupCreateDialog
        open={createGroupOpen}
        candidates={groupMemberCandidates}
        state={groupAdminState.create}
        onSubmit={async (title, members) => {
          const created = await groupAdmin.createGroup(title, members);
          if (created) setCreateGroupOpen(false);
        }}
        onRetry={async () => {
          const created = await groupAdmin.retryCreateGroup();
          if (created) setCreateGroupOpen(false);
        }}
        onDismissState={() => groupAdmin.dismissCreateState()}
        onClose={() => setCreateGroupOpen(false)}
      />
    )}
  </section>;
}
