import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ConversationStep } from '../src/features/onboarding/impl/ConversationStep';
import { FirstRunOnboarding } from '../src/features/onboarding';
import type { FirstRunAttemptView, FirstRunReplyActions, ReplyAttemptResult, ReplyMessageView } from '../src/features/onboarding';

const ATTEMPT: FirstRunAttemptView = {
  attemptId: 'att-0123456789abcdef',
  revision: 2,
  state: 'pending',
  operationKey: 'opkey-0123456789abcdef',
  sendKey: 'frs-0123456789abcdef0123456789abcdef',
  receiptRevision: 0,
  loginEpoch: 3,
  agentUri: 'agent:alice@node:first-run',
  agentCreatedAtMs: 1760000000000,
  provider: 'deepseek',
  model: 'deepseek-flash',
  harness: 'pi',
  configRevision: null,
  credentialKind: 'key',
  requestMessageId: null,
  conversationId: null,
  replyMessageId: null,
};

const BOUND: FirstRunAttemptView = {
  ...ATTEMPT,
  revision: 3,
  state: 'bound',
  requestMessageId: 'm.request-0123456789',
  conversationId: 'c.0123456789abcdef',
};

const CONFIRMED: FirstRunAttemptView = {
  ...BOUND,
  revision: 4,
  state: 'confirmed',
  receiptRevision: 0,
  replyMessageId: 'm.reply-0123456789',
};

const REPLY: FirstRunReplyActions = {
  begin: vi.fn(async (): Promise<ReplyAttemptResult> => ({ status: 'begun', attempt: ATTEMPT })),
  send: vi.fn(async (): Promise<ReplyAttemptResult> => ({ status: 'bound', attempt: BOUND })),
  history: vi.fn(async (): Promise<readonly ReplyMessageView[]> => [
    { id: 'm.request', author: 'user', text: '你好，这是确认消息' },
    { id: 'm.reply', author: 'agent', text: '这是 Agent 的真实回复' },
  ]),
  confirm: vi.fn(async (): Promise<ReplyAttemptResult> => ({
    status: 'confirmed',
    attempt: CONFIRMED,
    firstRun: {
      state: 'recorded', revision: 1, confirmed: ['configuration', 'reply'],
      updatedAt: '2026-11-18T00:00:00Z',
    },
  })),
  cancel: vi.fn(async (): Promise<ReplyAttemptResult> => ({
    status: 'cancelled', attempt: { ...BOUND, state: 'cancelled' },
  })),
};

function replyView(attempt: FirstRunAttemptView | null, overrides: Record<string, unknown> = {}) {
  return {
    attempt,
    agentUri: ATTEMPT.agentUri,
    busy: false,
    notice: null,
    onStart: vi.fn(),
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
}

describe('the P3 reply step', () => {
  it('offers only an explicit send before any attempt exists', () => {
    const reply = replyView(null);
    render(<ConversationStep state="active" chatStatus="unavailable" reply={reply} />);
    fireEvent.click(screen.getByRole('button', { name: '发送确认消息' }));
    expect(reply.onStart).toHaveBeenCalledTimes(1);
    expect(reply.onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /确认完成/ })).not.toBeInTheDocument();
  });

  it('shows the exact conversation reply in the confirmation panel', () => {
    const reply = replyView(BOUND, { messages: [
      { id: 'm.request', author: 'user', text: '确认消息' },
      { id: 'm.reply', author: 'agent', text: '这是 Agent 的真实回复' },
    ] });
    render(<ConversationStep state="active" chatStatus="unavailable" reply={reply} />);
    expect(screen.getByText('这是 Agent 的真实回复')).toBeInTheDocument();
    expect(screen.getByRole('log', { name: '确认真实回复的消息' })).toBeInTheDocument();
  });

  it('asks the user to confirm after a bound request and never auto-completes', () => {
    const reply = replyView(BOUND);
    render(<ConversationStep state="active" chatStatus="unavailable" reply={reply} />);
    expect(screen.getByRole('button', { name: '我已收到真实回复，确认完成' })).toBeInTheDocument();
    expect(reply.onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '我已收到真实回复，确认完成' }));
    expect(reply.onConfirm).toHaveBeenCalledTimes(1);
  });

  it('does not let a pending attempt look complete', () => {
    render(<ConversationStep state="active" chatStatus="unavailable" reply={replyView(ATTEMPT)} />);
    expect(screen.queryByText('已确认')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '检查发送结果' })).toBeInTheDocument();
  });

  it('shows the confirmed state only from the server attempt', () => {
    render(<ConversationStep state="active" chatStatus="unavailable"
      reply={replyView(CONFIRMED)} onComplete={() => undefined} />);
    expect(screen.getByText('已确认')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '进入开始页' })).toBeInTheDocument();
  });

  it('refuses to send when no Agent was selected', () => {
    render(<ConversationStep state="active" chatStatus="unavailable"
      reply={replyView(null, { agentUri: null })} />);
    expect(screen.getByText(/请先完成运行配置/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发送确认消息' })).not.toBeInTheDocument();
  });
});

