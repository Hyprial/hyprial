import { useMemo, type ReactNode } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ChatController } from '../src/features/agent-chat';
import { createAgentChatMock, type AgentChatScenario } from '../src/dev/mock/agent-chat';
import { FirstRunOnboarding, useFirstRunConversation } from '../src/features/onboarding';
import { createDemoActions } from '../src/dev/demo-actions';
import { ConversationStep } from '../src/features/onboarding/impl/ConversationStep';
import type { FirstRunConversationRuntime } from '../src/features/onboarding/impl/types';
import { preparePreview } from './support/first-run-journey';

const FIXTURE_LABEL = 'FIXTURE · 隔离契约模拟 · 不连接真实服务';

const DEMO_ACTIONS = createDemoActions();

function Harness({ scenario, onComplete, children, chat: chatOverride }: {
  scenario: AgentChatScenario;
  onComplete?: () => void;
  children?: (conversation: ReturnType<typeof useFirstRunConversation>) => ReactNode;
  chat?: ChatController;
}) {
  const chat = useMemo(
    () => chatOverride ?? new ChatController(createAgentChatMock(scenario)),
    [scenario, chatOverride],
  );
  const conversation = useFirstRunConversation(chat, FIXTURE_LABEL);
  return <>
    {children?.(conversation)}
    <FirstRunOnboarding
      scenario="isolated"
      actions={DEMO_ACTIONS}
      conversation={conversation ?? undefined}
      onComplete={onComplete}
    />
  </>;
}

async function openConversation(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await preparePreview(user);
  await screen.findByRole('heading', { name: '首次对话' });
}

async function confirmedForeignSend(chat: ChatController, text: string): Promise<void> {
  await chat.start();
  const targetId = chat.getSnapshot().activeTargetId;
  expect(targetId).not.toBeNull();
  await chat.select(targetId!);
  await chat.send(targetId!, text);
  expect(chat.getSnapshot().entries[targetId!]?.operation)
    .toMatchObject({ status: 'success', confirmationPending: false });
}

const TARGET_URI = 'agent:owner:node:a.assistant';

function conversationNamed(targetName: string): FirstRunConversationRuntime {
  return {
    status: 'ready',
    targetId: TARGET_URI,
    targetName,
    draft: '',
    messages: [{ id: 'assistant-1', targetId: TARGET_URI, author: 'assistant', text: '你好' }],
    history: { status: 'ready' },
    sendAccepted: false,
    capability: 'ready',
    onDraftChange: vi.fn(),
    onSend: vi.fn(),
    onRetry: vi.fn(),
    onCheckResult: vi.fn(),
  };
}

