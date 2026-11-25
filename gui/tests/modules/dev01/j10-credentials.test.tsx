import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CredentialsSection } from '../../../src/features/connections/CredentialsSection';
import { App } from '../../../src/App';
import { createEmptyState } from '../../../src/state/appReducer';
import { createFixtureFirstRunActions } from '../../../src/dev/first-run-capabilities';
import { createStartupMockAgents } from '../../../src/dev/mock/startup-agents';
import { createMockSharedCredentialsApi } from '../../../src/dev/mock/agent-settings';
import { HttpError, MethodRefusedError } from '../../../src/client';

function StartupPreview() {
  return <App initialState={createEmptyState('complete')} capabilitiesFactory={dispatch => ({
    firstRun: createFixtureFirstRunActions('complete', dispatch, 'ready'),
    firstRunWizardEnabled: false,
    agents: createStartupMockAgents().api,
    sharedCredentials: createMockSharedCredentialsApi(),
  })} />;
}
import {
  createSharedCredentialsApiFromClient,
  type CredentialEntry,
  type SharedCredentialWrite,
  type SharedCredentialsApi,
  type SharedGrantRow,
} from '../../../src/features/connections/shared-credentials-api';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const KEY: CredentialEntry = {
  keyId: 'deepseek-official:DEEPSEEK_API_KEY',
  provider: 'deepseek',
  label: 'DeepSeek',
  state: 'active',
  createdAt: 1_760_000_000_000,
  lastUsedAt: null,
  grants: [
    {
      agent: '00000000-0000-0000-0000-000000000001',
      agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa',
      grantId: 'grant-1',
      provider: 'deepseek',
      state: 'active',
    },
  ],
};

const AUTH: SharedGrantRow = {
  grantId: 'auth-1',
  actor: 'squire',
  source: 'agent-shared',
  entryId: 'claude',
  fieldName: 'CLAUDE_CODE_OAUTH_TOKEN',
  environments: ['CLAUDE_CODE_OAUTH_TOKEN'],
  ownerActor: 'squire',
  revision: 1,
};

function createApi(overrides: Partial<SharedCredentialsApi> = {}) {
  const calls: string[] = [];
  const api: SharedCredentialsApi = {
    async listKeys() {
      calls.push('listKeys');
      return [KEY];
    },
    async listSecretGrants() { return [AUTH]; },
    async testKey() { return { kind: 'model', status: 200, ok: true, latencyMs: 10 }; },
    async addKey(provider, label, value) {
      calls.push(`addKey:${provider}:${label}:${value}`);
      return { status: 'saved', message: 'Key 已保存。' };
    },
    async replaceKey(keyId, value) {
      calls.push(`replaceKey:${keyId}:${value}`);
      return { status: 'saved', message: 'Key 已更新。' };
    },
    async deleteKey(keyId) {
      calls.push(`deleteKey:${keyId}`);
      return { status: 'saved', message: '凭据已删除。' };
    },
    async revokeGrant(agent, provider) {
      calls.push(`revokeGrant:${agent}:${provider}`);
      return { status: 'saved', message: '授权已撤销。' };
    },
    ...overrides,
  };
  return { api, calls };
}

