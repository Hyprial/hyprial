import type { FormEvent } from 'react';
import {
  CHAT_COMPOSER_READY,
  CHAT_INTERFACE_UNAVAILABLE,
  CHAT_SEND_FAILED,
  CHAT_SENDING,
} from './chat-copy';

export interface ChatComposerProps {
  /** Stable prefix for this surface: chat page or quick-chat panel. */
  readonly idPrefix: string;
  readonly label: string;
  readonly draft: string;
  readonly canSend: boolean;
  readonly pending: boolean;
  readonly failed: boolean;
  /** Id of the unavailable notice, when the Agent cannot take messages. */
  readonly noticeId?: string;
  readonly onDraftChange: (value: string) => void;
  readonly onSend: () => void;
  readonly onRetry: () => void;
}

/**
 * Composer shared by the chat page and the quick-chat panel.
 *
 * Sending is local: the form never calls the placeholder adapter, so a submit
 * produces no HTTP request and no storage write.
 */
export function ChatComposer({
  idPrefix,
  label,
  draft,
  canSend,
  pending,
  failed,
  noticeId,
  onDraftChange,
  onSend,
  onRetry,
}: ChatComposerProps) {
  const inputId = `${idPrefix}-input`;
  const noteId = `${idPrefix}-note`;
  const disabled = !canSend || pending;

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (disabled || draft.trim().length === 0) {
      return;
    }
    onSend();
  };

  return (
    <form className="composer-wrap" onSubmit={handleSubmit}>
      {failed && (
        <div className="chat-error">
          <p className="chat-error-text" role="alert">
            {CHAT_SEND_FAILED}
          </p>
          <button
            type="button"
            className="text-button chat-retry"
            onClick={onRetry}
          >
            重试
          </button>
        </div>
      )}
      <div className="composer">
        <label className="sr-only" htmlFor={inputId}>
          {label}
        </label>
        <input
          id={inputId}
          className="composer-input"
          value={draft}
          placeholder={canSend ? '输入消息…' : '当前 Agent 不可发送'}
          disabled={disabled}
          autoComplete="off"
          aria-describedby={noticeId ?? noteId}
          onChange={(event) => onDraftChange(event.target.value)}
        />
        <button
          type="submit"
          className="button button-strong send-button"
          disabled={disabled || draft.trim().length === 0}
          aria-busy={pending || undefined}
        >
          {pending ? CHAT_SENDING : '发送'}
        </button>
      </div>
      <p className="composer-note" id={noteId}>
        {canSend ? CHAT_COMPOSER_READY : CHAT_INTERFACE_UNAVAILABLE}
      </p>
    </form>
  );
}
