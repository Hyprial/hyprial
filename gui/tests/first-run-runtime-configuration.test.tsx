import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, expect, it, vi } from 'vitest';
import { FirstRunOnboarding } from '../src/features/onboarding';
import type { FirstRunActions, RuntimeConfigurationResult } from '../src/features/onboarding';
import type { AgentDraft } from '../src/features/agents';
import type { CredentialEntry, SharedCredentialsApi } from '../src/features/connections';
import type { ProviderLoginFile } from '../src/features/agent-provider';

type User = ReturnType<typeof userEvent.setup>;

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

function entry(overrides: Partial<CredentialEntry> = {}): CredentialEntry {
  return {
    keyId: 'deepseek-official',
    provider: 'deepseek',
    label: 'DeepSeek Key',
    state: 'active',
    createdAt: 1,
    lastUsedAt: null,
    grants: [],
    ...overrides,
  };
}

function credentials(entries: readonly CredentialEntry[]): SharedCredentialsApi {
  return {
    listKeys: async () => entries,
    listSecretGrants: async () => [],
    addKey: async () => ({ status: 'failed', message: 'unused' }),
    replaceKey: async () => ({ status: 'failed', message: 'unused' }),
    revokeGrant: async () => ({ status: 'failed', message: 'unused' }),
  } as SharedCredentialsApi;
}

function configured(actorUri = 'agent:test-owner@hub:node:first-agent'): RuntimeConfigurationResult {
  return { status: 'configured', actorUri, created: true };
}

async function enterName(user: User, name = 'first-agent'): Promise<void> {
  await user.type(screen.getByLabelText('名称'), name);
}

async function walkToRuntime(
  user: User,
  actions: FirstRunActions,
): Promise<void> {
  render(<FirstRunOnboarding actions={actions} />);
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await user.click(await screen.findByRole('button', { name: '继续到设备' }));
  await user.click(await screen.findByRole('button', { name: '初始化当前设备' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
}

async function choose(
  user: User,
  label: string,
  option: string,
): Promise<void> {
  await user.click(screen.getByLabelText(label));
  await user.click(await screen.findByRole('option', { name: option }));
}

async function waitForKey(user: User): Promise<void> {
  await waitFor(() =>
    expect(screen.getByLabelText('Key')).toHaveTextContent('DeepSeek Key'),
  );
  await user.click(screen.getByLabelText('Key'));
  await user.click(await screen.findByRole('option', { name: /DeepSeek Key/ }));
}

it('does not default the first-run Agent name to squire and requires an explicit name before creating', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  expect(screen.getByLabelText('名称')).toHaveValue('');
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));

  expect(await screen.findByText('请填写 Agent 名称')).toBeInTheDocument();
  expect(configureAgent).not.toHaveBeenCalled();
});

it('creates the first-run Agent through the shared Agent form', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  expect(
    await screen.findByRole('heading', { name: '运行配置' }),
  ).toBeInTheDocument();
  expect(
    await screen.findByRole('heading', { name: '创建 Agent' }),
  ).toBeInTheDocument();
  expect(screen.getByLabelText('名称')).toHaveValue('');
  await enterName(user, 'first-agent');
  await waitForKey(user);
  await choose(user, 'harness', 'claude');
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));

  await waitFor(() => expect(configureAgent).toHaveBeenCalledTimes(1));
  expect(configureAgent.mock.calls[0][0]).toMatchObject({
    displayName: 'first-agent',
    preferredHarness: 'claude',
    provider: 'deepseek',
    model: null,
    credential: { kind: 'key', keyId: 'deepseek-official', provider: 'deepseek' },
  });
  expect(
    await screen.findByRole('heading', { name: '首次对话' }),
  ).toBeInTheDocument();
  expect(
    document.querySelector('[data-step-nav="runtime"]'),
  ).toHaveAttribute('data-status', 'completed');
});

it('keeps the fixed routed model when the Key carries an endpoint', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([
      entry({ endpoint: 'https://api.deepseek.invalid/v1', protocol: 'openai-completions' }),
    ]),
  });

  await enterName(user);
  await waitForKey(user);
  expect(screen.getByLabelText('模型')).toHaveTextContent('deepseek-flash');
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await waitFor(() => expect(configureAgent).toHaveBeenCalledTimes(1));
  expect(configureAgent.mock.calls[0][0]).toMatchObject({
    preferredHarness: 'pi',
    provider: 'deepseek',
    model: 'deepseek-flash',
  });
});

