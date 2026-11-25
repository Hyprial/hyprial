import type { Dispatch } from 'react';
import { useGuiCapabilities } from '../app/capabilities';
import { usePageAvailable } from '../app/page-availability';
import { AgentChatSection } from '../features/agent-chat';
import { useAgentDirectory } from '../features/agents';
import { ChatRefusalHint } from '../features/org';
import { getChatUnavailableNotice } from '../features/chat';
import { canSendToTarget, getActiveDraft, getActiveThread, getFailedMessage, getPendingMessage } from '../state/selectors';
import type { AppAction, AppState } from '../state/types';
import { useChatTargetInfo } from './chat-target-info';

interface ChatPageProps {
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
}

/** Fixture controller only. The presentation is also used by LiveChatPage. */
export function ChatPage({ state, dispatch }: ChatPageProps) {
  const { agents } = useGuiCapabilities();
  const directory = useAgentDirectory(agents);
  const localAgentUris = directory.status === 'ready' ? directory.rows.map((row) => row.uri) : undefined;
  const agentPageAvailable = usePageAvailable('contacts');
  const targets = state.openedChatTargetIds === undefined ? state.targets
    : state.targets.filter(target => state.openedChatTargetIds!.includes(target.id)
      || (state.threads[target.id]?.length ?? 0) > 0);
  const active = targets.find(target => target.id === state.activeTargetId) ?? targets[0];
  const targetInfo = useChatTargetInfo(active, directory.status === 'ready' ? directory.rows : []);
  if (!active) {
    return <section className="page page-chat" data-page="chat">
      <h1 className="sr-only">聊天</h1>
      <AgentChatSection targets={targets} localAgentUris={localAgentUris} activeTargetId={null}
        messages={[]} draft="" capability="unavailable"
        history={{ targetId: '', status: 'ready' }}
        notice={state.notice ?? undefined}
        infoExpanded={false}
        onInfoExpandedChange={() => undefined}
        onSelectTarget={() => undefined}
        onDraftChange={() => undefined}
        onSend={() => undefined}
        onRetry={() => undefined} />
    </section>;
  }
  const pending = getPendingMessage(state, active.id);
  const failed = getFailedMessage(state, active.id);
  const operation = pending ?? failed;
  return <section className="page page-chat" data-page="chat">
    <h1 className="sr-only">聊天</h1>
    <AgentChatSection targets={targets} targetInfo={targetInfo} localAgentUris={localAgentUris} activeTargetId={active.id}
      messages={getActiveThread(state)} draft={getActiveDraft(state)}
      capability={canSendToTarget(active) ? 'ready' : 'unavailable'}
      operation={operation ? { targetId: active.id, requestId: operation.id,
        status: pending ? 'in-flight' : 'definitive-failure', canRetry: !pending } : undefined}
      history={{ targetId: active.id, status: 'ready' }}
      notice={getChatUnavailableNotice(active) ?? undefined} targetDetail={active.modelLabel}
      renderAfterMessage={message => <ChatRefusalHint messageId={message.id} targetId={active.id} dispatch={dispatch} />}
      infoExpanded={!state.infoCollapsed}
      onInfoExpandedChange={expanded => dispatch({ type: 'set-info-collapsed', collapsed: !expanded })}
      onSelectTarget={targetId => dispatch({ type: 'select-target', targetId })}
      onDraftChange={(_id, value) => dispatch({ type: 'set-draft', value })}
      onSend={(targetId, text) => dispatch({ type: 'chat-send', targetId, text })}
      onRetry={messageId => dispatch({ type: 'chat-retry', targetId: active.id, messageId })}
      onManageTarget={!agentPageAvailable ? undefined : agentId => dispatch({ type: 'open-agent', agentId })} />
  </section>;
}
