/**
 * Decision record (system message layout, 2026-11-16):
- A daemon `messageClass: "system"` row is local runtime information (PAC
  dispatch, watchdog, TTL), not conversation prose. It stays folded, but once
  opened it needs to read as machine detail rather than as another message: the
  label row carries 系统 and the timestamp together, the fold control owns its
  own row, and the body sits in one inset block with a capped measure.
- The timestamp moved out of the message tail for system rows only: with a long
  dispatch body the tail put the time a screen away from 系统 and made the
  opened state read as an unstructured paragraph sequence. User and assistant
  rows keep the tail timestamp next to the delivery label.
- Long identifiers (workflow graph/request ids, hashes) must wrap inside the
  block: the body keeps `overflow-wrap: anywhere` together with `pre-wrap` so
  the preserved line breaks of the dispatch text survive without horizontal
  scrolling.
- A message whose body the conversation did not record (`bodyNotRecorded`,
  slice G1, 2026-11-18) keeps its author and time and shows one muted line in
  place of the body (inside the fold for a system row), so it never reads as an
  empty bubble. The line says only that the body is not in the record; the
  daemon names a reason (`bodyOmitted`), but one line covers every reason.
 */
import type { HTMLAttributes, ReactNode, Ref } from 'react';
import { EmptyState, ErrorState, LoadingState } from '../../../components/primitives';
import type { ConversationMessage, SessionTarget } from '../../../state/gui-view-model';

const deliveryLabels: Readonly<Record<NonNullable<ConversationMessage['delivery']>, string>> = {
  'not-sent': '尚未发送', pending: '发送中', sent: '已发送', failed: '发送失败', unknown: '发送结果未知',
};

const BODY_NOT_RECORDED = '这条消息的正文没有记录在会话中。';

function stamp(createdAt: string): string {
  return new Date(createdAt).toLocaleString('zh-CN', { hour12: false });
}

/** Controlled projection of sessions.history → daemon message.history. */
export function AgentHistoryField({ target, messages, history, busy = false, disabled = false, renderAfterMessage, scrollRef, ...scrollHandlers }: {
  readonly target: SessionTarget;
  readonly messages: readonly ConversationMessage[];
  readonly history?: { readonly status: 'loading' | 'ready' | 'error' | 'unconfirmed' | 'new' | 'unread'; readonly error?: string; readonly stale?: boolean };
  readonly busy?: boolean;
  readonly disabled?: boolean;
  readonly renderAfterMessage?: (message: ConversationMessage) => ReactNode;
  readonly scrollRef?: Ref<HTMLDivElement>;
} & Pick<HTMLAttributes<HTMLDivElement>, 'onScroll' | 'onWheel' | 'onTouchMove' | 'onPointerDown' | 'onKeyDown'>) {
  const thread = messages.filter(message => message.targetId === target.id);
  const localOnly = history !== undefined && history.status !== 'ready' && thread.length > 0
    && thread.every(message => message.delivery === 'sent');
  return <>
    <div className="chat-workspace__history-state">
      {!history ? <LoadingState title="正在等待当前对象的历史消息。" /> : <>
        {history.status === 'loading' && <LoadingState title="正在加载历史消息…" />}
        {history.status === 'unconfirmed' && <p className="chat-workspace__stale" role="status">尚未确认与该对象的会话。</p>}
        {history.status === 'new' && <p className="chat-workspace__stale" role="status">还没有会话；发送第一条消息后会开始新会话。</p>}
        {history.status === 'error' && <ErrorState title="历史消息加载失败。" message={history.error ?? '请稍后重新查看。'} />}
        {history.status === 'ready' && history.error && <p role="alert">{history.error}；当前保留已读取的消息。</p>}
        {localOnly && <p className="chat-workspace__stale" role="status">历史尚未读取，这里只显示你刚发送的消息。</p>}
        {!localOnly && (history.stale || (history.status !== 'ready' && thread.length > 0)) && <p className="chat-workspace__stale" role="status">当前显示上次取得的消息。</p>}
      </>}
    </div>
    <div ref={scrollRef} className="chat-workspace__messages" role="log"
      aria-label={target.kind === 'group' ? `群聊 ${target.displayName} 的消息` : `与 ${target.displayName} 的消息`}
      aria-live="polite" aria-relevant="additions" aria-busy={!history || history.status === 'loading' || busy}
      aria-disabled={disabled} tabIndex={disabled ? -1 : 0} {...scrollHandlers}>
      {thread.length === 0 && history?.status === 'ready' && <EmptyState title="暂无消息。" />}
      {thread.map(message => <article key={message.id} data-message-id={message.id} className="chat-workspace__message"
        data-author={message.author} data-membership-event={message.membershipEvent ? 'true' : undefined} data-delivery={message.delivery}>
        {message.author === 'system' && message.membershipEvent ? (
          <div className="chat-workspace__membership-event">
            <span>{message.text}</span>
            {message.createdAt && <time className="chat-workspace__message-meta" dateTime={message.createdAt}>
              {stamp(message.createdAt)}
            </time>}
          </div>
        ) : message.author === 'system' ? <>
          <div className="chat-workspace__system-heading">
            <span className="chat-workspace__author">系统</span>
            {message.createdAt && <time className="chat-workspace__message-meta" dateTime={message.createdAt}>
              {stamp(message.createdAt)}
            </time>}
          </div>
          <details className="chat-workspace__system"><summary>任务与系统消息</summary>
            <div className="chat-workspace__system-body">{message.bodyNotRecorded
              ? <p className="chat-workspace__body-not-recorded">{BODY_NOT_RECORDED}</p>
              : <p>{message.text}</p>}</div>
          </details>
        </> : <>
          <span className="chat-workspace__author">{message.author === 'user' ? '你' : (message.authorName ?? target.displayName)}</span>
          {message.replyTo && (
            <p className="chat-workspace__reply-ref">回复 {message.replyTo.authorName}：{message.replyTo.excerpt}</p>
          )}
          {message.bodyNotRecorded
            ? <p className="chat-workspace__body-not-recorded">{BODY_NOT_RECORDED}</p>
            : <p>{message.text}</p>}
          {message.mentions && message.mentions.length > 0 && (
            <p className="chat-workspace__mentions">{message.mentions.map(name => `@${name}`).join(' ')}</p>
          )}
          {message.createdAt && <time className="chat-workspace__message-meta" dateTime={message.createdAt}>
            {stamp(message.createdAt)}
          </time>}
          {message.author === 'user' && message.delivery && <span className="chat-workspace__message-meta">{deliveryLabels[message.delivery]}</span>}
        </>}
        {renderAfterMessage?.(message)}
      </article>)}
    </div>
  </>;
}
