import type { Dispatch } from 'react';
import { AgentChatSection } from '../features/agent-chat';
import { ChatRefusalHint } from '../features/org/crosstab/ChatRefusalHint';
import { ChatChannelSwitch } from '../features/chat/ChatChannelSwitch';
import { getChatUnavailableNotice } from '../features/chat/chat-copy';
import { canSendToTarget, getActiveDraft, getActiveTarget, getActiveThread, getFailedMessage, getPendingMessage } from '../state/selectors';
import type { AppAction, AppState } from '../state/types';
import type { MessageConnectionModel } from '../features/connections/message-connection-view-model';

interface ChatPageProps {
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly message?: MessageConnectionModel;
  readonly channel?: 'in-app' | 'feishu';
  readonly onChannelChange?: (channel: 'in-app' | 'feishu') => void;
}

/** Fixture controller only. The presentation is also used by LiveChatPage. */
export function ChatPage({ state, dispatch, message, channel = 'in-app', onChannelChange }: ChatPageProps) {
  const active = getActiveTarget(state);
  const pending = getPendingMessage(state, active.id);
  const failed = getFailedMessage(state, active.id);
  const operation = pending ?? failed;
  return <section className="page page-chat" data-page="chat">
    <h1 className="sr-only">聊天</h1>
    <AgentChatSection targets={state.targets} activeTargetId={active.id}
      messages={getActiveThread(state)} draft={getActiveDraft(state)}
      capability={canSendToTarget(active) ? 'ready' : 'unavailable'}
      operation={operation ? { targetId: active.id, requestId: operation.id,
        status: pending ? 'in-flight' : 'definitive-failure', canRetry: !pending } : undefined}
      history={{ targetId: active.id, status: 'ready' }}
      notice={getChatUnavailableNotice(active) ?? undefined} targetDetail={active.modelLabel}
      channelControl={message && onChannelChange ? <ChatChannelSwitch model={message} channel={channel} onChange={onChannelChange} /> : undefined}
      renderAfterMessage={message => <ChatRefusalHint messageId={message.id} targetId={active.id} dispatch={dispatch} />}
      infoExpanded={!state.infoCollapsed}
      onInfoExpandedChange={expanded => dispatch({ type: 'set-info-collapsed', collapsed: !expanded })}
      onSelectTarget={targetId => dispatch({ type: 'select-target', targetId })}
      onDraftChange={(_id, value) => dispatch({ type: 'set-draft', value })}
      onSend={(targetId, text) => dispatch({ type: 'chat-send', targetId, text })}
      onRetry={messageId => dispatch({ type: 'chat-retry', targetId: active.id, messageId })}
      onManageTarget={agentId => dispatch({ type: 'open-agent', agentId })} />
  </section>;
}
