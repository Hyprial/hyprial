import type { HTMLAttributes, ReactNode, Ref } from 'react';
import { EmptyState, ErrorState, LoadingState } from '../../components/primitives';
import type { ConversationMessage, SessionTarget } from '../../state/gui-view-model';

const deliveryLabels: Readonly<Record<NonNullable<ConversationMessage['delivery']>, string>> = {
  'not-sent': '尚未发送', pending: '发送中', sent: '已发送', failed: '发送失败', unknown: '发送结果未知',
};

/** Controlled projection of sessions.history → daemon message.history. */
export function AgentHistoryField({ target, messages, history, busy = false, disabled = false, renderAfterMessage, scrollRef, ...scrollHandlers }: {
  readonly target: SessionTarget;
  readonly messages: readonly ConversationMessage[];
  readonly history?: { readonly status: 'loading' | 'ready' | 'error'; readonly error?: string; readonly stale?: boolean };
  readonly busy?: boolean;
  readonly disabled?: boolean;
  readonly renderAfterMessage?: (message: ConversationMessage) => ReactNode;
  readonly scrollRef?: Ref<HTMLDivElement>;
} & Pick<HTMLAttributes<HTMLDivElement>, 'onScroll' | 'onWheel' | 'onTouchMove' | 'onPointerDown' | 'onKeyDown'>) {
  const thread = messages.filter(message => message.targetId === target.id);
  return <>
    <div className="chat-workspace__history-state">
      {!history ? <LoadingState title="正在等待当前对象的历史消息。" /> : <>
        {history.status === 'loading' && <LoadingState title="正在加载历史消息…" />}
        {history.status === 'error' && <ErrorState title="历史消息加载失败。" message={history.error ?? '请稍后重新查看。'} />}
        {(history.stale || (history.status !== 'ready' && thread.length > 0)) && <p className="chat-workspace__stale" role="status">当前显示上次取得的消息。</p>}
      </>}
    </div>
    <div ref={scrollRef} className="chat-workspace__messages" role="log" aria-label={`与 ${target.displayName} 的消息`}
      aria-live="polite" aria-relevant="additions" aria-busy={!history || history.status === 'loading' || busy}
      aria-disabled={disabled} tabIndex={disabled ? -1 : 0} {...scrollHandlers}>
      {thread.length === 0 && history?.status === 'ready' && <EmptyState title="从第一条消息开始。" />}
      {thread.map(message => <article key={message.id} data-message-id={message.id} className="chat-workspace__message" data-author={message.author} data-delivery={message.delivery}>
        <span className="chat-workspace__author">{message.author === 'user' ? '你' : message.author === 'system' ? '系统' : target.displayName}</span>
        <p>{message.text}</p>
        {message.createdAt && <span className="chat-workspace__message-meta">{message.createdAt}</span>}
        {message.delivery && <span className="chat-workspace__message-meta">{deliveryLabels[message.delivery]}</span>}
        {renderAfterMessage?.(message)}
      </article>)}
    </div>
  </>;
}
