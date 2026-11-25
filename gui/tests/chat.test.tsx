import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { createAgentJourney } from '../src/dev/journey';
import { demoFixtureState, AUTHENTICATED_TEST_CAPABILITIES } from './support/demo-baseline';
import { ChatPage } from '../src/pages/ChatPage';
import { appReducer } from '../src/state/appReducer';

async function openChat() {
  const user = userEvent.setup();
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} initialState={demoFixtureState()} />);
  await user.click(screen.getByRole('button', { name: '聊天' }));
  return { user };
}

describe('chat fixture page', () => {
  it('places a newly created local Agent in the local chat group', async () => {
    const journey = createAgentJourney();
    await journey.agents.create({ name: 'agent-123', preferredHarness: 'claude', provider: null, model: null });
    const [agent] = await journey.agents.list();
    const state = demoFixtureState();
    render(<App capabilities={{ ...AUTHENTICATED_TEST_CAPABILITIES, agents: journey.agents }}
      initialState={{ ...state, targets: [{
        id: agent.uri, kind: 'agent', displayName: agent.displayName, role: 'Agent',
        availability: 'needs-connection', avatarTone: 'green', modelLabel: '未设置', capabilities: [],
      }] }} />);

    await userEvent.click(screen.getByRole('button', { name: '聊天' }));
    const local = await screen.findByRole('group', { name: '本机 Agent' });
    expect(within(local).getByRole('button', { name: /agent-123/ })).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '其他可联系' })).not.toBeInTheDocument();
  });
  it('keeps new directory entries out until a conversation is explicitly opened', () => {
    const state = { ...demoFixtureState(), openedChatTargetIds: [] };
    const { rerender } = render(<ChatPage state={state} dispatch={() => {}} />);
    expect(within(screen.getByLabelText('聊天对象')).queryAllByRole('button')).toHaveLength(0);
    const opened = appReducer(state, { type: 'open-agent-chat', targetId: state.targets[0].id });
    rerender(<ChatPage state={opened} dispatch={() => {}} />);
    expect(within(screen.getByLabelText('聊天对象')).getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: state.targets[0].displayName })).toBeInTheDocument();
  });
  it('starts empty and does not enable sending without a connection', async () => {
    await openChat();

    const input = screen.getByRole('textbox', {
      name: '给 Squire 的消息',
    });
    const log = screen.getByRole('log', {
      name: '与 Squire 的消息',
    });
    expect(within(log).getByText('暂无消息。')).toBeInTheDocument();
    expect(input).toBeEnabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(input).toHaveValue('');
  });

  it('keeps each conversation draft when switching targets', async () => {
    const { user } = await openChat();

    const squireInput = screen.getByRole('textbox', {
      name: '给 Squire 的消息',
    });
    expect(squireInput).toBeEnabled();

    await user.click(
      within(screen.getByLabelText('聊天对象')).getByRole('button', {
        name: /研究者/,
      }),
    );
    const researcherInput = screen.getByRole('textbox', {
      name: '给 研究者 的消息',
    });
    expect(researcherInput).toBeEnabled();

    await user.click(
      within(screen.getByLabelText('聊天对象')).getByRole('button', {
        name: /Squire/,
      }),
    );
    expect(
      screen.getByRole('textbox', { name: '给 Squire 的消息' }),
    ).toBeEnabled();
  });

  it('collapses and restores the information panel', async () => {
    const { user } = await openChat();

    const toggle = screen.getByRole('button', { name: '信息' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('对象信息')).toBeInTheDocument();

    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByLabelText('对象信息')).not.toBeInTheDocument();

    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('对象信息')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '收起信息' }));

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByLabelText('对象信息')).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();
  });

  it('disables the composer for a target that needs a connection', async () => {
    const { user } = await openChat();

    await user.click(
      within(screen.getByLabelText('聊天对象')).getByRole('button', {
        name: /写作者/,
      }),
    );

    expect(
      screen.getByRole('heading', { level: 2, name: '写作者' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('textbox', { name: '给 写作者 的消息' }),
    ).toBeEnabled();
    expect(screen.getByRole('note')).toHaveTextContent('需要连接模型服务');
  });
});
