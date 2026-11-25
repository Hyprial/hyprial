import { explicitAgentFixtureSeed } from '../../unified-backend/agents-test-client';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ChatSeed } from '../../../src/state/types';
import { App } from '../../../src/App';
import { DEMO_BASELINE, AUTHENTICATED_TEST_CAPABILITIES } from '../../support/demo-baseline';

const REPLY = '已经整理好今天的安排：先确认模型服务，再查看 Agent 状态。';
const SEND_FAILED = '发送失败。';

const replySeed: ChatSeed = {
  availableTargetId: 'agent-squire',
  sendMode: 'reply',
  replyText: REPLY,
};

const failSeed: ChatSeed = {
  availableTargetId: 'agent-squire',
  sendMode: 'fail',
  replyText: REPLY,
};

const completedSeed: ChatSeed = {
  ...replySeed,
  threads: {
    'agent-squire': [
      {
        id: 'seed-squire-1',
        targetId: 'agent-squire',
        author: 'user',
        text: '请把今天的安排整理一下。',
        delivery: 'sent',
      },
      {
        id: 'seed-squire-2',
        targetId: 'agent-squire',
        author: 'assistant',
        text: REPLY,
        delivery: 'sent',
      },
    ],
  },
};

function renderApp(chatSeed?: ChatSeed) {
  const user = userEvent.setup();
  render(
    <App capabilities={AUTHENTICATED_TEST_CAPABILITIES} baseline={DEMO_BASELINE} agentSeed={explicitAgentFixtureSeed()} chatSeed={chatSeed} />,
  );
  return { user };
}

async function openChat(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '聊天' }));
}

function composerInput(name = '给 Squire 的消息') {
  return screen.getByRole('textbox', { name });
}

describe('J3 three entries', () => {
  it('TC-001 opens the chat page from the main navigation and keeps one surface', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    expect(document.querySelector('[data-page="chat"]')).not.toBeNull();
    expect(
      screen.getByRole('heading', { level: 2, name: 'Squire' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('聊天对象')).toBeInTheDocument();
    expect(screen.getByLabelText('对象信息')).toBeInTheDocument();
  });

  it('TC-002 TC-004 opens the most recent conversation from the start page', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);
    await user.click(
      within(screen.getByLabelText('聊天对象')).getByRole('button', {
        name: /研究者/,
      }),
    );

    await user.click(screen.getByRole('button', { name: '开始' }));
    const entry = screen.getByRole('button', { name: '打开聊天' });
    const card = entry.closest('[data-start-block="recent-chat"]');
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent('研究者');

    await user.click(entry);
    expect(
      screen.getByRole('heading', { level: 2, name: '研究者' }),
    ).toBeInTheDocument();
  });

  it('TC-A05 hides the recent-conversation entry before any chat is opened', () => {
    renderApp(replySeed);

    expect(screen.queryByRole('button', { name: '打开聊天' })).toBeNull();
    expect(document.querySelector('[data-start-block="recent-chat"]')).toBeNull();
  });

  it('TC-003 TC-037 does not render a quick-chat launcher in the product shell', () => {
    renderApp(replySeed);

    expect(screen.queryByRole('button', { name: /快捷对话/ })).toBeNull();
  });
});