it('shows and submits the supported default for an ordinary Pi DeepSeek Key', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const user = userEvent.setup();
  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent, runtimeCredentials: credentials([entry()]),
  });
  await enterName(user);
  await waitForKey(user);
  expect(screen.getByLabelText('模型')).toHaveTextContent('deepseek-flash');
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await waitFor(() => expect(configureAgent).toHaveBeenCalledTimes(1));
  expect(configureAgent.mock.calls[0][0]).toMatchObject({ model: 'deepseek-flash' });
});

it('adds a connection through the shared form without losing the Agent draft', async () => {
  let keys: readonly CredentialEntry[] = [];
  const connection = { version: 1 as const, connectionRef: '0123456789abcdef0123456789abcdef', configRevision: 1,
    endpoint: 'https://model.example/v1', protocol: 'openai-completions' as const, profile: 'openai-chat-completions/v1' as const, preset: { id: 'custom', version: 1 } };
  const api: SharedCredentialsApi = {
    ...credentials([]),
    listKeys: async () => keys,
    listConnectionPresets: async () => [],
    configureKey: vi.fn(async () => {
      keys = [entry({ provider: 'connection', label: 'gateway', connection })];
      return { status: 'saved' as const };
    }),
  };
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const user = userEvent.setup();
  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent, runtimeCredentials: api, modelConfigurationApi: { configureModel: vi.fn() } as never,
  });
  expect(screen.getByLabelText('名称')).toHaveValue('');
  await user.type(screen.getByLabelText('名称'), 'first-agent');
  await choose(user, 'harness', 'pi');
  await user.type(screen.getByLabelText('模型 ID'), 'hy4-flash');
  await user.click(await screen.findByRole('button', { name: '添加连接' }));
  const dialog = within(screen.getByRole('dialog', { name: '添加连接' }));
  await user.type(dialog.getByLabelText('名称'), 'gateway');
  await user.type(dialog.getByLabelText('服务地址'), connection.endpoint);
  await user.type(dialog.getByLabelText('Key', { exact: true }), 'synthetic-key');
  await user.click(dialog.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '添加连接' })).toBeNull());
  await choose(user, '连接', `gateway · ${connection.endpoint}`);
  expect(screen.getByLabelText('名称')).toHaveValue('first-agent');
  expect(screen.queryByRole('heading', { name: 'Auth' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '查看详情' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await waitFor(() => expect(configureAgent).toHaveBeenCalledTimes(1));
  expect(configureAgent.mock.calls[0][0]).toMatchObject({
    displayName: 'first-agent', model: 'hy4-flash',
    modelConfiguration: { configuration: { connectionRef: connection.connectionRef, model: 'hy4-flash' } },
  });
  expect(api.configureKey).toHaveBeenCalledTimes(1);
});

it('clears the Key-specific model when switching to native Auth', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const user = userEvent.setup();
  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent, runtimeCredentials: credentials([entry()]),
    pickRuntimeLoginFiles: async () => [
      { filename: 'auth.json', contentBase64: 'ZmFrZQ==' },
      { filename: 'auth2.json', contentBase64: 'ZmFrZQ==' },
    ],
  });
  await enterName(user);
  await waitForKey(user);
  await choose(user, '凭据', 'Auth');
  await user.click(screen.getByRole('button', { name: '选择两个登录文件' }));
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await waitFor(() => expect(configureAgent).toHaveBeenCalledTimes(1));
  expect(configureAgent.mock.calls[0][0]).toMatchObject({
    provider: null, model: null, credential: { kind: 'auth', harness: 'pi' },
  });
});

it('offers native Auth without a Key source', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  const loginFile: ProviderLoginFile = {
    filename: 'auth.json',
    contentBase64: 'ZmFrZQ==',
  };
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    pickRuntimeLoginFiles: async () => [loginFile, { ...loginFile, filename: 'auth2.json' }],
  });

  await screen.findByRole('heading', { name: '创建 Agent' });
  await enterName(user);
  await choose(user, '凭据', 'Auth');
  await user.click(screen.getByRole('button', { name: '选择两个登录文件' }));
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));

  await waitFor(() => expect(configureAgent).toHaveBeenCalledTimes(1));
  expect(configureAgent.mock.calls[0][0]).toMatchObject({
    preferredHarness: 'pi',
    credential: { kind: 'auth', harness: 'pi' },
  });
  expect(
    await screen.findByRole('heading', { name: '首次对话' }),
  ).toBeInTheDocument();
});

