import { createMockAgentAdapterBindings } from '../../fixtures/agents/adapter-bindings';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MethodRefusedError } from '../../../src/client';
import { expectUnavailableToast, renderWithNotifications } from '../../support/notify-harness';
import {

  type AgentAdapterBindingsApi,
  type AgentAdapterBindingRow,
} from '../../../src/features/agents/impl/api/agent-adapter-bindings';
import { PlatformBindingsTab } from '../../../src/features/agents/impl/detail/PlatformBindingsTab';

const AGENT = 'agent:owner:node:a.squire';

function row(overrides: Partial<AgentAdapterBindingRow>): AgentAdapterBindingRow {
  return {
    name: 'feishu-main',
    provider: 'lark',
    online: true,
    processRunning: true,
    boundAgent: null,
    ...overrides,
  };
}

function renderTab(api: AgentAdapterBindingsApi) {
  return render(<PlatformBindingsTab agentActor={AGENT} api={api} />);
}

describe('Agent 平台绑定页签', () => {
  it('observes an external binding without a user refresh or a mutation', async () => {
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    const list = vi.spyOn(api, 'list').mockResolvedValueOnce([row({})]).mockResolvedValue([row({ boundAgent: AGENT })]);
    const bind = vi.spyOn(api, 'bind');
    const view = renderTab(api);
    await screen.findByRole('button', { name: '绑定' });
    await waitFor(() => expect(screen.getByRole('button', { name: '解除绑定' })).toBeVisible(), { timeout: 4200 });
    expect(bind).not.toHaveBeenCalled();
    view.unmount();
    const count = list.mock.calls.length;
    await new Promise(resolve => setTimeout(resolve, 2100));
    expect(list).toHaveBeenCalledTimes(count);
  }, 10000);

  it('reads back a refused binding without retrying the mutation', async () => {
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    const list = vi.spyOn(api, 'list').mockResolvedValueOnce([row({})]).mockResolvedValue([row({ boundAgent: AGENT })]);
    const bind = vi.spyOn(api, 'bind').mockRejectedValue(new Error('绑定已变化，请重新确认。'));
    renderTab(api);
    await userEvent.setup().click(await screen.findByRole('button', { name: '绑定' }));
    expect(await screen.findByRole('button', { name: '解除绑定' })).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent('绑定已变化');
    expect(bind).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('rejects a late binding list from a replaced account source', async () => {
    let resolveOld!: (rows: readonly AgentAdapterBindingRow[]) => void;
    const oldApi = createMockAgentAdapterBindings({ adapters: [] });
    vi.spyOn(oldApi, 'list').mockReturnValue(new Promise(resolve => { resolveOld = resolve; }));
    const current = createMockAgentAdapterBindings({ adapters: [row({ name: 'current-platform' })] });
    const view = renderTab(oldApi);
    view.rerender(<PlatformBindingsTab agentActor={AGENT} api={current} />);
    await screen.findByText('飞书 · current-platform');
    await act(async () => resolveOld([row({ name: 'old-platform', boundAgent: AGENT })]));
    expect(screen.queryByText('飞书 · old-platform')).toBeNull();
    expect(screen.getByText('飞书 · current-platform')).toBeVisible();
  });

  it('binds an unbound adapter through the cell document', async () => {
    const user = userEvent.setup();
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    const bind = vi.spyOn(api, 'bind');
    renderTab(api);

    await user.click(await screen.findByRole('button', { name: '绑定' }));

    await waitFor(() => expect(screen.getByText(/已绑定 · 接入运行中/)).toBeVisible());
    expect(bind).toHaveBeenCalledWith('feishu-main', AGENT);
  });

  it('shows an adapter bound to another Agent as read-only', async () => {
    const previous = 'agent:owner:node:a.researcher';
    const api = createMockAgentAdapterBindings({
      adapters: [row({ boundAgent: previous })],
    });
    render(
      <PlatformBindingsTab
        agentActor={AGENT}
        agentLabels={[{ uri: previous, displayName: '研究者' }]}
        api={api}
      />,
    );

    expect(await screen.findByText(/已绑定 研究者/)).toBeVisible();
    expect(screen.queryByRole('button', { name: '绑定' })).toBeNull();
  });

  it('unbinds with the current actor', async () => {
    const user = userEvent.setup();
    const api = createMockAgentAdapterBindings({
      adapters: [row({ boundAgent: AGENT })],
    });
    const unbind = vi.spyOn(api, 'unbind');
    renderTab(api);

    await user.click(await screen.findByRole('button', { name: '解除绑定' }));

    await waitFor(() => expect(screen.getByText('未绑定')).toBeVisible());
    expect(unbind).toHaveBeenCalledWith('feishu-main', AGENT);
  });

  it('allows binding a second adapter on the same agent (cell documents carry no one-to-one rule)', async () => {
    const api = createMockAgentAdapterBindings({
      adapters: [
        row({ name: 'feishu-main', boundAgent: AGENT }),
        row({ name: 'feishu-ops' }),
      ],
    });
    renderTab(api);

    expect(await screen.findByRole('button', { name: '解除绑定' })).toBeEnabled();
    expect(screen.queryByText(/先解除/)).toBeNull();
    expect(screen.getByRole('button', { name: '绑定' })).toBeEnabled();
  });

  it('shows the daemon error and keeps the list unconfirmed when binding is refused', async () => {
    const user = userEvent.setup();
    const api: AgentAdapterBindingsApi = {
      routes: { available: false },
      async list() {
        return [row({})];
      },
      async bind() {
        throw new Error('绑定已变化，请刷新后重新确认。');
      },
      async unbind() {},
    };
    renderTab(api);

    await user.click(await screen.findByRole('button', { name: '绑定' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('绑定已变化');
    expect(screen.getByRole('button', { name: '绑定' })).toBeVisible();
  });

  it.each([
    ['bind', '绑定', '绑定平台'],
    ['unbind', '解除绑定', '解除绑定'],
  ] as const)('a refused %s notifies and leaves the control enabled without an inline message', async (method, label, subject) => {
    const user = userEvent.setup();
    const api = createMockAgentAdapterBindings({
      adapters: [row({ boundAgent: method === 'unbind' ? AGENT : null })],
    });
    vi.spyOn(api, method).mockRejectedValue(new MethodRefusedError('agent.cell.update', 'METHOD_NOT_AVAILABLE'));
    renderWithNotifications(<PlatformBindingsTab agentActor={AGENT} api={api} />);

    const button = await screen.findByRole('button', { name: label });
    await user.click(button);

    await waitFor(() => expectUnavailableToast(`${subject}暂不可用。`));
    expect(screen.getByRole('button', { name: label })).toBeEnabled();
    expect(document.querySelector('.agent-form-message')).toBeNull();
    expect(document.body.textContent).not.toMatch(/METHOD_NOT_AVAILABLE/);
  });

  it('a refused manual refresh notifies and keeps the rows that were read', async () => {
    const user = userEvent.setup();
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    renderWithNotifications(<PlatformBindingsTab agentActor={AGENT} api={api} />);
    await screen.findByText('飞书 · feishu-main');
    vi.spyOn(api, 'list').mockRejectedValue(new MethodRefusedError('adapter.list', 'METHOD_NOT_AVAILABLE'));

    await user.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => expectUnavailableToast('读取平台绑定暂不可用。'));
    expect(screen.getByText('飞书 · feishu-main')).toBeVisible();
    expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled();
  });

  it('a self-started read refused before dispatch with nothing to show renders nothing and raises nothing', async () => {
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    vi.spyOn(api, 'list').mockRejectedValue(new MethodRefusedError('adapter.list', 'METHOD_NOT_AVAILABLE'));
    const { container } = renderWithNotifications(<PlatformBindingsTab agentActor={AGENT} api={api} />);

    await waitFor(() => expect(api.list).toHaveBeenCalled());
    await waitFor(() => expect(document.querySelector('#platform-bindings-title')).toBeNull());
    expect(container.querySelector('.toast')).toBeNull();
    expect(document.body.textContent).not.toMatch(/读取失败|重试|METHOD_NOT_AVAILABLE/);
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('a refused re-read after a write keeps the rows and raises nothing', async () => {
    const user = userEvent.setup();
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    renderWithNotifications(<PlatformBindingsTab agentActor={AGENT} api={api} />);
    await user.click(await screen.findByRole('button', { name: '绑定' }));
    await screen.findByText(/已绑定 · 接入运行中/);
    vi.spyOn(api, 'list').mockRejectedValue(new MethodRefusedError('adapter.list', 'METHOD_NOT_AVAILABLE'));
    await user.click(screen.getByRole('button', { name: '解除绑定' }));

    await waitFor(() => expect(api.list).toHaveBeenCalled());
    expect(screen.getByText('飞书 · feishu-main')).toBeVisible();
    expect(document.querySelector('.toast')).toBeNull();
    expect(document.body.textContent).not.toMatch(/读取失败|重试/);
  });

  it('a bind refused by the catalog is notified before busy starts and sends no request', async () => {
    const user = userEvent.setup();
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    const bind = vi.spyOn(api, 'bind');
    renderWithNotifications(<PlatformBindingsTab agentActor={AGENT} api={api}
      checkAccess={(method) => method === 'agent.cell.update' ? { code: 'METHOD_NOT_CALLABLE', source: method } : null} />);

    await user.click(await screen.findByRole('button', { name: '绑定' }));

    await waitFor(() => expectUnavailableToast('绑定平台暂不可用。'));
    expect(bind).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '绑定' })).toBeEnabled();
  });

  it('renders no route card when the api has no route source', async () => {
    const api = createMockAgentAdapterBindings({ adapters: [row({})] });
    renderTab({ ...api, routes: { available: false } });
    await screen.findByText('飞书 · feishu-main');
    expect(screen.queryByRole('heading', { name: '消息路由' })).toBeNull();
    expect(screen.queryByRole('button', { name: '配置消息路由' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/暂不支持|未接入/);
  });
});
