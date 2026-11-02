import { useRef, useState, type FormEvent } from 'react';
import { Button, Field, Notice } from '../../components/primitives';

/** Explicit UI-only handoff. No send operation, persistence or completion event. */
export function ConversationPreviewForm({ onConfirm }: { readonly onConfirm: (text: string) => void }) {
  const [text, setText] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [failed, setFailed] = useState(false);
  const composing = useRef(false);
  const submittedRef = useRef(false);

  function confirm(event: FormEvent): void {
    event.preventDefault();
    if (composing.current || submittedRef.current || !text.trim()) return;
    submittedRef.current = true;
    setSubmitted(true);
    setFailed(false);
    try { onConfirm(text); }
    catch {
      submittedRef.current = false;
      setSubmitted(false);
      setFailed(true);
    }
  }

  return <>
    <Notice tone="info" title="界面模拟 · 不发送消息">
      确认后进入会话页面并保留这段文字；不会请求模型、生成回复或完成首次设置。
    </Notice>
    <form className="dev01-first-message-form" onSubmit={confirm}>
      <Field label="给 Squire 的第一条消息" htmlFor="dev01-first-message">
        <input id="dev01-first-message" value={text} maxLength={4000} autoComplete="off"
          placeholder="你想先做什么？" disabled={submitted}
          onChange={(event) => setText(event.target.value)}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (composing.current || event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
          }} />
      </Field>
      <div className="settings-actions">
        <Button type="submit" variant="primary" disabled={submitted || !text.trim()}>确认并进入会话</Button>
      </div>
    </form>
    {failed && <Notice tone="warning" title="未能打开会话页面">文字仍保留在本页，请重试。</Notice>}
  </>;
}
