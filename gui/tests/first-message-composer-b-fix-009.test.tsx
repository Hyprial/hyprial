// first-run 首条消息 composer 只在有聊天 runtime 时渲染，没有禁用的替身。
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatComposer } from '../src/features/agent-chat';
import { FirstMessageComposer } from '../src/features/onboarding/impl/FirstMessageComposer';
import type { FirstRunConversationRuntime } from '../src/features/onboarding/impl/types';

describe('ChatComposer null-target hint', () => {
  it('keeps the default copy for existing callers', () => {
    render(
      <ChatComposer
        targetId={null}
        targetName="研究者"
        draft=""
        capability="unavailable"
        onDraftChange={vi.fn()}
        onSend={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('请选择聊天对象。');
  });

});

describe('FirstMessageComposer', () => {
  it('keeps the real callbacks when the runtime is ready', async () => {
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    const conversation = {
      status: 'ready',
      error: undefined,
      targetId: 'squire-1',
      targetName: 'Squire',
      draft: '你好',
      capability: 'ready',
      messages: [],
      history: { status: 'ready' },
      operation: undefined,
      sendAccepted: false,
      onDraftChange,
      onSend,
      onRetry: vi.fn(),
      onCheckResult: vi.fn(),
    } as unknown as FirstRunConversationRuntime;
    render(<FirstMessageComposer conversation={conversation} />);
    const send = screen.getByRole('button', { name: '发送' });
    expect(send).toBeEnabled();
    await userEvent.click(send);
    expect(onSend).toHaveBeenCalledWith('你好');
  });
});
