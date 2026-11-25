import { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatComposer, type ChatComposerProps } from '../../src/features/agent-chat';

function props(overrides: Partial<ChatComposerProps> = {}): ChatComposerProps {
  return {
    targetId: 'agent-a', targetName: '研究者', draft: '  原样消息\n第二行  ',
    capability: 'ready', onDraftChange: vi.fn(), onSend: vi.fn(), onRetry: vi.fn(),
    ...overrides,
  };
}
function operation(status: NonNullable<ChatComposerProps['operation']>['status'], canRetry = false) {
  return { targetId: 'agent-a', requestId: 'opaque-request-7', status, canRetry };
}
function input() { return screen.getByRole('textbox', { name: '给 研究者 的消息' }); }
function submit() { fireEvent.submit(screen.getByRole('form', { name: '发送消息' })); }

describe('controlled ChatComposer', () => {
  it.each(['unavailable', 'unconfirmed'] as const)('%s allows draft edits but blocks click, Enter and direct submit', async (capability) => {
    const p = props({ capability });
    render(<ChatComposer {...p} />);
    expect(input()).toBeEnabled();
    fireEvent.change(input(), { target: { value: '  离线草稿\n' } });
    expect(p.onDraftChange).toHaveBeenCalledExactlyOnceWith('agent-a', '  离线草稿\n');
    expect(input()).toHaveValue(p.draft);
    await userEvent.click(screen.getByRole('button', { name: '发送' }));
    fireEvent.keyDown(input(), { key: 'Enter' });
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
    expect(p.onRetry).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent(capability === 'unavailable' ? '尚未连接' : '尚未确认');
  });

  it.each(['', ' \n\t '])('blocks blank draft %j even on direct submit', (draft) => {
    const p = props({ draft });
    render(<ChatComposer {...p} />);
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    fireEvent.keyDown(input(), { key: 'Enter' });
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it('handles null targets without any business intent or draft write', () => {
    const p = props({ targetId: null });
    render(<ChatComposer {...p} />);
    expect(input()).toBeDisabled();
    fireEvent.change(input(), { target: { value: 'ignored' } });
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
    expect(p.onDraftChange).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('选择');
  });

  it('keeps Chinese composition Enter from submitting, including direct form submit', () => {
    const p = props();
    render(<ChatComposer {...p} />);
    fireEvent.compositionStart(input());
    fireEvent.keyDown(input(), { key: 'Enter' });
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input());
    fireEvent.keyDown(input(), { key: 'Enter' });
    expect(p.onSend).toHaveBeenCalledExactlyOnceWith('agent-a', p.draft);
  });

  it.each([{ isComposing: true }, { keyCode: 229 }])('ignores native IME Enter %j', (nativeProps) => {
    const p = props();
    render(<ChatComposer {...p} />);
    fireEvent.keyDown(input(), { key: 'Enter', ...nativeProps });
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it('leaves Shift+Enter to textarea newline editing', async () => {
    const p = props({ draft: '第一行' });
    function ControlledDraft() {
      const [draft, setDraft] = useState(p.draft);
      return <ChatComposer {...p} draft={draft} onDraftChange={(id, value) => {
        p.onDraftChange(id, value); setDraft(value);
      }} />;
    }
    render(<ControlledDraft />);
    await userEvent.click(input());
    await userEvent.keyboard('{End}{Shift>}{Enter}{/Shift}');
    expect(input()).toHaveValue('第一行\n');
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it.each(['enter', 'button', 'submit'])('%s sends the original text without clearing the draft', async (method) => {
    const p = props();
    render(<ChatComposer {...p} />);
    if (method === 'enter') fireEvent.keyDown(input(), { key: 'Enter' });
    if (method === 'button') await userEvent.click(screen.getByRole('button', { name: '发送' }));
    if (method === 'submit') submit();
    expect(p.onSend).toHaveBeenCalledExactlyOnceWith('agent-a', p.draft);
    expect(p.onDraftChange).not.toHaveBeenCalled();
    expect(input()).toHaveValue(p.draft);
  });

  it('uses synchronous controller pending to stop rapid repeat intents', async () => {
    const p = props();
    function Controller() {
      const [pending, setPending] = useState(false);
      return <ChatComposer {...p} operation={pending ? operation('in-flight') : undefined} onSend={(id, text) => {
        p.onSend(id, text); setPending(true);
      }} />;
    }
    render(<Controller />);
    fireEvent.keyDown(input(), { key: 'Enter' });
    fireEvent.keyDown(input(), { key: 'Enter' });
    submit();
    await userEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(p.onSend).toHaveBeenCalledTimes(1);
    expect(input()).toHaveValue(p.draft);
    expect(screen.getByRole('status')).toHaveTextContent('发送中');
  });

  it('does not permanently lock a no-op controller callback', async () => {
    const p = props();
    render(<ChatComposer {...p} />);
    fireEvent.keyDown(input(), { key: 'Enter' });
    await act(async () => {});
    submit();
    expect(p.onSend).toHaveBeenCalledTimes(2);
  });

  it.each(['in-flight', 'unknown'] as const)('%s blocks send and retry even if canRetry is true', async (status) => {
    const p = props({ operation: operation(status, true) });
    render(<ChatComposer {...p} />);
    fireEvent.keyDown(input(), { key: 'Enter' });
    submit();
    await userEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    expect(p.onSend).not.toHaveBeenCalled();
    expect(p.onRetry).not.toHaveBeenCalled();
    expect(input()).toHaveValue(p.draft);
    expect(screen.getByRole('status')).toHaveTextContent(status === 'unknown' ? '结果未知' : '发送中');
  });

  it.each([false, true])('definitive failure preserves text; canRetry=%s gates retry of original id', async (canRetry) => {
    const p = props({ operation: { ...operation('definitive-failure', canRetry), error: '明确拒绝' } });
    render(<ChatComposer {...p} />);
    expect(screen.getByRole('alert')).toHaveTextContent('明确拒绝');
    expect(input()).toHaveValue(p.draft);
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
    if (canRetry) {
      await userEvent.click(screen.getByRole('button', { name: '重试' }));
      expect(p.onRetry).toHaveBeenCalledExactlyOnceWith('opaque-request-7');
    } else {
      expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
      expect(p.onRetry).not.toHaveBeenCalled();
    }
    expect(p.onDraftChange).not.toHaveBeenCalled();
  });

  it.each(['unavailable', 'unconfirmed'] as const)('does not retry a failure when capability is %s', async (capability) => {
    const p = props({ capability, operation: operation('definitive-failure', true) });
    render(<ChatComposer {...p} />);
    const retry = screen.queryByRole('button', { name: '重试' });
    if (retry) await userEvent.click(retry);
    submit();
    expect(p.onRetry).not.toHaveBeenCalled();
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it.each(['in-flight', 'unknown', 'definitive-failure', 'success'] as const)('ignores another target’s %s operation entirely', async (status) => {
    const p = props({ operation: { ...operation(status, true), targetId: 'agent-b', error: '旧错误' } });
    render(<ChatComposer {...p} />);
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    expect(screen.getByRole('status')).not.toHaveTextContent(/旧错误|结果未知|发送中|已发送/);
    await userEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(p.onSend).toHaveBeenCalledExactlyOnceWith('agent-a', p.draft);
    expect(p.onRetry).not.toHaveBeenCalled();
  });

  it('projects authoritative success without clearing and accepts later controlled revisions', () => {
    const p = props({ operation: operation('success') });
    const { rerender } = render(<ChatComposer {...p} />);
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
    expect(input()).toHaveValue(p.draft);
    rerender(<ChatComposer {...p} draft="控制器更新" />);
    expect(input()).toHaveValue('控制器更新');
    expect(p.onDraftChange).not.toHaveBeenCalled();
  });
});

describe.each(['unavailable', 'unconfirmed'] as const)('ChatComposer capability changes to %s', (capability) => {
  it.each([
    ['in-flight', '发送中'],
    ['unknown', '发送结果未知'],
    ['definitive-failure', '发送失败：明确拒绝'],
    ['success', ''],
  ] as const)('retains the matching %s outcome and draft without accepting new writes', async (status, label) => {
    const p = props({ operation: { ...operation(status, true), error: '明确拒绝' } });
    const { rerender } = render(<ChatComposer {...p} />);
    const initialNotice = screen.getByRole(status === 'definitive-failure' ? 'alert' : 'status');
    if (label) expect(initialNotice).toHaveTextContent(label);
    else expect(initialNotice).toBeEmptyDOMElement();

    rerender(<ChatComposer {...p} capability={capability} />);
    const notice = screen.getByRole(status === 'definitive-failure' ? 'alert' : 'status');
    if (label) expect(notice).toHaveTextContent(label);
    expect(notice).toHaveTextContent(capability === 'unavailable' ? '尚未连接' : '发送能力尚未确认');
    if (status !== 'definitive-failure') expect(notice).not.toHaveTextContent('发送失败');
    expect(notice).not.toHaveTextContent('消息已发送');
    expect(input()).toHaveValue(p.draft);
    expect(input()).toBeEnabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '发送' }));
    fireEvent.keyDown(input(), { key: 'Enter' });
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
    expect(p.onRetry).not.toHaveBeenCalled();
    expect(p.onDraftChange).not.toHaveBeenCalled();
  });

  it('keeps another target’s outcome out of the current capability notice', () => {
    const p = props({ capability, operation: { ...operation('unknown'), targetId: 'agent-b' } });
    render(<ChatComposer {...p} />);
    expect(screen.getByRole('status')).not.toHaveTextContent(/结果未知|发送失败|消息已发送/);
    expect(screen.getByRole('status')).toHaveTextContent(capability === 'unavailable' ? '尚未连接' : '发送能力尚未确认');
    submit();
    expect(p.onSend).not.toHaveBeenCalled();
    expect(p.onRetry).not.toHaveBeenCalled();
  });
});
