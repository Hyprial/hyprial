import { useId, useRef } from 'react';
import { Button } from '../../components/primitives';
import type { OperationStatus } from '../../state/operation-lifecycle';

export interface ChatComposerProps {
  readonly successHint?: string;
  readonly targetId: string | null;
  readonly targetName: string;
  readonly draft: string;
  readonly capability: 'ready' | 'unavailable' | 'unconfirmed';
  readonly operation?: {
    readonly targetId: string;
    readonly requestId: string;
    readonly status: OperationStatus;
    readonly canRetry: boolean;
    readonly error?: string;
  };
  readonly onDraftChange: (targetId: string, value: string) => void;
  /** The controller must synchronously project in-flight after accepting an intent. */
  readonly onSend: (targetId: string, text: string) => void;
  /** Retry the original request; reset operation before allowing a fresh send. */
  readonly onRetry: (requestId: string) => void;
}

/** Presentation only: drafts and operation outcomes remain owned by the controller. */
export function ChatComposer({
  targetId, targetName, draft, capability, operation,
  onDraftChange, onSend, onRetry, successHint,
}: ChatComposerProps) {
  const statusId = useId();
  const composing = useRef(false);
  const currentOperation = targetId !== null && operation?.targetId === targetId
    ? operation : undefined;
  const status = currentOperation?.status;
  const pending = status === 'in-flight';
  const ready = targetId !== null && capability === 'ready';
  const canSend = ready && draft.trim().length > 0 &&
    (status === undefined || status === 'not-sent' || status === 'success');
  const canRetry = ready && status === 'definitive-failure' &&
    currentOperation?.canRetry === true;

  const capabilityHint = capability === 'unavailable'
    ? '尚未连接，可以先保存草稿。'
    : capability === 'unconfirmed' ? '发送能力尚未确认，可以先保存草稿。' : undefined;
  let operationHint: string | undefined;
  if (pending) operationHint = '发送中，请等待结果。';
  else if (status === 'unknown') operationHint = '发送结果未知，请等待确认，暂不能重新发送。';
  else if (status === 'definitive-failure') operationHint = currentOperation?.error
    ? `发送失败：${currentOperation.error}` : '发送失败。';
  else if (status === 'success') operationHint = successHint ?? '消息已发送。';

  let hint = operationHint ?? capabilityHint ?? 'Enter 发送，Shift+Enter 换行。';
  if (targetId === null) hint = '请选择聊天对象。';

  function send() {
    if (canSend && targetId !== null && !composing.current) onSend(targetId, draft);
  }

  return (
    <form className="chat-composer" aria-label="发送消息" onSubmit={(event) => {
      event.preventDefault();
      send();
    }}>
      <textarea
        className="chat-composer__input"
        aria-label={`给 ${targetName} 的消息`}
        aria-describedby={statusId}
        rows={3}
        value={draft}
        disabled={targetId === null}
        onChange={(event) => {
          if (targetId !== null) onDraftChange(targetId, event.currentTarget.value);
        }}
        onCompositionStart={() => { composing.current = true; }}
        onCompositionEnd={() => { composing.current = false; }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.shiftKey || composing.current ||
            event.nativeEvent.isComposing || event.keyCode === 229) return;
          event.preventDefault();
          send();
        }}
      />
      <div className="chat-composer__footer">
        <p className="chat-composer__status" id={statusId} role={status === 'definitive-failure' ? 'alert' : 'status'}>
          {hint}
          {operationHint && capabilityHint && <><br />{capabilityHint}</>}
        </p>
        <div className="chat-composer__actions">
          {canRetry && currentOperation && (
            <Button onClick={() => {
              if (!composing.current) onRetry(currentOperation.requestId);
            }}>重试</Button>
          )}
          <Button type="submit" variant="primary" disabled={!canSend} loading={pending}>发送</Button>
        </div>
      </div>
    </form>
  );
}
