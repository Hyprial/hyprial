import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useReducer } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { GuiCapabilitiesProvider } from '../../../src/app/capabilities';
import { ToastHost } from '../../../src/components/ToastHost';
import { NotificationsProvider } from '../../../src/components/notifications';
import type { MethodAccess } from '../../../src/client';
import { ContactsPage } from '../../../src/pages/ContactsPage';
import { appReducer } from '../../../src/state/appReducer';
import { createFixtureState } from '../../../src/dev/fixtures/fixture-state';
import type { AppState } from '../../../src/state/types';
import type { SharedCredentialsApi } from '../../../src/features/connections/shared-credentials-api';
import type { ProviderLoginFile } from '../../../src/features/agent-provider/impl/api';
import { createTestAgentsApi } from '../../unified-backend/agents-test-client';
import { CREATE_NEW_AGENT } from '../../fixtures/agents/agent-mutations';
import { stubFirstRunActions } from '../../support/first-run-actions';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

interface ApiOptions {
  readonly created?: boolean;
  readonly entries?: ReturnType<typeof credentialEntries>;
  readonly createError?: Error;
  readonly allowError?: Error;
  readonly loginError?: Error;
  readonly methodAccess?: MethodAccess;
  readonly keyReadError?: Error;
}

function credentialEntries() {
  return [
    {
      keyId: 'anthropic:ANTHROPIC_API_KEY',
      provider: 'anthropic',
      label: '旧 Key',
      state: 'active',
      createdAt: 1,
      lastUsedAt: null,
      grants: [],
    },
    {
      keyId: 'openai:OPENAI_API_KEY',
      provider: 'openai',
      label: 'OpenAI Key',
      state: 'active',
      createdAt: 2,
      lastUsedAt: null,
      grants: [],
    },
    {
      keyId: 'pk-revoked',
      provider: 'anthropic',
      label: '已撤销 Key',
      state: 'revoked',
      createdAt: 3,
      lastUsedAt: null,
      grants: [],
    },
  ];
}

function sharedCredentials(entries = credentialEntries()): SharedCredentialsApi {
  return {
    listKeys: async () => entries,
    listSecretGrants: async () => [],
    addKey: async () => ({ status: 'failed', message: 'unused' }),
    replaceKey: async () => ({ status: 'failed', message: 'unused' }),
    deleteKey: async () => ({ status: 'failed', message: 'unused' }),
    revokeGrant: async () => ({ status: 'failed', message: 'unused' }),
  };
}

function renderCreate(options: ApiOptions & {
  readonly pickLoginFiles?: (
    provider: string,
  ) => Promise<readonly ProviderLoginFile[]>;
} = {}) {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const rows: Record<string, unknown>[] = [];
  const api = createTestAgentsApi({
    async request(method, params) {
      calls.push({ method, params });
      switch (method) {
        case 'agent.list':
          return { ok: true, agents: rows, agentsWithoutRoutine: [] };
        case 'agent.provider.default':
          return { ok: true, provider: 'claude', previous: null };
        case 'agent.secret.list':
          return {
            ok: true,
            actor: 'agent:acme:node-1:worker',
            agent: 'a.worker',
            agentId: 'a.worker',
            grants: [],
          };
        case 'agent.create':
          if (options.createError) throw options.createError;
          rows.push({ ...(CREATE_NEW_AGENT.result!.agent as Record<string, unknown>),
            displayName: params.name, cwd: params.cwd ?? '/var/mock-agents/worker/workspace' });
          return {
            ...CREATE_NEW_AGENT.result,
            agent: rows[rows.length - 1],
            created: options.created ?? true,
          };
        case 'agent.start':
          return { ok: true, accepted: true, running: true };
        case 'agent.secret.grant':
          if (options.allowError) throw options.allowError;
          return {
            ok: true,
            changed: true,
            grant: {
              ...params, ownerActor: null, ownerEntityToken: null,
            },
            restart: { state: 'not-supported' },
          };
        case 'agent.provider.login':
          if (options.loginError) throw options.loginError;
          return {
            ok: true,
            actor: params.actor,
            provider: params.provider,
            files: [{
              path: '/agent/.credentials.json',
              bytes: 3,
              replaced: false,
              unchanged: false,
            }],
          };
        default:
          throw new Error(`unexpected method ${method}`);
      }
    },
  }, options.methodAccess);

  function Harness() {
    const initial: AppState = {
      ...createFixtureState(),
      page: 'contacts',
      agentPanel: { kind: 'create' },
    };
    const [state, dispatch] = useReducer(appReducer, initial);
    return (
      <GuiCapabilitiesProvider
        value={{
          firstRun: stubFirstRunActions(),
          agents: api,
          sharedCredentials: { ...sharedCredentials(options.entries), ...(options.keyReadError ? {
            listKeys: async () => { throw options.keyReadError; },
          } : {}) },
          agentProviderLoginFilePicker: options.pickLoginFiles,
          methodAccess: options.methodAccess,
        }}
      >
        <NotificationsProvider dispatch={dispatch}>
          <ContactsPage state={state} dispatch={dispatch} />
          <ToastHost notices={state.notices} dispatch={dispatch} />
        </NotificationsProvider>
      </GuiCapabilitiesProvider>
    );
  }

  const user = userEvent.setup();
  render(<Harness />);
  return { calls, user };
}