describe('Agent 设置凭据模块', () => {
  it('maps Pi Kimi and Moonshot keys through declared environmentNames', async () => {
    const providerWrite = vi.fn(async (params) => ({ ok: true, entryId: params.entryId, fieldNames: [params.fieldName] }));
    const providerList = vi.fn(async () => ({ ok: true, entries: [] }));
    const api = createSharedCredentialsApiFromClient({ agent: { secret: { providerWrite, providerList } } } as never);
    for (const [provider, fieldName] of [['kimi-coding', 'KIMI_API_KEY'], ['moonshotai', 'MOONSHOT_API_KEY']]) {
      expect((await api.addKey(provider, 'pi-key', 'synthetic')).status).toBe('saved');
      expect(providerWrite).toHaveBeenLastCalledWith({ entryId: 'pi-key', fieldName, value: 'synthetic' });
    }
  });
  it('lists key and auth credentials with different details', async () => {
    const user = userEvent.setup();
    const { api } = createApi();
    render(<CredentialsSection api={api} />);

    const list = await screen.findByRole('list', { name: '凭据列表' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(within(list).getByText('DeepSeek')).toBeInTheDocument();
    expect(within(list).getByText('Key · deepseek · 1 条授权')).toBeInTheDocument();
    expect(within(list).getByText('claude')).toBeInTheDocument();
    expect(within(list).getByText('服务秘钥 · 所有者 squire · 1 个 Agent 已授权')).toBeInTheDocument();

    const keyItem = within(list).getByText('DeepSeek').closest('li');
    const authItem = within(list).getByText('claude').closest('li');
    expect(keyItem).not.toBeNull();
    expect(authItem).not.toBeNull();

    await user.click(within(keyItem as HTMLElement).getByRole('button', { name: '查看详情' }));
    expect(within(keyItem as HTMLElement).getByText('deepseek')).toBeInTheDocument();
    expect(within(keyItem as HTMLElement).getByText('已保存')).toBeInTheDocument();
    expect(within(keyItem as HTMLElement).getByRole('button', { name: '撤销 a.aaaaaaaaaaaaaaaaaaaaaaaaaa 的授权' })).toBeEnabled();
    expect(within(keyItem as HTMLElement).getByRole('button', { name: '更换 Key' })).toBeEnabled();
    expect(within(keyItem as HTMLElement).getByRole('button', { name: '删除凭据' })).toBeEnabled();
    expect(within(keyItem as HTMLElement).queryByText('user-provider')).toBeNull();
    expect(within(keyItem as HTMLElement).queryByText(/environmentNames|revision/)).toBeNull();

    await user.click(within(authItem as HTMLElement).getByRole('button', { name: '查看详情' }));
    expect(within(authItem as HTMLElement).getByText('服务秘钥授权记录（只读）。')).toBeInTheDocument();
    expect(within(authItem as HTMLElement).queryByRole('button', { name: '删除凭据' })).toBeNull();
    expect(within(authItem as HTMLElement).queryByRole('button', { name: /撤销 .* 的授权/ })).toBeNull();
  });

  it('adds a key and refreshes the list immediately', async () => {
    const user = userEvent.setup();
    const entries = [KEY];
    const calls: string[] = [];
    const api: SharedCredentialsApi = {
      async listKeys() {
        calls.push('listKeys');
        return [...entries];
      },
      async listSecretGrants() { return [AUTH]; },
      async addKey(provider, label, value) {
        calls.push(`addKey:${provider}:${label}:${value}`);
        entries.push({ ...KEY, keyId: 'pk.aaaaaaaaaaaaaaaaaaaaaaaaaa', provider, label, grants: [] });
        return { status: 'saved', message: 'Key 已保存。' };
      },
      async replaceKey() {
        return { status: 'saved', message: 'Key 已更新。' };
      },
      async deleteKey() {
        return { status: 'saved', message: '凭据已删除。' };
      },
      async revokeGrant() {
        return { status: 'saved', message: '授权已撤销。' };
      },
    };
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });

    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.click(screen.getByLabelText('模型服务商'));
    await user.click(screen.getByRole('option', { name: 'OpenAI' }));
    expect(screen.getByLabelText('名称')).toHaveValue('OpenAI');
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key');
    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(calls).toContain('addKey:openai:OpenAI:qa-fake-key');
    expect(await screen.findByText('Key 已保存。')).toBeInTheDocument();
    expect(await screen.findByText('OpenAI')).toBeInTheDocument();
    expect(screen.getAllByText('OpenAI')).toHaveLength(1);
    expect(document.body.textContent).not.toContain('qa-fake-key');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('adds multiple keys for the same provider with different names', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });

    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.clear(screen.getByLabelText('名称'));
    await user.type(screen.getByLabelText('名称'), 'deepseek-work');
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key-1');
    await user.click(screen.getByRole('button', { name: '保存' }));

    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.clear(screen.getByLabelText('名称'));
    await user.type(screen.getByLabelText('名称'), 'deepseek-spare');
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key-2');
    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(calls).toContain('addKey:deepseek:deepseek-work:qa-fake-key-1');
    expect(calls).toContain('addKey:deepseek:deepseek-spare:qa-fake-key-2');
  });

  it('rejects an empty name before dispatching', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });

    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.clear(screen.getByLabelText('名称'));
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key');

    expect(await screen.findByText('名称不能为空。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
    expect(calls).toEqual(['listKeys']);
  });

  it('stops writes after an unknown result until the user refreshes', async () => {
    const user = userEvent.setup();
    const unknown: SharedCredentialWrite = {
      status: 'unknown',
      message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。',
    };
    const { api } = createApi({ addKey: async () => unknown });
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });

    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key');
    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(await screen.findByText(unknown.message)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加 Key' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: '刷新' }));
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.getByRole('button', { name: '添加 Key' })).toBeEnabled();
  });

  it('renders no section when the key list is refused before dispatch', async () => {
    const { api } = createApi({
      listKeys: async () => {
        throw new MethodRefusedError('user.provider.list', 'METHOD_NOT_FOUND');
      },
    });
    const { container } = render(<CredentialsSection api={api} />);

    await waitFor(() => expect(screen.queryByText('正在读取凭据')).toBeNull());
    expect(container.querySelector('.credentials-section')).toBeNull();
    expect(screen.queryByRole('button', { name: '添加 Key' })).toBeNull();
  });

  it('renders a fixture model read-only with no add action', async () => {
    const user = userEvent.setup();
    render(<CredentialsSection model={{ entries: [KEY], authGrants: [AUTH] }} />);

    const list = screen.getByRole('list', { name: '凭据列表' });
    expect(within(list).getByText('DeepSeek')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '添加 Key' })).toBeNull();
    const keyItem = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
    await user.click(within(keyItem).getByRole('button', { name: '查看详情' }));
    expect(within(keyItem).queryByRole('button', { name: '删除凭据' })).toBeNull();
    expect(within(keyItem).queryByRole('button', { name: '更换 Key' })).toBeNull();
    expect(within(keyItem).queryByRole('button', { name: '撤销 a.aaaaaaaaaaaaaaaaaaaaaaaaaa 的授权' })).toBeNull();
  });

  it('revokes one agent grant without deleting the key', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    render(<CredentialsSection api={api} />);
    const list = await screen.findByRole('list', { name: '凭据列表' });
    const keyItem = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
    await user.click(within(keyItem).getByRole('button', { name: '查看详情' }));
    await user.click(within(keyItem).getByRole('button', { name: '撤销 a.aaaaaaaaaaaaaaaaaaaaaaaaaa 的授权' }));

    expect(calls).toContain('revokeGrant:00000000-0000-0000-0000-000000000001:grant-1');
    expect(await screen.findByText('授权已撤销。')).toBeInTheDocument();
    expect(calls).not.toContain(`deleteKey:${KEY.keyId}`);
  });

  it('deletes a key only after confirmation', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    render(<CredentialsSection api={api} />);
    const list = await screen.findByRole('list', { name: '凭据列表' });
    const keyItem = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
    await user.click(within(keyItem).getByRole('button', { name: '查看详情' }));
    await user.click(within(keyItem).getByRole('button', { name: '删除凭据' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('删除后该 Key 及其全部 Agent 授权都会撤销，且不可恢复。')).toBeInTheDocument();
    expect(calls).not.toContain(`deleteKey:${KEY.keyId}`);

    await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
    expect(calls).toContain(`deleteKey:${KEY.keyId}`);
    expect(await screen.findByText('凭据已删除。')).toBeInTheDocument();
  });

  it('replaces a key value from the detail dialog', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    render(<CredentialsSection api={api} />);
    const list = await screen.findByRole('list', { name: '凭据列表' });
    const keyItem = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
    await user.click(within(keyItem).getByRole('button', { name: '查看详情' }));
    await user.click(within(keyItem).getByRole('button', { name: '更换 Key' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: '更换 Key' })).toBeInTheDocument();
    expect(within(dialog).getByText('仅更换 Key；名称、服务商、服务地址和已授权 Agent 保持不变。')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('新 Key')).toHaveFocus();
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeDisabled();

    await user.type(within(dialog).getByLabelText('新 Key'), 'qa-new-key');
    await user.click(within(dialog).getByRole('button', { name: '保存' }));

    expect(calls).toContain(`replaceKey:${KEY.keyId}:qa-new-key`);
    expect(calls.some(call => call.startsWith('addKey:'))).toBe(false);
    expect(await screen.findByText('Key 已更新。')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('Key · deepseek · 1 条授权')).toBeInTheDocument();
    expect(screen.getByText('DeepSeek')).toBeInTheDocument();
  });

  it('tests replacement through its API without saving or making a browser request', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    const probe = vi.spyOn(api, 'testKey');
    const browserFetch = vi.fn();
    vi.stubGlobal('fetch', browserFetch);
    try {
      render(<CredentialsSection api={api} />);
      const list = await screen.findByRole('list', { name: '凭据列表' });
      const item = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
      await user.click(within(item).getByRole('button', { name: '查看详情' }));
      await user.click(within(item).getByRole('button', { name: '更换 Key' }));
      const dialog = await screen.findByRole('dialog');
      await user.type(within(dialog).getByLabelText('新 Key'), 'qa-new-key');
      expect(within(dialog).getByRole('button', { name: '测试' })).toBeDisabled();
      await user.type(within(dialog).getByLabelText('测试模型'), 'deepseek-flash');
      await user.click(within(dialog).getByRole('button', { name: '测试' }));
      expect(probe).toHaveBeenCalledWith({ provider: 'deepseek', secret: 'qa-new-key', model: 'deepseek-flash' });
      expect(await within(dialog).findByText('测试通过 · 10 ms')).toBeVisible();
      expect(browserFetch).not.toHaveBeenCalled();
      expect(calls).toEqual(['listKeys']);
    } finally { vi.unstubAllGlobals(); }
  });

  it('cancels key replacement without dispatching', async () => {
    const user = userEvent.setup();
    const { api, calls } = createApi();
    render(<CredentialsSection api={api} />);
    const list = await screen.findByRole('list', { name: '凭据列表' });
    const keyItem = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
    await user.click(within(keyItem).getByRole('button', { name: '查看详情' }));
    await user.click(within(keyItem).getByRole('button', { name: '更换 Key' }));

    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('新 Key'), 'qa-new-key');
    await user.click(within(dialog).getByRole('button', { name: '取消' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(calls).toEqual(['listKeys']);
  });

  it.each(['DeepSeek', 'OpenAI', 'Anthropic'])('tests %s via the injected API', async name => {
    const user = userEvent.setup();
    const { api } = createApi();
    const probe = vi.spyOn(api, 'testKey');
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.click(screen.getByLabelText('模型服务商'));
    await user.click(screen.getByRole('option', { name }));
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key');
    expect(screen.getByRole('button', { name: '测试' })).toBeDisabled();
    await user.type(screen.getByLabelText('测试模型'), 'provider-model');
    await user.click(screen.getByRole('button', { name: '测试' }));
    expect(probe).toHaveBeenCalledWith({ provider: name.toLowerCase(), secret: 'qa-fake-key', model: 'provider-model' });
    expect(await screen.findByText('测试通过 · 10 ms')).toBeVisible();
    await user.type(screen.getByLabelText('测试模型'), '-changed');
    expect(screen.queryByText('测试通过 · 10 ms')).toBeNull();
  });

  it('keeps network failure distinct from a rejected Key', async () => {
    const user = userEvent.setup();
    const { api } = createApi({ async testKey() { return { kind: 'blocked', status: null, ok: null, latencyMs: 10 }; } });
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key');
    await user.type(screen.getByLabelText('测试模型'), 'deepseek-flash');
    await user.click(screen.getByRole('button', { name: '测试' }));
    expect(await screen.findByText('无法连接模型服务 · 10 ms')).toBeVisible();
    expect(screen.queryByText(/HTTP 401/)).toBeNull();
  });

  it('does not treat HTTP 200 without a successful model result as a passed test', async () => {
    const user = userEvent.setup();
    const { api } = createApi({ async testKey() { return { kind: 'model', status: 200, ok: false, latencyMs: 10 }; } });
    render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.type(screen.getByLabelText('Key'), 'synthetic-key');
    await user.type(screen.getByLabelText('测试模型'), 'model-id');
    await user.click(screen.getByRole('button', { name: '测试' }));
    expect(await screen.findByText('测试未通过 · HTTP 200 · 10 ms')).toBeVisible();
    expect(screen.queryByText(/测试通过/)).toBeNull();
  });

  it('discards a pending test result when the credential authority changes', async () => {
    const user = userEvent.setup();
    let resolve: (result: Awaited<ReturnType<NonNullable<SharedCredentialsApi['testKey']>>>) => void = () => {};
    const { api } = createApi({ testKey: () => new Promise(done => { resolve = done; }) });
    const { rerender } = render(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.type(screen.getByLabelText('Key'), 'synthetic-key');
    await user.type(screen.getByLabelText('测试模型'), 'model-id');
    await user.click(screen.getByRole('button', { name: '测试' }));
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '刷新' })).toBeDisabled();
    rerender(<CredentialsSection api={createApi().api} />);
    resolve({ kind: 'model', status: 200, ok: true, latencyMs: 10 });
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    expect(screen.getByLabelText('测试模型')).toHaveValue('');
    expect(screen.getByLabelText('Key')).toHaveValue('');
    expect(screen.queryByText(/测试通过/)).toBeNull();
  });

  it('offers no custom provider option and no endpoint field', async () => {
    const user = userEvent.setup();
    const api = createMockSharedCredentialsApi({ entries: [], authGrants: [] });
    render(<CredentialsSection api={api} />);
    await screen.findByText('暂无 Key');
    expect(screen.queryByRole('list', { name: 'Auth 清单' })).toBeNull();
    expect(screen.queryByText('暂无 Auth')).toBeNull();
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.click(screen.getByLabelText('模型服务商'));
    expect(screen.queryByRole('option', { name: /自定义/ })).toBeNull();
    expect(screen.queryByLabelText('服务地址')).toBeNull();
  });

  it.each(['openai-completions', 'anthropic-messages'] as const)(
    'keeps a previously stored %s custom record readable', async protocol => {
      const user = userEvent.setup();
      render(<CredentialsSection model={{ entries: [{ ...KEY, endpoint: 'https://example.invalid/v1', protocol }], authGrants: [] }} />);
      await user.click(screen.getByRole('button', { name: '查看详情' }));
      expect(screen.getByText('https://example.invalid/v1')).toBeVisible();
      expect(screen.queryByRole('button', { name: '保存' })).toBeNull();
    },
  );

  it('shows a newly added key in the mock list', async () => {
    const user = userEvent.setup();
    render(<StartupPreview />);

    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    await screen.findByRole('list', { name: '凭据列表' });

    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    await user.clear(screen.getByLabelText('名称'));
    await user.type(screen.getByLabelText('名称'), 'deepseek-2');
    await user.type(screen.getByLabelText('Key'), 'qa-fake-key');
    await user.click(screen.getByRole('button', { name: '保存' }));

    expect(await screen.findByText('deepseek-2')).toBeInTheDocument();
    expect(screen.getByText('Key · deepseek · 0 条授权')).toBeInTheDocument();
  });

  it('shows the mock credential list on the Agent 设置 page', async () => {
    const user = userEvent.setup();
    render(<StartupPreview />);

    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));

    expect(screen.getByRole('heading', { level: 2, name: '凭据设置' })).toBeInTheDocument();
    expect(await screen.findByText('DeepSeek')).toBeInTheDocument();
    expect(screen.getByText('claude')).toBeInTheDocument();
    expect(screen.getByText('服务秘钥 · 所有者 squire · 1 个 Agent 已授权')).toBeInTheDocument();
    expect(screen.queryByText(/当前默认/)).toBeNull();
    expect(screen.queryByRole('heading', { name: '模型服务' })).toBeNull();
  });
});

