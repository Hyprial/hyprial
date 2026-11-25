import { ChatComposer } from '../../agent-chat';
import type { FirstRunConversationRuntime } from './types';

interface FirstMessageComposerProps {
  readonly conversation: FirstRunConversationRuntime;
}

/**
 * step4 唯一的 composer 投影：始终复用生产 `features/agent-chat/ChatComposer`，
 * 只在存在聊天 runtime 时渲染；没有 runtime 时调用方不渲染它，也没有禁用的替身。
 */
export function FirstMessageComposer({ conversation }: FirstMessageComposerProps) {
  return (
    <ChatComposer
      targetId={conversation.targetId}
      targetName={conversation.targetName}
      draft={conversation.draft}
      capability={conversation.capability}
      operation={conversation.operation}
      successHint="消息已送达，正在进入开始页。"
      onDraftChange={(_target, value) => conversation.onDraftChange(value)}
      onSend={(_target, text) => conversation.onSend(text)}
      onRetry={() => conversation.onRetry()}
    />
  );
}