async function choose(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
  option: string,
): Promise<void> {
  await user.click(screen.getByLabelText(label));
  await user.click(await screen.findByRole('option', { name: option }));
}

describe('Agent 创建时的凭据组合', () => {
  it('keeps a Key read failure out of the selected Auth form', async () => {
    const { user } = renderCreate({ keyReadError: new Error('Key 管理暂不可用。'), pickLoginFiles: async () => [] });
    await screen.findByLabelText('harness');
    await choose(user, 'harness', 'pi');
    await choose(user, '凭据', 'Auth');
    expect(screen.queryByText('Key 管理暂不可用。')).toBeNull();
    expect(screen.getByRole('button', { name: '选择两个登录文件' })).toBeEnabled();
    await user.click(screen.getByLabelText('凭据'));
    expect(screen.getByRole('option', { name: 'Key' })).toHaveAttribute('aria-disabled', 'true');
  });

  it.each(['claude', 'codex', 'pi', 'Antigravity'])('explains the disabled Key option when %s has no compatible stored credential', async (harnessLabel) => {
    const { user } = renderCreate({ entries: [{ ...credentialEntries()[0], provider: 'moonshot' }],
      pickLoginFiles: async () => [] });
    await choose(user, 'harness', harnessLabel);
    expect(screen.getByLabelText('凭据')).toHaveTextContent('Auth');
    await user.click(screen.getByLabelText('凭据'));
    expect(screen.getByRole('option', { name: 'Key（暂无适用 Key）' })).toHaveAttribute('aria-disabled', 'true');
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: harnessLabel === 'pi' ? '选择两个登录文件' : '选择登录文件' })).toBeEnabled();
    const location = harnessLabel === 'claude' ? '~/.claude/.credentials.json'
      : harnessLabel === 'codex' ? '~/.codex/auth.json'
        : harnessLabel === 'Antigravity' ? '~/.gemini/antigravity-cli/antigravity-oauth-token'
          : '~/.pi/agent/auth.json、~/.pi/agent/models-store.json';
    const fileNames = harnessLabel === 'claude' ? '.credentials.json'
      : harnessLabel === 'codex' ? 'auth.json'
        : harnessLabel === 'Antigravity' ? 'antigravity-oauth-token'
          : 'auth.json、models-store.json';
    expect(screen.getByText(fileNames)).toBeVisible();
    const locationButton = screen.getByRole('button', { name: '登录文件位置' });
    const tooltip = screen.getByRole('tooltip', { hidden: true });
    expect(tooltip).toHaveAttribute('hidden');
    expect(tooltip).toHaveTextContent(location);
    await user.hover(locationButton);
    expect(tooltip).toBeVisible();
    await user.unhover(locationButton);
    expect(tooltip).toHaveAttribute('hidden');
  });
  it('blocks creation and changing harness while the selected Auth file is being read', async () => {
    let finish!: (files: readonly ProviderLoginFile[]) => void;
    const picking = new Promise<readonly ProviderLoginFile[]>(resolve => { finish = resolve; });
    const { user, calls } = renderCreate({ pickLoginFiles: () => picking });
    await choose(user, 'harness', 'claude');
    await choose(user, '凭据', 'Auth');
    await user.click(screen.getByRole('button', { name: '选择登录文件' }));
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeDisabled();
    expect(screen.getByLabelText('harness')).toBeDisabled();
    expect(calls.some(call => call.method === 'agent.create')).toBe(false);
    await act(async () => { finish([{ filename: '.credentials.json', contentBase64: 'e30=' }]); });
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
  });
  it('creates a Pi Agent with credentials, then waits for explicit model configuration', async () => {
    const { calls, user } = renderCreate();
    await user.type(screen.getByLabelText('名称'), 'pi-worker');
    await choose(user, 'harness', 'pi');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(await screen.findByTestId('agent-detail-panel')).toHaveTextContent('pi-worker');
    expect(screen.queryByText('Agent 创建已完成，Key 已授权。')).toBeNull();
    expect(calls.some(call => call.method === 'agent.secret.grant')).toBe(true);
    expect(calls.some(call => call.method === 'agent.start')).toBe(false);
  });
  it('选择 Key 后创建 Agent，并把 Key 授权到新 actor', async () => {
    const { calls, user } = renderCreate();

    await user.type(screen.getByLabelText('名称'), 'worker-key');
    await choose(user, 'harness', 'claude');
    await choose(user, '凭据', 'Key');
    await choose(user, 'Key', '旧 Key · anthropic');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(await screen.findByTestId('agent-detail-panel')).toHaveTextContent('worker-key');
    expect(screen.queryByRole('button', { name: '配置 Agent' })).toBeNull();
    const methods = calls.map((call) => call.method);
    expect(methods.indexOf('agent.create')).toBeLessThan(
      methods.indexOf('agent.secret.grant'),
    );
    expect(methods).not.toContain('agent.start');
    expect(calls.find((call) => call.method === 'agent.create')?.params).toMatchObject({
      name: 'worker-key',
      preferredHarness: 'claude',
      provider: 'anthropic',
      model: null,
    });
    expect(calls.find((call) => call.method === 'agent.secret.grant')?.params).toMatchObject({
      actor: 'agent:acme:node-1:worker',
      source: 'user-provider', entryId: 'anthropic', fieldName: 'ANTHROPIC_API_KEY',
      grantId: 'model-anthropic', revision: 1, environmentNames: ['ANTHROPIC_API_KEY'],
    });
  });

  it('requires explicit provider selection for an existing CLI entry with an unclassified field', async () => {
    const { user, calls } = renderCreate({ entries: [{ ...credentialEntries()[0],
      keyId: 'deepseek-name-only:apiKey', label: 'deepseek-name-only', provider: '',
    }] });
    await user.type(screen.getByLabelText('名称'), 'explicit-provider');
    await choose(user, 'harness', 'pi');
    await choose(user, '凭据', 'Key');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(calls.some(call => call.method === 'agent.create')).toBe(false);
    await choose(user, '模型服务商', 'deepseek');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await screen.findByTestId('agent-detail-panel');
    expect(calls.find(call => call.method === 'agent.secret.grant')?.params).toMatchObject({
      entryId: 'deepseek-name-only', fieldName: 'apiKey', environmentNames: ['DEEPSEEK_API_KEY'],
    });
    expect(calls.some(call => call.method === 'agent.start')).toBe(false);
  });

  it('does not offer the incomplete Codex shared OpenAI Key path', async () => {
    const { user } = renderCreate({ pickLoginFiles: async () => [] });

    await choose(user, 'harness', 'codex');
    expect(screen.getByLabelText('凭据')).toHaveTextContent('Auth');
    await user.click(screen.getByLabelText('凭据'));
    expect(screen.queryByRole('option', { name: 'Key' })).toBeNull();
  });

  it('选择 Auth 时先选登录文件，创建成功后写入', async () => {
    const pickLoginFiles = vi.fn(async (
      provider: string,
    ): Promise<readonly ProviderLoginFile[]> => [{
      filename: provider === 'claude' ? '.credentials.json' : 'auth.json',
      contentBase64: 'YWJj',
    }]);
    const { calls, user } = renderCreate({ pickLoginFiles });

    await user.type(screen.getByLabelText('名称'), 'worker-auth');
    await choose(user, 'harness', 'claude');
    await choose(user, '凭据', 'Auth');
    await user.click(screen.getByRole('button', { name: '选择登录文件' }));
    expect(await screen.findByText('已选择：.credentials.json')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(await screen.findByTestId('agent-detail-panel')).toHaveTextContent('worker-auth');
    expect(calls.some(call => call.method === 'agent.start')).toBe(false);
    expect(screen.queryByText('Agent 创建已完成，登录文件已写入。')).toBeNull();
    expect(pickLoginFiles).toHaveBeenCalledWith('claude');
    expect(calls.map((call) => call.method)).toContain('agent.provider.login');
    const login = calls.find((call) => call.method === 'agent.provider.login');
    expect(login?.params).toMatchObject({
      actor: 'agent:acme:node-1:worker',
      provider: 'claude',
      files: [{ filename: '.credentials.json', contentBase64: 'YWJj' }],
    });
  });

  it('凭据步骤失败时保留 Agent 并提示到凭据页重试', async () => {
    const { user } = renderCreate({
      loginError: new Error('登录写入失败'),
      pickLoginFiles: async () => [{
        filename: '.credentials.json',
        contentBase64: 'YWJj',
      }],
    });

    await user.type(screen.getByLabelText('名称'), 'worker-partial');
    await choose(user, 'harness', 'claude');
    await choose(user, '凭据', 'Auth');
    await user.click(screen.getByRole('button', { name: '选择登录文件' }));
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Agent 已创建，但凭据未完成');
    expect(alert).toHaveTextContent('结果未知');
    expect(await screen.findByTestId('agent-detail-panel')).toHaveTextContent('worker-partial');
    await user.click(screen.getByRole('link', { name: 'Agent' }));
    expect(screen.queryByText(/Agent 已创建，但凭据未完成/)).toBeNull();
  });

  it('a credential write refused after the create only states that the credential is not configured', async () => {
    let refused = false;
    const methodAccess: MethodAccess = (method) => refused && method === 'agent.provider.login'
      ? { available: false, code: 'METHOD_NOT_CALLABLE', reason: '目录原因句' }
      : { available: true };
    const { user, calls } = renderCreate({
      methodAccess,
      pickLoginFiles: async () => [{ filename: '.credentials.json', contentBase64: 'YWJj' }],
    });
    await user.type(screen.getByLabelText('名称'), 'worker-refused');
    await choose(user, 'harness', 'claude');
    await choose(user, '凭据', 'Auth');
    await user.click(screen.getByRole('button', { name: '选择登录文件' }));
    refused = true;
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('Agent 已创建，凭据未配置。');
    expect(document.querySelectorAll('.toast')).toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/重试|目录原因句/);
    expect(calls.some(call => call.method === 'agent.provider.login')).toBe(false);
  });

  it('offers no credential kind whose write method is refused and creates without a credential section', async () => {
    const methodAccess: MethodAccess = (method) => method === 'agent.provider.login' || method === 'agent.secret.grant'
      ? { available: false, code: 'METHOD_NOT_FOUND', reason: '目录原因句' }
      : { available: true };
    const { user, calls } = renderCreate({
      methodAccess,
      pickLoginFiles: async () => [{ filename: '.credentials.json', contentBase64: 'YWJj' }],
    });
    expect(screen.queryByLabelText('凭据')).toBeNull();
    await user.type(screen.getByLabelText('名称'), 'worker-bare');
    await choose(user, 'harness', 'claude');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(await screen.findByTestId('agent-detail-panel')).toHaveTextContent('worker-bare');
    expect(document.querySelectorAll('.toast')).toHaveLength(0);
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(calls.some(call => call.method === 'agent.secret.provider-list')).toBe(false);
    expect(calls.some(call => call.method === 'agent.secret.grant' || call.method === 'agent.provider.login')).toBe(false);
  });

  it('passes the optional working directory through the generated create request', async () => {
    const { calls, user } = renderCreate();
    await user.type(screen.getByLabelText('名称'), 'cwd-worker');
    await choose(user, 'harness', 'claude');
    await user.type(screen.getByLabelText('工作目录'), '/tmp/synthetic-project');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(await screen.findByTestId('agent-detail-panel')).toBeVisible();
    expect(calls.find(call => call.method === 'agent.create')?.params.cwd).toBe('/tmp/synthetic-project');
    expect(screen.getByTestId('overview-cwd')).toHaveTextContent('/tmp/synthetic-project');
  });

  it('labels Pi selection as two files and reports both selected filenames', async () => {
    const { user } = renderCreate({ entries: [], pickLoginFiles: async () => [
      { filename: 'auth.json', contentBase64: 'e30=' },
      { filename: 'models-store.json', contentBase64: 'e30=' },
    ] });
    await choose(user, 'harness', 'pi');
    await user.click(screen.getByRole('button', { name: '选择两个登录文件' }));
    expect(await screen.findByText('已选择：auth.json、models-store.json')).toBeVisible();
  });

  it('复用已有 Agent 时不改动其凭据', async () => {
    const { calls, user } = renderCreate({ created: false });

    await user.type(screen.getByLabelText('名称'), 'worker');
    await choose(user, 'harness', 'claude');
    await choose(user, '凭据', 'Key');
    await choose(user, 'Key', '旧 Key · anthropic');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(
      await screen.findByText('已复用现有 Agent；未改动其凭据。'),
    ).toBeTruthy();
    expect(calls.some((call) => call.method === 'agent.secret.grant')).toBe(false);
  });
});
