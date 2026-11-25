import { createMockPlatformIntegrationsApi } from '../../../src/dev/mock/platform-integrations';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { PlatformIntegrationDetail } from '../../../src/features/connections/PlatformIntegrationDetail';
import userEvent from '@testing-library/user-event';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import {
  type AdapterOperation,
  type PlatformIntegrationsApi,
} from '../../../src/features/connections/platform-integrations-api';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

function renderSection(api: PlatformIntegrationsApi = createMockPlatformIntegrationsApi()) {
  const user = userEvent.setup();
  render(<PlatformIntegrationsSection api={api} />);
  return { user, api };
}

async function openDetail(
  user: ReturnType<typeof userEvent.setup>,
  name = 'feishu-main',
): Promise<HTMLElement> {
  const list = await screen.findByRole('list', { name: '平台接入列表' });
  const row = within(list).getByText(name).closest('li') as HTMLElement;
  await user.click(within(row).getByRole('button', { name: '详情' }));
  return row;
}

describe('平台接入模块', () => {
  it('shows global configuration reload below the list only after an integration exists', async () => {
    const api = createMockPlatformIntegrationsApi({ integrations: [] });
    const { user } = renderSection(api);
    await screen.findByText('暂无平台接入');
    expect(screen.queryByRole('button', { name: '重新加载全部配置' })).toBeNull();
    await api.beginAdd!({ provider: 'lark', name: 'configured', mode: 'adopt', appId: 'cli_test', secret: 'fake' });
    await user.click(screen.getByRole('button', { name: /^刷新$/ }));
    const list = await screen.findByRole('list', { name: '平台接入列表' });
    const reload = screen.getByRole('button', { name: '重新加载全部配置' });
    expect(list.compareDocumentPosition(reload) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(list).queryByRole('button', { name: '重新加载全部配置' })).toBeNull();
  });

  it('offers only supported platforms and clears cancelled progress when changing mode', async () => {
    const { user, api } = renderSection();
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByLabelText('平台'));
    expect(screen.getAllByRole('option')).toHaveLength(1);
    await user.click(screen.getByRole('option', { name: '飞书' }));
    await user.type(within(dialog).getByLabelText('名称'), 'cancelled-app');
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    await user.click(await within(dialog).findByRole('button', { name: '取消添加' }));
    expect(await within(dialog).findByText('已取消（模拟）')).toBeVisible();
    expect(within(dialog).queryByRole('link')).toBeNull();
    expect(within(dialog).queryByText(/有效期至/)).toBeNull();
    await user.click(within(dialog).getByLabelText('使用已有应用'));
    expect(within(dialog).queryByText(/已取消/)).toBeNull();
    await user.type(within(dialog).getByLabelText('App ID'), 'cli_adopted');
    await user.type(within(dialog).getByLabelText('App Secret'), 'fake-only');
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    expect(await screen.findByText('cancelled-app')).toBeVisible();
    expect((await api.list()).filter(row => row.name === 'cancelled-app')).toHaveLength(1);
  });

  it('shows observed grants and required gaps without claiming unknown authorization is empty', async () => {
    const base = createMockPlatformIntegrationsApi();
    const rows = await base.list();
    const api = createMockPlatformIntegrationsApi({ integrations: [{
      ...rows[0],
      authorization: { ...rows[0].authorization, capabilities: ['message-send', 'vendor-extra'], missingCapabilities: ['receive-p2p'] },
    }] });
    const { user } = renderSection(api);
    const row = await openDetail(user);
    expect(within(within(row).getByRole('list', { name: '已授权权限' })).getByText('message-send')).toBeVisible();
    expect(within(row).getByText('vendor-extra')).toBeVisible();
    const missing = within(row).getByRole('list', { name: '缺少权限' });
    expect(within(missing).getByText('receive-p2p')).toBeVisible();
    expect(missing).toHaveClass('platform-integrations__error');
  });

  it('keeps real authorization links manual and hides expired links', async () => {
    const base = createMockPlatformIntegrationsApi();
    const operation: AdapterOperation = {
      operationId: 'real-op', adapter: 'real-app', provider: 'lark', kind: 'add', status: 'awaiting_user',
      verificationUrl: 'https://accounts.feishu.cn/device?code=test-placeholder', expiresAt: Date.now() + 300_000,
    };
    const begin = vi.fn(async () => operation);
    const api = { ...base, beginAdd: begin, addOperation: vi.fn(async () => ({ ...operation, status: 'expired' as const })) };
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const { user } = renderSection(api);
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('名称'), 'real-app');
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    expect(await within(dialog).findByRole('link', { name: '打开平台授权页面' })).toHaveAttribute('href', operation.verificationUrl);
    expect(open).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: '刷新添加状态' }));
    expect(await within(dialog).findByText('已过期')).toBeVisible();
    expect(within(dialog).queryByRole('link')).toBeNull();
    expect(within(dialog).queryByText(/有效期至/)).toBeNull();
    expect(begin).toHaveBeenCalledTimes(1);
    open.mockRestore();
  });

  it('keeps mock operation outcomes independent, terminal and duplicate-free', async () => {
    const api = createMockPlatformIntegrationsApi({ integrations: [] });
    const first = await api.beginAdd!({ name: 'first', provider: 'lark', mode: 'create' });
    const second = await api.beginAdd!({ name: 'second', provider: 'lark', mode: 'create' });
    await expect(api.beginAdd!({ name: 'first', provider: 'lark', mode: 'create' })).rejects.toThrow();
    await api.cancelAdd!(first.operationId);
    await expect(api.addOperation!(first.operationId)).resolves.toMatchObject({ status: 'cancelled' });
    await expect(api.addOperation!(second.operationId)).resolves.toMatchObject({ status: 'succeeded' });
    await api.addOperation!(second.operationId);
    await expect(api.cancelAdd!(second.operationId)).resolves.toMatchObject({ status: 'succeeded' });
    expect((await api.list()).map(row => row.name)).toEqual(['second']);
  });

  it('offers only progress actions while adding or authorizing an app', async () => {
    const { user, api } = renderSection();
    const begin = vi.spyOn(api, 'beginAdd');
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '授权' }));
    expect(await within(row).findByRole('button', { name: '刷新授权状态' })).toBeEnabled();
    expect(within(row).queryByRole('button', { name: '授权' })).toBeNull();
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('名称'), 'feishu-progress');
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    expect(await within(dialog).findByRole('button', { name: '刷新添加状态' })).toBeEnabled();
    expect(within(dialog).queryByRole('button', { name: '开始添加' })).toBeNull();
    expect(within(dialog).getByLabelText('名称')).toBeDisabled();
    expect(begin).toHaveBeenCalledTimes(1);
    await user.click(within(dialog).getByRole('button', { name: /^关闭$/ }));
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const resumed = await screen.findByRole('dialog');
    expect(within(resumed).getByRole('button', { name: '刷新添加状态' })).toBeEnabled();
    expect(within(resumed).getByLabelText('名称')).toHaveValue('feishu-progress');
    expect(within(resumed).queryByRole('button', { name: '开始添加' })).toBeNull();
    expect(begin).toHaveBeenCalledTimes(1);
  });

  it('groups by provider and keeps lifecycle actions in the entry detail', async () => {
    const { user } = renderSection();
    const list = await screen.findByRole('list', { name: '平台接入列表' });
    expect(within(list).getByRole('heading', { name: /飞书/ })).toHaveTextContent('1');
    const row = within(list).getByText('feishu-main').closest('li') as HTMLElement;
    expect(within(row).queryByText('授权：未授权')).toBeNull();
    expect(within(row).queryByText('运行：离线')).toBeNull();
    expect(within(row).queryByText('身份未同步')).toBeNull();
    expect(within(row).queryByText('Agent：未绑定')).toBeNull();
    expect(within(row).queryByRole('button', { name: '授权' })).toBeNull();
    expect(within(row).getByRole('button', { name: '详情' })).toHaveAttribute('aria-expanded', 'false');

    await user.click(within(row).getByRole('button', { name: '详情' }));

    expect(within(row).getByRole('button', { name: '收起' })).toHaveAttribute('aria-expanded', 'true');
    expect(within(row).getByRole('button', { name: '授权' })).toBeEnabled();
    expect(within(row).getByRole('button', { name: '同步平台身份' })).toBeEnabled();
    expect(within(row).getByRole('button', { name: '启动' })).toBeEnabled();
    expect(within(row).getByRole('button', { name: '删除接入' })).toBeEnabled();
    expect(within(row).queryByRole('heading', { name: '删除接入' })).toBeNull();
    expect(within(row).queryByRole('button', { name: /绑定/ })).toBeNull();
    expect(within(row).queryByText('绑定与解绑在 Agent 配置中修改。')).toBeNull();
    expect(within(row).queryByText('由平台管理员批准应用权限。')).toBeNull();
    expect(within(row).queryByText('显示平台上已记录的应用、机器人和用户。')).toBeNull();
  });

  it('starts and stops an integration from its detail', async () => {
    const { user } = renderSection();
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '启动' }));
    expect(await within(row).findAllByText('运行：在线')).not.toHaveLength(0);
    await user.click(within(row).getByRole('button', { name: '停止' }));
    expect(await within(row).findAllByText('运行：离线')).not.toHaveLength(0);
  });

  it('reloads daemon adapter configs as a separate action from status refresh', async () => {
    const base = createMockPlatformIntegrationsApi();
    const reload = vi.spyOn(base, 'reload');
    const list = vi.spyOn(base, 'list');
    const { user } = renderSection(base);
    await screen.findByRole('list', { name: '平台接入列表' });

    await user.click(screen.getByRole('button', { name: /^刷新$/ }));
    expect(list).toHaveBeenCalledTimes(2);
    expect(reload).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '重新加载全部配置' }));

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('已重新加载接入配置。')).toBeInTheDocument();
  });

  it('completes simulated authorization without sending a fake code to the real platform', async () => {
    const { user } = renderSection();
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '授权' }));
    expect(await within(row).findByText('等待平台确认（模拟）')).toBeInTheDocument();
    expect(within(row).queryByRole('link', { name: '打开平台授权页面' })).toBeNull();
    await user.click(within(row).getByRole('button', { name: '刷新授权状态' }));
    expect(await within(row).findByRole('list', { name: '已授权权限' })).toBeVisible();
    expect(within(row).getByRole('button', { name: '重新授权' })).toBeInTheDocument();
    const granted = within(row).getByRole('list', { name: '已授权权限' });
    expect(within(granted).getByText('message-send')).toBeVisible();
    expect(within(granted).getByText('receive-p2p')).toBeVisible();
    expect(within(granted).getByText('receive-group-mention')).toBeVisible();
    expect(within(row).queryByRole('list', { name: '缺少权限' })).toBeNull();
  });

  it('adds a new app through the create flow and keeps the secret out of the page', async () => {
    const { user } = renderSection();
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText('平台')).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText('名称'), 'feishu-second');
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));

    expect(await within(dialog).findByText('等待平台确认（模拟）')).toBeInTheDocument();
    expect(within(dialog).queryByRole('link', { name: '打开平台授权页面' })).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: '刷新添加状态' }));
    await screen.findByText('feishu-second');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('adopts an existing app with app id and secret', async () => {
    const { user } = renderSection();
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByLabelText('使用已有应用'));
    await user.type(within(dialog).getByLabelText('名称'), 'feishu-existing');
    await user.type(within(dialog).getByLabelText('App ID'), 'cli_existing');
    await user.type(within(dialog).getByLabelText('App Secret'), 'qa-fake-secret');
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));

    expect(await screen.findByText('feishu-existing')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.body.textContent).not.toContain('qa-fake-secret');
  });

  it('syncs platform identities and renders observed rows without inventing an owner', async () => {
    const { user } = renderSection();
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '同步平台身份' }));
    expect(await screen.findByText('平台身份已同步：3 个。')).toBeInTheDocument();
    expect(within(row).getAllByText('已同步 3 个身份').length).toBeGreaterThan(0);

    const table = await within(row).findByRole('table', { name: '平台身份明细' });
    expect(within(table).getAllByText('已采集')).toHaveLength(3);
    expect(within(table).getByRole('columnheader', { name: '记录状态' })).toBeVisible();
    expect(within(table).queryByRole('columnheader', { name: 'Hyprial 账号' })).toBeNull();
    expect(within(table).queryByRole('columnheader', { name: '身份绑定' })).toBeNull();
    expect(within(table).queryByRole('button', { name: '查询绑定' })).toBeNull();
    expect(within(table).queryByRole('button', { name: '配置绑定' })).toBeNull();
    expect(within(table).getByText('机器人')).toBeVisible();
    expect(within(table).getByText('张三')).toBeVisible();
    expect(within(table).getByText('ou_mock_user')).toBeVisible();
    expect(within(table).getByText('chat-members:oc_mock_chat')).toBeVisible();
    expect(within(table).queryByText('已核验')).toBeNull();
  });

  it('keeps an existing verified identity row when syncing observations', async () => {
    const api = createMockPlatformIntegrationsApi({
      identities: {
        'feishu-main': [{
          kind: 'user',
          platformId: 'ou_verified',
          displayName: '李四',
          unionId: 'on_verified',
          standing: 'verified',
          source: 'test-verified',
          firstSeenMs: 1,
          lastSeenMs: 1,
        }],
      },
    });
    const { user } = renderSection(api);
    const row = await openDetail(user);

    const before = await within(row).findByRole('table', { name: '平台身份明细' });
    expect(within(before).getByText('李四')).toBeVisible();
    expect(within(before).getByText('已核验')).toBeVisible();

    await user.click(within(row).getByRole('button', { name: '同步平台身份' }));
    expect(await screen.findByText('平台身份已同步：3 个。')).toBeInTheDocument();
    const table = await within(row).findByRole('table', { name: '平台身份明细' });
    expect(within(table).getByText('李四')).toBeVisible();
    expect(within(table).getByText('已核验')).toBeVisible();
    expect(within(table).getByText('张三')).toBeVisible();
    expect(within(table).queryByRole('columnheader', { name: 'Hyprial 账号' })).toBeNull();
  });

  it('does not show rows from a stale list after switching platforms', async () => {
    const api = createMockPlatformIntegrationsApi();
    const row = (await api.list())[0];
    let finish!: (value: Awaited<ReturnType<NonNullable<PlatformIntegrationsApi['listIdentities']>>>) => void;
    api.listIdentities = vi.fn().mockReturnValueOnce(new Promise(resolve => { finish = resolve; }))
      .mockResolvedValue({ adapter: 'new', count: 0, identities: [] });
    const props = { api, busy: null, onAuthorize: vi.fn(), onPollAuthorization: vi.fn(),
      onCancelAuthorization: vi.fn(), onStartStop: vi.fn(), onSyncIdentities: vi.fn(), onRemove: vi.fn() };
    const view = render(<PlatformIntegrationDetail {...props} row={row} />);
    view.rerender(<PlatformIntegrationDetail {...props} row={{ ...row, name: 'new' }} />);
    await waitFor(() => expect(api.listIdentities).toHaveBeenCalledWith('new'));
    await act(async () => finish({ adapter: row.name, count: 1, identities: [{ kind: 'user', platformId: 'ou_stale',
      displayName: 'Old user', unionId: null, standing: 'observed', source: 'test', firstSeenMs: 1, lastSeenMs: 1 }] }));
    expect(screen.queryByText('ou_stale')).toBeNull();
  });

  it.each([['name', false], ['api', false], ['name', true], ['api', true]] as const)('ignores old sync after a %s switch (new sync pending: %s)', async (change, newPending) => {
    const api = createMockPlatformIntegrationsApi();
    const row = (await api.list())[0];
    const data = (id: string) => ({ adapter: row.name, count: 1, identities: [{
      kind: 'user' as const, platformId: id, displayName: id, unionId: null,
      standing: 'observed' as const, source: 'test', firstSeenMs: 1, lastSeenMs: 1,
    }] });
    api.listIdentities = vi.fn(async name => data(name === row.name ? 'ou_old' : 'ou_new'));
    const nextApi = change === 'api' ? { ...api, listIdentities: vi.fn(async () => data('ou_new')) } : api;
    let finishOld!: () => void;
    let finishNew!: () => void;
    const oldSync = vi.fn(() => new Promise<void>(resolve => { finishOld = resolve; }));
    const newSync = vi.fn(() => new Promise<void>(resolve => { finishNew = resolve; }));
    const props = { api, busy: null, onAuthorize: vi.fn(), onPollAuthorization: vi.fn(),
      onCancelAuthorization: vi.fn(), onStartStop: vi.fn(), onSyncIdentities: oldSync, onRemove: vi.fn() };
    const view = render(<PlatformIntegrationDetail {...props} row={row} />);
    await screen.findAllByText('ou_old');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '同步平台身份' }));
    view.rerender(<PlatformIntegrationDetail {...props} api={nextApi}
      row={change === 'name' ? { ...row, name: 'new' } : row} onSyncIdentities={newSync} />);
    await screen.findAllByText('ou_new');
    if (newPending) await user.click(screen.getByRole('button', { name: '同步平台身份' }));
    await act(async () => finishOld());
    expect(screen.queryAllByText('ou_old')).toHaveLength(0);
    expect(screen.getAllByText('ou_new').length).toBeGreaterThan(0);
    if (!newPending) await user.click(screen.getByRole('button', { name: '同步平台身份' }));
    expect(newSync).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '同步平台身份' })).toBeDisabled();
    await act(async () => finishNew());
    expect(screen.getByRole('button', { name: '同步平台身份' })).toBeEnabled();
  });

  it('preserves stored identity facts across repeated syncs without inventing membership', async () => {
    const api = createMockPlatformIntegrationsApi({ identities: { 'feishu-main': [{
      kind: 'user', platformId: 'ou_mock_user', displayName: '旧名', unionId: 'on_verified',
      standing: 'verified', source: 'test-verified',
      firstSeenMs: 1, lastSeenMs: 1,
    }, {
      kind: 'user', platformId: 'ou_previously_seen', displayName: '历史记录', unionId: null,
      standing: 'observed', source: 'event:previous', firstSeenMs: 2, lastSeenMs: 2,
    }] } });
    const first = await api.syncIdentities!('feishu-main');
    expect(first.identitiesRecorded).toBe(3);
    const result = await api.listIdentities!('feishu-main');
    expect(result.identities).toHaveLength(4);
    expect(result.identities.find(row => row.platformId === 'ou_mock_user')).toMatchObject({
      displayName: '张三', unionId: 'on_verified',
      standing: 'verified', source: 'test-verified', firstSeenMs: 1,
    });
    expect(result.identities.some(row => row.platformId === 'ou_previously_seen')).toBe(true);
    await api.syncIdentities!('feishu-main');
    const second = await api.listIdentities!('feishu-main');
    expect(second.identities.map(row => row.firstSeenMs)).toEqual(result.identities.map(row => row.firstSeenMs));
    const fresh = createMockPlatformIntegrationsApi();
    await fresh.syncIdentities!('feishu-main');
    expect((await fresh.listIdentities!('feishu-main')).identities.every(row => row.unionId === null)).toBe(true);
  });

  it('previews and removes an integration after confirmation', async () => {
    const { user } = renderSection();
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '删除接入' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('绑定 Agent：未绑定')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
    expect(await screen.findByText('暂无平台接入')).toBeInTheDocument();
  });

  it('removes a stopped integration even when an Agent is still bound', async () => {
    const api = createMockPlatformIntegrationsApi({
      integrations: [{
        name: 'feishu-bound',
        provider: 'lark',
        status: 'stopped',
        configured: true,
        desired: false,
        processRunning: false,
        online: false,
        boundAgent: 'agent:owner:node:worker',
        authorization: {
          adapter: 'feishu-bound',
          appId: 'cli_bound',
          state: 'authorized',
          capabilities: ['im:message'],
          missingCapabilities: [],
          checkedAt: 1,
        },
        identities: { state: 'synced', count: 1 },
      }],
    });
    const { user } = renderSection(api);
    const row = await openDetail(user, 'feishu-bound');
    await user.click(within(row).getByRole('button', { name: '删除接入' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('绑定 Agent：agent:owner:node:worker')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
    expect(await screen.findByText('暂无平台接入')).toBeInTheDocument();
  });

  it('keeps controls operable and shows no explanation when capabilities are refused', async () => {
    const api = createMockPlatformIntegrationsApi();
    const refused = (source: string) => ({ available: false, code: 'METHOD_NOT_FOUND', source });
    const unavailable: PlatformIntegrationsApi = {
      ...api,
      capabilities: {
        add: refused('adapter.register'),
        authorization: refused('adapter.authorization.begin'),
        identities: refused('adapter.identities.sync'),
      },
    };
    const { user } = renderSection(unavailable);
    const row = await openDetail(user);
    expect(screen.getByRole('button', { name: '添加接入' })).toBeEnabled();
    expect(within(row).getByRole('button', { name: '授权' })).toBeEnabled();
    expect(within(row).getByRole('button', { name: '同步平台身份' })).toBeEnabled();
    expect(within(row).getByRole('button', { name: '删除接入' })).toBeEnabled();
    expect(document.body.textContent ?? '').not.toMatch(/尚未提供|不可用/);
  });

});