describe('first message over the shared chat contract', () => {
  it.each(['Alfred', TARGET_URI])('uses the target label throughout the conversation: %s', targetName => {
    render(<ConversationStep
      state="active"
      chatStatus="ready"
      conversation={conversationNamed(targetName)}
    />);

    expect(screen.getByRole('textbox', { name: `给 ${targetName} 的消息` })).toBeInTheDocument();
    const log = screen.getByRole('log', { name: `与 ${targetName} 的消息` });
    expect(within(log).getByText(`${targetName}：`)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('Squire');
  });

  it('uses the neutral no-target label from the shared conversation runtime', async () => {
    render(<Harness scenario="empty">
      {conversation => <output data-testid="target-name">{conversation?.targetName}</output>}
    </Harness>);

    expect(await screen.findByTestId('target-name')).toHaveTextContent('未选择对象');
  });
  it('sends the typed text, keeps the real reply in the log and completes once', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem');
    const onComplete = vi.fn();
    const user = userEvent.setup();
    try {
      render(<Harness scenario="success" onComplete={onComplete} />);
      await openConversation(user);
      expect(screen.getByText('隔离数据来源')).toBeInTheDocument();
      const composer = screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
      await user.type(composer, '你好，Squire，请安排今天的工作');
      await user.click(screen.getByRole('button', { name: '发送' }));
      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
      const log = screen.getByRole('log', { name: '与 Fixture Squire 的消息' });
      const sent = within(log).getByText('你好，Squire，请安排今天的工作').closest('[data-author]');
      expect(sent?.getAttribute('data-author')).toBe('user');
      expect(log.querySelectorAll('[data-author="assistant"]').length).toBeGreaterThan(0);
      expect(within(log).getByText(/FIXTURE · 已收到：你好，Squire/)).toBeInTheDocument();
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(storageSpy).not.toHaveBeenCalled();
    } finally { fetchSpy.mockRestore(); storageSpy.mockRestore(); }
  });

  it('keeps the draft and offers retry when the send is definitively rejected', async () => {
    const onComplete = vi.fn();
    const user = userEvent.setup();
    render(<Harness scenario="failure" onComplete={onComplete} />);
    await openConversation(user);
    const composer = screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    await user.type(composer, '这条消息应当发送失败');
    await user.click(screen.getByRole('button', { name: '发送' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('发送失败');
    expect(composer).toHaveValue('这条消息应当发送失败');
    expect(onComplete).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '开始' })).toBeNull();
    expect(screen.queryByRole('navigation', { name: '主导航' })).toBeNull();
    await user.click(screen.getByRole('button', { name: '修改后重新发送' }));
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('发送失败');
    expect(screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' })).toHaveValue('这条消息应当发送失败');
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('never completes from a confirmed send minted outside this flow instance', async () => {
    const chat = new ChatController(createAgentChatMock('success'));
    await confirmedForeignSend(chat, '早前在聊天页发出的消息');
    const onComplete = vi.fn();
    const user = userEvent.setup();
    render(<Harness scenario="success" onComplete={onComplete} chat={chat} />);
    await screen.findByRole('heading', { name: '登录' });
    expect(
      document.querySelector('[aria-current="step"]')?.getAttribute('data-step-nav'),
    ).toBe('account');
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(onComplete).not.toHaveBeenCalled();

    await openConversation(user);
    expect(onComplete).not.toHaveBeenCalled();
    const composer = screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    await user.type(composer, '由本次首次设置发出的消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
  });

  it('confirms an unknown original request through lookup without resending, but completion still requires this flow\'s own send', async () => {
    const onComplete = vi.fn();
    const user = userEvent.setup();
    render(<Harness scenario="unknown" onComplete={onComplete} />);
    await openConversation(user);
    const log = screen.getByRole('log', { name: '与 Fixture Squire 的消息' });
    expect(within(log).getByText('请查询上一条消息是否被接收。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查询发送结果' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '查询发送结果' })).toBeNull(),
    );
    expect(within(log).getAllByText('请查询上一条消息是否被接收。')).toHaveLength(1);
    expect(within(log).queryByText(/禁止另发/)).toBeNull();
    expect(onComplete).not.toHaveBeenCalled();

    const composer = screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    await user.type(composer, '本次首次设置独立发送的消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '查询发送结果' })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole('button', { name: '查询发送结果' }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(within(log).getAllByText('本次首次设置独立发送的消息')).toHaveLength(1);
  });

  it('blocks empty and IME-composing submits', async () => {
    const onComplete = vi.fn();
    const user = userEvent.setup();
    render(<Harness scenario="success" onComplete={onComplete} />);
    await openConversation(user);
    const composer = screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    const send = screen.getByRole('button', { name: '发送' });
    expect(send).toBeDisabled();
    await user.type(composer, '   ');
    expect(send).toBeDisabled();
    fireEvent.change(composer, { target: { value: '输入中的中文' } });
    fireEvent.compositionStart(composer);
    fireEvent.keyDown(composer, { key: 'Enter', keyCode: 229, isComposing: true });
    await user.click(send);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('renders no composer, notice or status card when no chat runtime is provided', () => {
    render(
      <ConversationStep state="active" chatStatus="unavailable" />,
    );
    expect(screen.getByRole('heading', { name: '首次对话' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/的消息$/)).toBeNull();
    expect(screen.queryByRole('button', { name: '发送' })).toBeNull();
    expect(screen.queryByText('首次对话', { selector: '.ui-status-card *' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/暂不|不可用|尚未接入|未连接/);
  });
});
