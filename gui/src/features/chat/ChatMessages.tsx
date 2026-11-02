import type { ReactNode, Ref } from 'react';
import type { ConversationMessage } from '../../state/gui-view-model';
import { DELIVERY_LABELS } from '../../state/selectors';
import { CHAT_EMPTY } from './chat-copy';

export interface ChatMessagesProps {
  readonly messages: readonly ConversationMessage[];
  /** Agent name used as the author label of assistant messages. */
  readonly targetName: string;
  readonly ariaLabel: string;
  readonly emptyText?: string;
  readonly scrollRef?: Ref<HTMLDivElement>;
  readonly onScroll?: () => void;
  /** dev-04：气泡下方的附加内容（发送被拒的原因与修复入口）。 */
  readonly renderAfter?: (message: ConversationMessage) => ReactNode;
}

/**
 * Message log shared by the chat page and the quick-chat panel.
 *
 * The log announces additions only. Delivery state stays visible per message so
 * a failed send never looks delivered.
 */
export function ChatMessages({
  messages,
  targetName,
  ariaLabel,
  emptyText = CHAT_EMPTY,
  scrollRef,
  onScroll,
  renderAfter,
}: ChatMessagesProps) {
  return (
    <div
      ref={scrollRef}
      className="chat-messages"
      onScroll={onScroll}
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label={ariaLabel}
    >
      {messages.length === 0 ? (
        <div className="chat-empty">
          <p>{emptyText}</p>
        </div>
      ) : (
        messages.map((message) => (
          <article
            key={message.id}
            className={`message message-${message.author}`}
            data-message-id={message.id}
          >
            <span className="message-author">
              {message.author === 'user' ? '你' : targetName}
            </span>
            <p>{message.text}</p>
            {message.delivery ? (
              <span
                className="message-delivery"
                data-delivery={message.delivery}
              >
                {DELIVERY_LABELS[message.delivery]}
              </span>
            ) : null}
            {renderAfter?.(message)}
          </article>
        ))
      )}
    </div>
  );
}
