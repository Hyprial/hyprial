import { explicitAgentFixtureSeed } from './unified-backend/agents-test-client';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { createLocalAgentGateway } from '../src/features/agents';
import { demoFixtureState, AUTHENTICATED_TEST_CAPABILITIES } from './support/demo-baseline';

async function openContacts(withGateway = false) {
  const user = userEvent.setup();
  const seed = explicitAgentFixtureSeed();
  render(
    <App
      capabilities={AUTHENTICATED_TEST_CAPABILITIES}
      initialState={demoFixtureState()}
      agentSeed={seed}
      agentGateway={withGateway
        ? createLocalAgentGateway({ agents: [], connections: seed.connections, organizations: [] })
        : undefined}
    />,
  );
  await user.click(screen.getByRole('button', { name: 'Agent' }));
  return { user };
}

describe('contacts fixture page', () => {
  it('renders all fixture agents and supports search', async () => {
    const { user } = await openContacts();

    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(4);

    await user.type(
      screen.getByRole('searchbox', { name: '搜索 Agent' }),
      '研究',
    );

    expect(
      screen.getByTestId('contact-agent-researcher'),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('contact-agent-writer'),
    ).not.toBeInTheDocument();
    expect(screen.getByText('找到 1 个，共 4 个')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '清除筛选' }));

    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(4);
  });

  it('filters by availability and clears from the empty state', async () => {
    const { user } = await openContacts();

    await user.click(
      screen.getByRole('button', { name: '需要连接' }),
    );

    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(3);
    expect(
      screen.getByTestId('contact-agent-writer'),
    ).toBeInTheDocument();

    await user.type(
      screen.getByRole('searchbox', { name: '搜索 Agent' }),
      '不存在的筛选词',
    );

    expect(screen.getByText('没有匹配的 Agent')).toBeInTheDocument();
    await user.click(
      screen.getAllByRole('button', { name: '清除筛选' })[0],
    );

    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(4);
  });

  it('opens the selected Agent detail from a contact card', async () => {
    const { user } = await openContacts();

    await user.click(screen.getByTestId('contact-agent-researcher'));

    expect(
      screen.getByRole('heading', { level: 2, name: '研究者' }),
    ).toBeInTheDocument();
    expect(document.querySelector('[data-selected="true"]')).toHaveTextContent(
      '研究者',
    );
  });

  it('renders no create control when the composition has a seed but no gateway', async () => {
    await openContacts();

    expect(screen.queryByRole('button', { name: '创建 Agent' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/暂不可用|尚未开放/);
  });

  it('opens the create form from the contacts page', async () => {
    const { user } = await openContacts(true);

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(
      screen.getByRole('heading', { level: 2, name: '创建 Agent' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('名称')).toBeInTheDocument();
  });
});