describe('startup mock 的 Agent 创建表单', () => {
  it('使用目录路径字段，不显示 fixture 的职责/服务商自由输入', async () => {
    const user = userEvent.setup();
    render(<StartupPreview />);

    await user.click(screen.getByRole('button', { name: 'Agent' }));
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(screen.getByLabelText('名称')).toBeInTheDocument();
    expect(screen.getByLabelText('harness')).toBeInTheDocument();
    expect(screen.getByLabelText('凭据')).toBeInTheDocument();
    expect(screen.getByLabelText('模型')).toBeInTheDocument();
    expect(screen.queryByLabelText('职责')).toBeNull();
    expect(screen.queryByLabelText('所属组织')).toBeNull();
    expect(screen.queryByLabelText('服务商')).toBeNull();
  });
});

describe('共享凭据 API 的 daemon 方法映射', () => {
  const AUTH_GRANT = {
    actor: 'squire',
    entryId: 'claude',
    environmentNames: ['CLAUDE_CODE_OAUTH_TOKEN'],
    fieldName: 'CLAUDE_CODE_OAUTH_TOKEN',
    grantId: 'auth-1',
    ownerActor: 'squire',
    ownerEntityToken: 'entity-1',
    revision: 1,
    source: 'agent-shared' as const,
  };
  const USER_GRANT = {
    ...AUTH_GRANT,
    actor: 'worker',
    entryId: 'deepseek-official',
    fieldName: 'DEEPSEEK_API_KEY',
    environmentNames: ['DEEPSEEK_API_KEY'],
    grantId: 'legacy-key-1',
    ownerActor: null,
    ownerEntityToken: null,
    source: 'user-provider' as const,
  };
  function fakeClient() {
    const calls: { method: string; params: Record<string, unknown> }[] = [];
    const client = { agent: { secret: {
      async providerList(params: Record<string, unknown>) {
        calls.push({ method: 'agent.secret.provider-list', params });
        return { ok: true, entries: [{ entryId: 'deepseek-official', fieldNames: ['DEEPSEEK_API_KEY'] }] };
      },
      async providerWrite(params: { entryId: string; fieldName: string; value: string }) {
        calls.push({ method: 'agent.secret.provider-write', params });
        return { ok: true, entryId: params.entryId, fieldNames: [params.fieldName] };
      },
      async list(params: Record<string, unknown>) {
        calls.push({ method: 'agent.secret.list', params });
        return { ok: true, grants: [USER_GRANT, AUTH_GRANT] };
      },
      async revoke(params: { actor: string; grantId: string }) {
        calls.push({ method: 'agent.secret.revoke', params });
        return { ok: true, revoked: true };
      },
    } } };
    return { client: client as never, rawClient: client, calls };
  }

  it('invents no probe method in the connected port and takes no custom endpoint', async () => {
    const { client, calls } = fakeClient();
    const api = createSharedCredentialsApiFromClient(client);
    expect(api.testKey).toBeUndefined();
    expect(api.addKey.length).toBe(3);
    expect(calls).toEqual([]);
  });

  it('keeps Key reads and writes available when service grants cannot be read', async () => {
    const { client, rawClient } = fakeClient();
    vi.spyOn(rawClient.agent.secret, 'list').mockRejectedValue(new Error('grants unavailable'));
    render(<CredentialsSection api={createSharedCredentialsApiFromClient(client)} />);
    expect(await screen.findByText('deepseek-official')).toBeVisible();
    expect(screen.getByText(/授权状态未确认/)).toBeVisible();
    expect(screen.getByRole('button', { name: '添加 Key' })).toBeEnabled();
  });

  it('keeps service grants visible when Key reads fail', async () => {
    const { client, rawClient } = fakeClient();
    vi.spyOn(rawClient.agent.secret, 'providerList').mockRejectedValue(new Error('keys unavailable'));
    render(<CredentialsSection api={createSharedCredentialsApiFromClient(client)} />);
    expect(await screen.findByText(AUTH_GRANT.entryId)).toBeVisible();
    expect(screen.getByRole('button', { name: '添加 Key' })).toBeDisabled();
  });

  it('reads only Keys when populating a Key selector', async () => {
    const { client, calls } = fakeClient();
    const api = createSharedCredentialsApiFromClient(client);
    expect(await api.listKeys()).toHaveLength(1);
    expect(calls.map(call => call.method)).toEqual(['agent.secret.provider-list']);
  });

  it('maps metadata, writes and exact grant revocation without inventing deletion', async () => {
    const { client, calls } = fakeClient();
    const api = createSharedCredentialsApiFromClient(client);
    const entries = await api.listKeys();
    const grants = await api.listSecretGrants();
    expect(entries).toEqual([{ keyId: KEY.keyId, provider: 'deepseek', label: 'deepseek-official',
      state: 'active', createdAt: null, lastUsedAt: null, grants: [], grantsKnown: false }]);
    expect(grants).toEqual([
      { grantId: 'legacy-key-1', actor: 'worker', source: 'user-provider', entryId: 'deepseek-official',
        fieldName: 'DEEPSEEK_API_KEY', environments: ['DEEPSEEK_API_KEY'], ownerActor: null, revision: 1 },
      { grantId: 'auth-1', actor: 'squire', source: 'agent-shared', entryId: 'claude',
        fieldName: 'CLAUDE_CODE_OAUTH_TOKEN', environments: ['CLAUDE_CODE_OAUTH_TOKEN'], ownerActor: 'squire', revision: 1 },
    ]);
    expect(api.deleteKey).toBeUndefined();
    await api.addKey('deepseek', 'deepseek-work', 'qa-fake-key');
    await api.replaceKey(KEY.keyId, 'qa-new-key');
    await api.revokeGrant(USER_GRANT.actor, USER_GRANT.grantId);
    expect(calls).toEqual([
      { method: 'agent.secret.provider-list', params: {} },
      { method: 'agent.secret.list', params: {} },
      { method: 'agent.secret.provider-list', params: {} },
      { method: 'agent.secret.provider-write', params: { entryId: 'deepseek-work', fieldName: 'DEEPSEEK_API_KEY', value: 'qa-fake-key' } },
      { method: 'agent.secret.provider-write', params: { entryId: 'deepseek-official', fieldName: 'DEEPSEEK_API_KEY', value: 'qa-new-key' } },
      { method: 'agent.secret.revoke', params: { actor: USER_GRANT.actor, grantId: USER_GRANT.grantId } },
    ]);
  });

  it('refuses mismatched write receipts without retrying', async () => {
    const providerWrite = vi.fn(async () => ({ ok: true, entryId: 'other', fieldNames: ['DEEPSEEK_API_KEY'] }));
    const providerList = vi.fn(async () => ({ ok: true, entries: [] }));
    const api = createSharedCredentialsApiFromClient({ agent: { secret: { providerWrite, providerList } } } as never);
    expect((await api.addKey('deepseek', 'deepseek-work', 'qa-fake-key')).status).toBe('unknown');
    expect((await api.replaceKey(KEY.keyId, 'qa-new-key')).status).toBe('unknown');
    expect(providerWrite).toHaveBeenCalledTimes(2);
  });

  it('renders a failed production key store read as an ordinary error and blocks writes', async () => {
    const { client, rawClient, calls } = fakeClient();
    vi.spyOn(rawClient.agent.secret, 'providerList').mockRejectedValue(new HttpError(
      'PROVIDER_STORE_UNAVAILABLE', 'store unavailable', 500,
      { method: 'agent.secret.provider-list', requestId: null, status: 500, dispatch: 'dispatched',
        catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 1 },
    ));
    render(<CredentialsSection api={createSharedCredentialsApiFromClient(client)} />);
    await screen.findByText('凭据读取失败，请刷新后重试。');
    expect(screen.getByRole('button', { name: '添加 Key' })).toBeDisabled();
    expect(screen.queryByText('暂无 Key')).toBeNull();
    expect(calls.some(call => call.method === 'agent.secret.provider-write')).toBe(false);
  });

  it('lets a method access refusal reach the handler as a classifiable error', async () => {
    const { client } = fakeClient();
    const api = createSharedCredentialsApiFromClient(client, () => ({
      available: false,
      code: 'METHOD_NOT_FOUND',
      reason: 'catalog sentence',
    }));
    await expect(api.listKeys()).rejects.toBeInstanceOf(MethodRefusedError);
    await expect(api.listKeys()).rejects.toMatchObject({ method: 'agent.secret.provider-list', code: 'METHOD_NOT_FOUND' });
  });
});