it('does not complete the runtime step from a recorded receipt alone', async () => {
  const configureAgent = vi.fn(async (_draft: AgentDraft) => configured());
  render(<FirstRunOnboarding actions={{
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
    getSetupState: async () => ({
      keySaved: false,
      pendingAction: null,
    }),
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'recorded',
        revision: 1,
        confirmed: ['configuration'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  expect(
    await screen.findByLabelText('账号：账号已登录'),
  ).toBeInTheDocument();
  expect(
    document.querySelector('[data-step-nav="runtime"]'),
  ).not.toHaveAttribute('data-status', 'completed');
  expect(configureAgent).not.toHaveBeenCalled();
});

it('blocks another create after an unknown runtime write result', async () => {
  const configureAgent = vi.fn(async (): Promise<RuntimeConfigurationResult> => ({
    status: 'unknown',
    message: '运行配置结果未确认（review）',
  }));
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await screen.findByText('运行配置结果未确认（review）');
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(configureAgent).toHaveBeenCalledTimes(1);
});

it('keeps the runtime write locked after leaving and reopening the step', async () => {
  const configureAgent = vi.fn(async (): Promise<RuntimeConfigurationResult> => ({
    status: 'unknown',
    message: '运行配置结果未确认（review）',
  }));
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await screen.findByText('运行配置结果未确认（review）');
  await user.click(screen.getByRole('button', { name: '取消' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));

  expect(await screen.findByText('运行配置结果未确认（review）')).toBeInTheDocument();
  await enterName(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(configureAgent).toHaveBeenCalledTimes(1);
});

it('ignores a late configured result after leaving the step', async () => {
  let resolveConfiguration: (result: RuntimeConfigurationResult) => void = () => {};
  const configureAgent = vi.fn(() => new Promise<RuntimeConfigurationResult>((resolve) => {
    resolveConfiguration = resolve;
  }));
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await user.click(screen.getByRole('button', { name: '取消' }));
  resolveConfiguration(configured());

  await waitFor(() =>
    expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'device'),
  );
  expect(screen.queryByRole('heading', { name: '首次对话' })).not.toBeInTheDocument();
  expect(
    document.querySelector('[data-step-nav="runtime"]'),
  ).not.toHaveAttribute('data-status', 'completed');
});

it('does not repeat an in-flight runtime write after cancel and reopening', async () => {
  const configureAgent = vi.fn(() => new Promise<RuntimeConfigurationResult>(() => {}));
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await user.click(screen.getByRole('button', { name: '取消' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(configureAgent).toHaveBeenCalledTimes(1);
});

it('does not copy an old unknown result into a new actions context', async () => {
  let resolveOld: (value: RuntimeConfigurationResult) => void = () => {};
  const oldActions: FirstRunActions = {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent: () => new Promise((resolve) => { resolveOld = resolve; }),
    runtimeCredentials: credentials([entry()]),
  };
  const user = userEvent.setup();
  const view = render(<FirstRunOnboarding actions={oldActions} />);
  async function enterRuntime(): Promise<void> {
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    await user.click(await screen.findByRole('button', { name: '初始化当前设备' }));
    await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  }

  await enterRuntime();
  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  view.rerender(<FirstRunOnboarding actions={{
    ...oldActions,
    configureAgent: async () => configured(),
  }} />);
  await screen.findByRole('button', { name: '登录 Hyprial' });
  await act(async () => resolveOld({ status: 'unknown', message: '旧上下文的未知结果' }));
  await enterRuntime();
  await waitForKey(user);

  expect(screen.queryByText('旧上下文的未知结果')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
});

it('keeps a same-generation late unknown result locked after leaving', async () => {
  let resolveConfiguration: (result: RuntimeConfigurationResult) => void = () => {};
  const configureAgent = vi.fn(() => new Promise<RuntimeConfigurationResult>((resolve) => {
    resolveConfiguration = resolve;
  }));
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  await user.click(screen.getByRole('button', { name: '取消' }));
  resolveConfiguration({ status: 'unknown', message: '迟到未知结果' });
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));

  expect(await screen.findByText('迟到未知结果')).toBeInTheDocument();
  await enterName(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(configureAgent).toHaveBeenCalledTimes(1);
});

it('does not advance when the shared Agent configuration fails', async () => {
  const configureAgent = vi.fn(async (): Promise<RuntimeConfigurationResult> => ({
    status: 'failed',
    message: 'Agent 创建失败。',
  }));
  const user = userEvent.setup();

  await walkToRuntime(user, {
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    configureAgent,
    runtimeCredentials: credentials([entry()]),
  });

  await enterName(user);
  await waitForKey(user);
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Agent 创建失败。');
  expect(
    screen.queryByRole('heading', { name: '首次对话' }),
  ).not.toBeInTheDocument();
  expect(
    document.querySelector('[data-active-step]'),
  ).toHaveAttribute('data-active-step', 'model');
});
