import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpTransport } from '../../../src/client';
import { createGeneratedClient } from '../../../src/client/generated';
import { createMockPlatformIntegrationsApi } from '../../../src/dev/mock/platform-integrations';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import { createPlatformIntegrationsApiFromClient } from '../../../src/features/connections/platform-integrations-api';

const input = { provider: 'lark' as const, name: 'demo', appId: 'cli_test', secret: 'synthetic-only' };
const saved = {
  ok: true, adapter: { provider: 'lark', name: 'demo', appId: 'cli_test' },
  configured: true, reloaded: true, nextAction: 'none',
};
const row = {
  id: 'lark:demo', provider: 'lark', name: 'demo', status: 'stopped', online: false,
  configured: true, desired: false, processRunning: false, agent: null,
};

function setup(reloaded = true, failure?: Error) {
  let loaded = false;
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'test-only' });
  const invoke = vi.spyOn(transport, 'invoke').mockImplementation(async method => {
    if (method === 'adapter.list') return { ok: true, adapters: loaded ? [row] : [] };
    if (method === 'adapter.register') {
      if (failure) throw failure;
      loaded = reloaded;
      return { ...saved, reloaded, nextAction: reloaded ? 'none' : 'reload' };
    }
    if (method === 'adapter.reload') {
      loaded = true;
      return { ok: true, added: ['demo'], updated: [], removed: [], removedRunning: [] };
    }
    throw new Error('unexpected method: ' + method);
  });
  const client = createGeneratedClient(transport);
  const api = createPlatformIntegrationsApiFromClient(client);
  return { client, api, invoke };
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('暂无平台接入');
  await user.click(screen.getByRole('button', { name: '添加接入' }));
  const dialog = await screen.findByRole('dialog');
  await user.click(within(dialog).getByLabelText('使用已有应用'));
  expect(within(dialog).getByLabelText('使用已有应用')).toBeChecked();
  await user.type(within(dialog).getByLabelText('名称'), input.name);
  await user.type(within(dialog).getByLabelText('App ID'), input.appId);
  await user.type(within(dialog).getByLabelText('App Secret'), input.secret);
  return dialog;
}

describe('register an existing platform app', () => {
  it('uses a single declared write without operation polling or extra reload', async () => {
    const { api, invoke } = setup();
    await expect(api.register!(input)).resolves.toEqual(saved);
    expect(invoke).toHaveBeenCalledExactlyOnceWith('adapter.register', {
      provider: 'lark', name: 'demo', appId: 'cli_test', appSecret: 'synthetic-only',
    }, { retry: 'never' });
  });

  it('blocks a disabled catalog capability before sending a secret', async () => {
    const { client, invoke } = setup();
    const api = createPlatformIntegrationsApiFromClient(client, () => ({ available: false, reason: 'SERVICE_NOT_READY' }));
    expect(api.capabilities.add.available).toBe(false);
    await expect(api.register!(input)).rejects.toMatchObject({ method: 'adapter.register', code: 'METHOD_NOT_AVAILABLE' });
    expect(invoke).not.toHaveBeenCalled();
  });

  it('does not retry an unknown registration write', async () => {
    const { api, invoke } = setup(true, new Error('response lost'));
    await expect(api.register!(input)).rejects.toThrow('response lost');
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it('keeps the mock duplicate rejection consistent with the declared error', async () => {
    const api = createMockPlatformIntegrationsApi({ integrations: [] });
    await api.register!(input);
    await expect(api.register!({ ...input, secret: 'replacement' })).rejects.toMatchObject({ code: 'ADAPTER_EXISTS' });
    expect(await api.list()).toHaveLength(1);
  });

  it('keeps an unknown write blocked if recovery cannot read the loaded configuration', async () => {
    const { api, invoke } = setup(true, new Error('response lost'));
    const user = userEvent.setup();
    render(<PlatformIntegrationsSection api={api} />);
    const dialog = await fill(user);
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    await screen.findByText('添加结果未知，请关闭后重新加载配置并查看列表。');
    await user.click(within(dialog).getByRole('button', { name: '取消' }));
    vi.spyOn(api, 'list').mockRejectedValue(new Error('read unavailable'));
    await user.click(screen.getByRole('button', { name: '重新加载全部配置' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '添加接入' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const reopened = await screen.findByRole('dialog');
    await user.click(within(reopened).getByLabelText('使用已有应用'));
    await user.type(within(reopened).getByLabelText('名称'), input.name);
    await user.type(within(reopened).getByLabelText('App ID'), input.appId);
    await user.type(within(reopened).getByLabelText('App Secret'), input.secret);
    expect(within(reopened).getByRole('button', { name: '开始添加' })).toBeDisabled();
    expect(invoke.mock.calls.filter(call => call[0] === 'adapter.register')).toHaveLength(1);
  });

  it('adds through the shared form and reads the saved stopped adapter', async () => {
    const { api, invoke } = setup();
    const user = userEvent.setup();
    render(<PlatformIntegrationsSection api={api} />);
    const dialog = await fill(user);
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    expect(await screen.findByText('平台接入已添加。')).toBeVisible();
    expect(within(await screen.findByRole('list', { name: '平台接入列表' })).getByText('demo')).toBeVisible();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(invoke.mock.calls.map(call => call[0])).toEqual(['adapter.list', 'adapter.register', 'adapter.list']);
  });

  it('keeps recovery reload available when saving succeeded but the first list is empty', async () => {
    const { api, invoke } = setup(false);
    const user = userEvent.setup();
    render(<PlatformIntegrationsSection api={api} />);
    const dialog = await fill(user);
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    expect(await screen.findByText('接入已保存，请重新加载配置。')).toBeVisible();
    expect(screen.getByText('暂无平台接入')).toBeVisible();
    expect(screen.queryByText('平台接入已添加。')).toBeNull();
    await user.click(screen.getByRole('button', { name: '重新加载全部配置' }));
    expect(await screen.findByText('demo')).toBeVisible();
    expect(invoke.mock.calls.filter(call => call[0] === 'adapter.register')).toHaveLength(1);
  });

  it('clears the secret and prevents replay while the first result is unknown', async () => {
    const { api, invoke } = setup(true, new Error('response lost'));
    const user = userEvent.setup();
    render(<PlatformIntegrationsSection api={api} />);
    const dialog = await fill(user);
    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));
    expect(await screen.findByText('添加结果未知，请关闭后重新加载配置并查看列表。')).toBeVisible();
    expect(within(dialog).getByLabelText('App Secret')).toHaveValue('');
    await user.type(within(dialog).getByLabelText('App Secret'), 'do-not-send-again');
    expect(within(dialog).getByRole('button', { name: '开始添加' })).toBeDisabled();
    await user.click(within(dialog).getByRole('button', { name: '取消' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByRole('button', { name: '重新加载全部配置' })).toBeEnabled();
    expect(invoke.mock.calls.filter(call => call[0] === 'adapter.register')).toHaveLength(1);
  });
});
