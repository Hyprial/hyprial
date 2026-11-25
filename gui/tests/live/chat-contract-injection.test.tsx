import { render, screen, fireEvent } from '@testing-library/react';
import { App } from '../../src/App';
import { useChatContractRuntime } from '../../src/dev/mock/use-contract-runtime';
import type { AgentChatScenario } from '../../src/dev/mock/agent-chat';
import { DEMO_BASELINE, AUTHENTICATED_TEST_CAPABILITIES } from '../support/demo-baseline';

/**
 * The composition root owns the chat contract runtime: it builds one with its
 * own scenario data and passes it as an App input, or injects nothing so the
 * product renders no fixture chat. This harness mirrors that seam.
 */
function FixtureChatApp({ scenario, withRuntime = true }: {
  readonly scenario: AgentChatScenario;
  readonly withRuntime?: boolean;
}) {
  const chatContract = useChatContractRuntime(withRuntime ? scenario : undefined);
  return <App capabilities={AUTHENTICATED_TEST_CAPABILITIES} firstRunMode="complete" chatContract={chatContract} baseline={DEMO_BASELINE} />;
}

it('renders the composition-injected chat contract runtime', async () => {
  const view = render(<FixtureChatApp scenario="success" />);
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  expect(await screen.findByRole('heading', { name: 'Fixture Squire' })).toBeInTheDocument();
  expect(screen.getByText('隔离契约 · Agent chat host · 不连接真实服务')).toBeInTheDocument();
  view.unmount();
  render(<FixtureChatApp scenario="success" withRuntime={false} />);
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  expect(screen.queryByText('Fixture Squire')).not.toBeInTheDocument();
  expect(screen.queryByText('隔离契约 · Agent chat host · 不连接真实服务')).not.toBeInTheDocument();
});

it('renders no fixture chat when the composition injects no runtime', () => {
  render(<FixtureChatApp scenario="success" withRuntime={false} />);
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  expect(screen.queryByText('隔离契约 · Agent chat host · 不连接真实服务')).not.toBeInTheDocument();
});

it('isolates each contract scene and performs no network or Lark calls', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch');
  const view = render(<FixtureChatApp scenario="success" />);
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  await screen.findByRole('heading', { name: 'Fixture Squire' });
  view.rerender(<FixtureChatApp scenario="empty" />);
  expect(await screen.findByText('暂无聊天对象')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Fixture Squire' })).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
  fetcher.mockRestore();
});