describe('J3 conversation, drafts and sending', () => {
  it('TC-007 TC-009 lists Agents with a summary and marks the active one', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    const list = screen.getByLabelText('聊天对象');
    const squire = within(list).getByRole('button', { name: /Squire/ });
    const researcher = within(list).getByRole('button', { name: /研究者/ });

    expect(squire).toHaveAttribute('aria-pressed', 'true');
    expect(researcher).toHaveAttribute('aria-pressed', 'false');
    expect(
      within(list).getByRole('button', { name: /写作者/ }),
    ).toHaveTextContent('需要连接');
  });

  it('TC-008 TC-010 keeps a draft per conversation while switching', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    await user.type(composerInput(), '给 Squire 的草稿');
    await user.click(screen.getByRole('button', { name: /研究者/ }));
    await user.type(composerInput('给 研究者 的消息'), '给研究者的草稿');
    await user.click(
      within(screen.getByLabelText('聊天对象')).getByRole('button', {
        name: /Squire/,
      }),
    );

    expect(composerInput()).toHaveValue('给 Squire 的草稿');
  });

  it('TC-014 TC-015 enables sending only with content', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    const send = screen.getByRole('button', { name: '发送' });
    expect(send).toBeDisabled();

    await user.type(composerInput(), '你好');
    expect(send).toBeEnabled();
  });

  it('TC-016 TC-017 TC-018 TC-019 TC-021 sends once, shows a pending state and then the reply', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    await user.type(composerInput(), '请整理今天的安排。');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const busy = screen.getByRole('button', { name: '发送' });
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(busy).toBeDisabled();
    expect(composerInput()).toBeEnabled();

    await user.keyboard('{Enter}');

    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    await waitFor(() => {
      expect(within(log).getByText(REPLY)).toBeInTheDocument();
    });

    const messages = log.querySelectorAll('[data-message-id]');
    expect(messages).toHaveLength(2);
    expect(messages[0]).toHaveTextContent('你');
    expect(messages[0]).toHaveTextContent('请整理今天的安排。');
    expect(messages[1]).toHaveTextContent('Squire');
    expect(messages[1]).toHaveTextContent(REPLY);
    expect(composerInput()).toHaveValue('');
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
  });

  it('TC-027 TC-028 TC-030 D06 reports the failure, keeps the draft and marks the message', async () => {
    const { user } = renderApp(failSeed);
    await openChat(user);

    await user.type(composerInput(), '这条发不出去');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(SEND_FAILED);
    expect(alert.textContent).toBe(SEND_FAILED);

    expect(composerInput()).toHaveValue('这条发不出去');
    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    const failed = log.querySelector('[data-delivery="failed"]');
    expect(failed).not.toBeNull();
    expect(
      within(log).queryByText(REPLY),
    ).toBeNull();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('TC-029 D07 retries without duplicating the user message', async () => {
    const { user } = renderApp(failSeed);
    await openChat(user);

    await user.type(composerInput(), '重试我');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByRole('button', { name: '发送' })).toBeTruthy();
    await screen.findByRole('alert');

    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    const userMessages = [...log.querySelectorAll('[data-author="user"]')];
    expect(userMessages).toHaveLength(1);
    expect(userMessages[0]).toHaveTextContent('重试我');
  });

  it('TC-033 TC-035 starts a completed conversation with one question and one reply', async () => {
    const { user } = renderApp(completedSeed);
    await openChat(user);

    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    const messages = log.querySelectorAll('[data-message-id]');
    expect(messages).toHaveLength(2);
    expect(messages[0]).toHaveTextContent('请把今天的安排整理一下。');
    expect(messages[1]).toHaveTextContent(REPLY);
    expect(log.querySelector('[data-delivery="pending"]')).toBeNull();
    expect(within(log).queryByText('暂无消息。')).toBeNull();
  });

  it('TC-031 TC-032 shows the empty state for a conversation without messages', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    expect(within(log).getByText('暂无消息。')).toBeInTheDocument();
    expect(log.querySelectorAll('[data-message-id]')).toHaveLength(0);
    expect(within(log).queryByText(REPLY)).toBeNull();
  });

  it('TC-024 TC-025 TC-026 TC-052 disables the composer for an Agent that needs a connection', async () => {
    const { user } = renderApp();
    await openChat(user);

    const input = composerInput();
    const note = screen.getByRole('note');
    expect(note).toHaveTextContent('需要连接模型服务');
    expect(input).toBeEnabled();
    expect(document.getElementById(input.getAttribute('aria-describedby')!)).toHaveTextContent('尚未连接');
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(
      screen.getByText('尚未连接，可以先保存草稿。'),
    ).toBeInTheDocument();

    await user.type(input, '发不出{Enter}');
    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    expect(log.querySelectorAll('[data-message-id]')).toHaveLength(0);
  });

  it('TC-036 names re-enabling the Agent for a disabled Agent', async () => {
    const { user } = renderApp();
    await openChat(user);
    await user.click(screen.getByRole('button', { name: /运营执行/ }));

    expect(screen.getByRole('note')).toHaveTextContent('需要启用 Agent');
    expect(composerInput('给 运营执行 的消息')).toBeEnabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
  });
});

describe('J3 manage-Agent jump and return', () => {
  it('TC-045 F01 F02 opens the Agent page with the Agent selected', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);
    await user.click(screen.getByRole('button', { name: '管理 Agent' }));

    expect(
      screen.getByRole('heading', { level: 1, name: 'Agent' }),
    ).toBeInTheDocument();
    const selected = document.querySelector('[data-selected="true"]');
    expect(selected).toHaveTextContent('Squire');
    expect(screen.queryByText(/agent\.manage/)).toBeNull();
  });

  it('TC-047 returns to the same conversation with its draft', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);
    await user.type(composerInput(), '保留这段草稿');
    await user.click(screen.getByRole('button', { name: '管理 Agent' }));

    await user.click(screen.getByRole('button', { name: '聊天' }));
    expect(
      screen.getByRole('heading', { level: 2, name: 'Squire' }),
    ).toBeInTheDocument();
    expect(composerInput()).toHaveValue('保留这段草稿');
  });
});

describe('J3 accessibility contract', () => {
  it('TC-049 TC-050 TC-054 exposes the log, busy state and info toggle semantics', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    expect(log).toHaveAttribute('aria-live', 'polite');
    expect(log).toHaveAttribute('aria-relevant', 'additions');

    const toggle = screen.getByRole('button', { name: '信息' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).toBe(screen.getByLabelText('对象信息'));

    await user.type(composerInput(), '检查忙碌状态');
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(screen.getByRole('button', { name: '发送' })).toHaveAttribute(
      'aria-busy',
      'true',
    );
  });

  it('TC-051 announces the failure from an alert region', async () => {
    const { user } = renderApp(failSeed);
    await openChat(user);

    await user.type(composerInput(), '失败播报');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const alert = await screen.findByRole('alert');
    expect(alert.tagName.toLowerCase()).toBe('p');
    expect(alert).toHaveTextContent(SEND_FAILED);
  });
});

describe('J3 boundaries', () => {
  it('TC-072 TC-073 sends without HTTP calls and without storage writes', async () => {
    const { user } = renderApp(replySeed);
    await openChat(user);

    await user.type(composerInput(), '不发请求');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => {
      expect(
        within(screen.getByRole('log', { name: '与 Squire 的消息' })).getByText(
          REPLY,
        ),
      ).toBeInTheDocument();
    });

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('TC-076 keeps an unwired build unable to send and free of success claims', async () => {
    const { user } = renderApp();
    await openChat(user);

    expect(screen.getByText('尚未连接，可以先保存草稿。')).toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent('需要连接模型服务');
    const log = screen.getByRole('log', { name: '与 Squire 的消息' });
    expect(log.querySelectorAll('[data-message-id]')).toHaveLength(0);
    expect(within(log).queryByText(REPLY)).toBeNull();
  });
});