describe('resuming the reply step from onboarding.status', () => {
  const base = {
    getAccountState: async () => ({
      account: { status: 'authenticated' as const, owner: 'alice' },
      device: { status: 'ready' as const },
      firstRun: {
        state: 'recorded' as const,
        revision: 1,
        confirmed: ['configuration', 'reply'] as const,
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  };

  it('restores a confirmed attempt as complete and switches once', async () => {
    const onComplete = vi.fn();
    render(<FirstRunOnboarding onComplete={onComplete} actions={{
      ...base,
      replyConfirmation: REPLY,
      getAccountState: async () => ({
        ...(await base.getAccountState()),
        firstRunAttempt: CONFIRMED,
      }),
    }} />);
    expect(await screen.findByText('已收到并确认这条确认消息的真实回复，首次设置完成。')).toBeInTheDocument();
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(document.querySelector('[data-step-nav="conversation"]'))
      .toHaveAttribute('data-status', 'completed');
  });

  it('resumes a bound attempt without completing it', async () => {
    const onComplete = vi.fn();
    render(<FirstRunOnboarding onComplete={onComplete} actions={{
      ...base,
      replyConfirmation: REPLY,
      getAccountState: async () => ({
        ...(await base.getAccountState()),
        firstRunAttempt: BOUND,
      }),
    }} />);
    expect(await screen.findByRole('button', { name: '我已收到真实回复，确认完成' })).toBeInTheDocument();
    expect(await screen.findByText('这是 Agent 的真实回复')).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('loads a late reply when the user refreshes the message log', async () => {
    let entries: readonly ReplyMessageView[] = [
      { id: 'm.request', author: 'user', text: '确认消息' },
    ];
    const history = vi.fn(async () => entries);
    const onComplete = vi.fn();
    render(<FirstRunOnboarding onComplete={onComplete} actions={{
      ...base,
      replyConfirmation: { ...REPLY, history },
      getAccountState: async () => ({
        ...(await base.getAccountState()),
        firstRunAttempt: BOUND,
      }),
    }} />);
    expect(await screen.findByText('确认消息')).toBeInTheDocument();
    expect(screen.queryByText('迟到回复')).not.toBeInTheDocument();
    entries = [...entries, { id: 'm.reply', author: 'agent', text: '迟到回复' }];
    fireEvent.click(screen.getByRole('button', { name: '刷新回复' }));
    expect(await screen.findByText('迟到回复')).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('opens the chat on this attempt’s Agent, not a stale conversation target', async () => {
    const onOpenReplyChat = vi.fn();
    render(<FirstRunOnboarding onOpenReplyChat={onOpenReplyChat}
      conversation={{
        status: 'ready',
        targetId: 'agent:stale@node:first-run',
        targetName: 'Stale',
        draft: '',
        messages: [],
        history: { status: 'ready' },
        sendAccepted: false,
        capability: 'ready',
        onDraftChange: vi.fn(),
        onSend: vi.fn(),
        onRetry: vi.fn(),
        onCheckResult: vi.fn(),
      }}
      actions={{
        ...base,
        replyConfirmation: REPLY,
        getAccountState: async () => ({
          ...(await base.getAccountState()),
          firstRunAttempt: BOUND,
        }),
      }} />);
    fireEvent.click(await screen.findByRole('button', { name: '打开聊天查看回复' }));
    expect(onOpenReplyChat).toHaveBeenCalledWith(BOUND.agentUri);
    expect(onOpenReplyChat).not.toHaveBeenCalledWith('agent:stale@node:first-run');
  });

  it('never completes from a legacy send acceptance or chat status while P3 owns the loop', async () => {
    const onComplete = vi.fn();
    render(<FirstRunOnboarding chatStatus="completed" onComplete={onComplete}
      conversation={{
        status: 'ready',
        targetId: ATTEMPT.agentUri,
        targetName: 'First Run',
        draft: '',
        messages: [{ id: 'assistant-1', targetId: ATTEMPT.agentUri, author: 'assistant', text: '你好' }],
        history: { status: 'ready' },
        sendAccepted: true,
        capability: 'ready',
        onDraftChange: vi.fn(),
        onSend: vi.fn(),
        onRetry: vi.fn(),
        onCheckResult: vi.fn(),
      }}
      actions={{
        ...base,
        replyConfirmation: REPLY,
        getAccountState: async () => ({
          ...(await base.getAccountState()),
          firstRunAttempt: BOUND,
        }),
      }} />);
    expect(await screen.findByRole('button', { name: '我已收到真实回复，确认完成' })).toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    expect(onComplete).not.toHaveBeenCalled();
    expect(document.querySelector('[data-step-nav="conversation"]'))
      .toHaveAttribute('data-status', 'active');
  });

  it('revokes a completed reply when a later status has no current attempt', async () => {
    const onComplete = vi.fn();
    const state = base.getAccountState;
    const setup = {
      keySaved: true,
      pendingAction: null,
      agentUri: ATTEMPT.agentUri,
    } as const;
    const confirmed = {
      ...base,
      replyConfirmation: REPLY,
      getAccountState: async () => ({ ...(await state()), firstRunAttempt: CONFIRMED }),
      getSetupState: async () => setup,
    };
    const revoked = {
      ...base,
      replyConfirmation: REPLY,
      getAccountState: async () => ({ ...(await state()) }),
      getSetupState: async () => setup,
    };
    const view = render(<FirstRunOnboarding onComplete={onComplete} actions={confirmed} />);
    expect(await screen.findByText('已收到并确认这条确认消息的真实回复，首次设置完成。')).toBeInTheDocument();
    expect(document.querySelector('[data-step-nav="conversation"]'))
      .toHaveAttribute('data-status', 'completed');

    view.rerender(<FirstRunOnboarding onComplete={onComplete} actions={revoked} />);
    await waitFor(() => expect(document.querySelector('[data-step-nav="conversation"]'))
      .toHaveAttribute('data-status', 'active'));
    expect(screen.queryByText('已收到并确认这条确认消息的真实回复，首次设置完成。')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送确认消息' })).toBeInTheDocument();
  });
});
