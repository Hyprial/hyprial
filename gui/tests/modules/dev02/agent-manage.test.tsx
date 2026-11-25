import { createTestAgentsApi, explicitAgentFixtureSeed } from '../../unified-backend/agents-test-client';
import { render, screen, waitFor, within } from '@testing-library/react';
import { vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { useReducer } from 'react';
import type { ReactElement } from 'react';
import { appReducer } from '../../../src/state/appReducer';
import { createFixtureState } from '../../../src/dev/fixtures/fixture-state';
import type { AppState } from '../../../src/state/types';
import type { AgentSeed } from '../../../src/state/agent-selectors';
import { buildManagedAgent } from '../../../src/state/agent-selectors';
import {
  type AgentExtensionSeed,
  type AgentExtensionView,
  getAgentExtension,
} from '../../../src/features/agents/impl/agent-extensions';
import {
  type AgentDraft,
  type AgentMutationGateway,
} from '../../../src/features/agents/impl/AgentMutationGateway';
import { createLocalAgentGateway } from '../../../src/features/agents';
import { ContactsPage } from '../../../src/pages/ContactsPage';
import { CredentialsTab } from '../../../src/features/agents/impl/detail/CredentialsTab';
import type { SharedCredentialsApi } from '../../../src/features/connections/shared-credentials-api';
import { buildAgentScenario } from '../../../src/dev/j2-scenarios';
import { DEMO_BASELINE } from '../../support/demo-baseline';

// Radix Select 在 jsdom 打开弹出层需要这些 API 存在（真实浏览器原生提供）。
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const M04 = buildAgentScenario('m04')!.seed;

interface PageOptions {
  readonly seed?: AgentSeed;
  readonly extensions?: AgentExtensionSeed;
  readonly gateway?: AgentMutationGateway;
}

function renderPage(options: PageOptions = {}) {
  const initial: AppState = {
    ...createFixtureState(DEMO_BASELINE),
    page: 'contacts',
    agentPanel: { kind: 'list' },
  };
  const latest: { state: AppState } = { state: initial };
  const seed = options.seed ?? explicitAgentFixtureSeed();
  const gateway = options.gateway ?? createLocalAgentGateway({
    agents: [], connections: seed.connections, organizations: seed.organizations ?? [],
  });

  function Harness(): ReactElement {
    const [state, dispatch] = useReducer(appReducer, initial);
    latest.state = state;
    return (
      <ContactsPage
        state={state}
        dispatch={dispatch}
        seed={seed}
        gateway={gateway}
        extensions={options.extensions}
      />
    );
  }

  const user = userEvent.setup();
  return { user, latest, view: render(<Harness />) };
}

function extensionOf(
  extensions: AgentExtensionSeed | undefined,
  agentId: string,
): AgentExtensionView {
  return getAgentExtension(extensions, agentId);
}

function lastCreateButton(): HTMLElement {
  const buttons = screen.getAllByRole('button', { name: '创建 Agent' });
  return buttons[buttons.length - 1];
}

async function openDetail(
  user: ReturnType<typeof userEvent.setup>,
  agentId: string,
): Promise<void> {
  await user.click(screen.getByTestId(`contact-${agentId}`));
}

describe('Agent 列表的 dev-02 扩展维度', () => {
  it('无扩展数据时卡片不伪造运行状态', () => {
    renderPage();
    const card = screen.getByTestId('contact-agent-squire');
    expect(within(card).queryByText('运行中')).toBeNull();
    expect(within(card).queryByText('未运行')).toBeNull();
  });

  it('预配置 Squire 显示「预配置」徽标，其他 Agent 不显示', () => {
    renderPage({ seed: M04 });
    const squire = screen.getByTestId('contact-agent-squire');
    expect(within(squire).getByText('预配置')).toBeTruthy();
    const other = screen
      .getAllByTestId(/^contact-agent-/)
      .find((node) => node !== squire);
    expect(other).toBeTruthy();
    expect(within(other!).queryByText('预配置')).toBeNull();
  });

  it('注入扩展后显示运行状态，未提供组织来源时不显示组织标签', () => {
    renderPage({
      seed: M04,
      extensions: { 'agent-squire': { runtime: 'online' } },
    });
    const squire = screen.getByTestId('contact-agent-squire');
    expect(within(squire).getByText('运行中')).toBeTruthy();
    expect(within(squire).queryByText('设计组')).toBeNull();
  });
});

describe('Agent 详情的管理面板（默认无数据）', () => {
  it('预览详情没有凭据页签，也不伪造成功', async () => {
    const { user } = renderPage();
    await openDetail(user, 'agent-squire');

    expect(screen.queryByRole('tab', { name: '凭据' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Key' })).toBeNull();
    expect(screen.queryByRole('button', { name: '编辑凭据' })).toBeNull();

  });


  it('没有 Auth 清单读取端口时只显示 Key，不留空的 Auth 小节', () => {
    render(
      <CredentialsTab
        active
        actor="agent:alice:node:worker"
        expectedAgent={null}
        providerStatus={{ view: { status: 'ready', rows: [] }, reload: () => {} }}
        onNavigateConnections={() => {}}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Key' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Auth' })).toBeNull();
    expect(screen.getByText('无 Key 授权。')).toBeTruthy();
  });
});

describe('Agent 详情的管理面板（注入扩展数据）', () => {
  const SEEDED: AgentExtensionSeed = {
    'agent-squire': {
      runtime: 'offline',
      preferredHarness: 'claude',
      providerGrants: [
        {
          provider: 'anthropic',
          state: 'ok',
          source: 'user-granted',
          ownerLabel: '林懿伦',
          keyTail: 'a3f2',
          revision: 2,
        },
      ],
      capabilityGrants: [
        { id: 'grant-feishu-r2', label: '消息渠道 · 飞书', state: 'granted' },
      ],
    },
  };

  it('显示运行状态与 harness·模型', async () => {
    const { user } = renderPage({ extensions: SEEDED });
    await openDetail(user, 'agent-squire');

    expect(screen.getAllByText('未运行').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId('overview-harness')).toHaveTextContent(
      'claude',
    );
    expect(screen.queryByRole('tab', { name: '凭据' })).toBeNull();
  });

  it('recipient-pending 显示「等待原主人设备」', async () => {
    const { user } = renderPage({
      extensions: {
        'agent-squire': {
          providerGrants: [
            { provider: 'anthropic', state: 'recipient-pending', source: 'user-granted' },
          ],
        },
      },
    });
    await openDetail(user, 'agent-squire');
    expect(
      screen.getAllByText('等待原主人设备').length,
    ).toBeGreaterThanOrEqual(1);
  });

  it('unseal-failed 显示「解封失败」；未授权不是未登录证据，无登录数据时不给链接', async () => {
    const { user } = renderPage({
      extensions: {
        'agent-squire': {
          providerGrants: [{ provider: 'anthropic', state: 'unseal-failed', source: 'user-granted' }],
        },
      },
    });
    await openDetail(user, 'agent-squire');
    expect(
      screen.getAllByText('解封失败').length,
    ).toBeGreaterThanOrEqual(1);
    // unseal-failed 不是 missing/unknown，不给绑定引导。
    expect(
      screen.queryByRole('button', { name: '去连接页登录' }),
    ).toBeNull();
  });
});

describe('创建表单对齐 agent create 字段', () => {
  function createdGateway(): {
    gateway: AgentMutationGateway;
    drafts: AgentDraft[];
  } {
    const drafts: AgentDraft[] = [];
    const gateway: AgentMutationGateway = {
      create: async (draft) => {
        drafts.push(draft);
        return {
          kind: 'created',
          agent: buildManagedAgent(
            {
              id: 'agent-new',
              displayName: draft.displayName,
              role: draft.role,
              avatarTone: 'lilac',
              modelServiceId: draft.modelServiceId,
              modelLabel: '主模型服务',
              capabilities: [],
            },
            M04.connections,
            M04.organizations,
          ),
        };
      },
      update: async () => ({ kind: 'failed', message: 'unused' }),
      delete: async () => ({ kind: 'failed', message: 'unused' }),
    };
    return { gateway, drafts };
  }

  it('fixture 表单把职责、服务商与默认 harness 带进 draft', async () => {
    const { gateway, drafts } = createdGateway();
    const { user } = renderPage({ seed: M04, gateway });

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await user.type(screen.getByLabelText('名称'), '研究助理');
    await user.type(screen.getByLabelText('职责'), '研究');
    await user.type(screen.getByLabelText('服务商'), 'anthropic');
    await user.click(screen.getByLabelText('默认 harness'));
    await user.click(screen.getByRole('option', { name: 'claude' }));
    await user.click(lastCreateButton());

    expect(drafts.length).toBe(1);
    expect(drafts[0].provider).toBe('anthropic');
    expect(drafts[0].preferredHarness).toBe('claude');
    expect(drafts[0].model).toBeNull();
    expect(drafts[0].role).toBe('研究');
  });

  it('缺少职责时阻止创建，服务商与模型可留空', async () => {
    const { gateway, drafts } = createdGateway();
    const { user } = renderPage({ seed: M04, gateway });

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await user.type(screen.getByLabelText('名称'), '极简助理');
    await user.click(lastCreateButton());
    expect(drafts.length).toBe(0);
    expect(screen.getByText('请填写 Agent 职责')).toBeTruthy();

    await user.type(screen.getByLabelText('职责'), '助理');
    await user.click(lastCreateButton());
    expect(drafts.length).toBe(1);
    expect(drafts[0].provider).toBeNull();
    expect(drafts[0].model).toBeNull();
    expect(drafts[0].preferredHarness).toBeNull();
  });
});

describe('模型服务概念已删除后的创建表单', () => {
  it('基线（无任何已连接 provider）不再出现模型服务选择器，创建不被门控', async () => {
    const { user } = renderPage();
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(screen.queryByLabelText('模型服务')).toBeNull();
    expect(
      screen.getAllByRole('button', { name: '创建 Agent' }).at(-1),
    ).toBeEnabled();
  });
});

describe('agent-extensions 视图模型', () => {
  it('缺省扩展全部落在状态未确认', () => {
    const view = extensionOf(undefined, 'agent-anyone');
    expect(view.runtime).toBe('unknown');
    expect(view.providerGrants).toEqual([]);
    expect(view.capabilityGrants).toEqual([]);
    expect(view.preset).toBe(false);
  });

  it('Squire 按 fixture id 识别为预配置', () => {
    expect(extensionOf(undefined, 'agent-squire').preset).toBe(true);
    expect(extensionOf({}, 'agent-squire').preset).toBe(true);
  });
});

describe('凭据页签不混入能力台账', () => {
  it('不渲染能力授权区，也不出现 agent secret 文案', () => {
    const { container } = render(
      <CredentialsTab
        active
        actor="agent:alice:node:worker"
        expectedAgent={null}
        providerStatus={{ view: { status: 'ready', rows: [] }, reload: () => {} }}
        onNavigateConnections={() => {}}
      />,
    );
    expect(container.querySelector('[aria-label="能力授权"]')).toBeNull();
    expect(within(container).queryByText(/agent secret/)).toBeNull();
  });
});

describe('Agent 凭据页签的 Key 切换与原生登录', () => {
  function credentialsApi(): SharedCredentialsApi {
    return {
      listKeys: async () => ([
          {
            keyId: 'anthropic-old:ANTHROPIC_API_KEY',
            provider: 'anthropic',
            label: '旧 Key',
            state: 'active',
            createdAt: 1,
            lastUsedAt: null,
            grants: [],
          },
          {
            keyId: 'anthropic-new:ANTHROPIC_API_KEY',
            provider: 'anthropic',
            label: '新 Key',
            state: 'active',
            createdAt: 2,
            lastUsedAt: null,
            grants: [],
          },
        ]),
          listSecretGrants: async () => [],
      addKey: async () => ({ status: 'failed', message: 'unused' }),
      replaceKey: async () => ({ status: 'failed', message: 'unused' }),
      deleteKey: async () => ({ status: 'failed', message: 'unused' }),
      revokeGrant: async () => ({ status: 'failed', message: 'unused' }),
    };
  }

  it('已有授权时切换 Key 保留 grantId 并递增授权 revision', async () => {
    const calls: { method: string; params: Record<string, unknown> }[] = [];
    const api = createTestAgentsApi({
      async request(method, params) {
        calls.push({ method, params });
        if (method === 'agent.secret.grant') {
          return {
            ok: true,
            grant: { ...params, actor: 'worker' },
          };
        }
        throw new Error(`unexpected method ${method}`);
      },
    });
    const reload = vi.fn();
    const user = userEvent.setup();
    render(
      <CredentialsTab
        active
        actor="agent:alice:node:worker"
        preferredHarness="claude"
        expectedAgent={null}
        providerStatus={{
          view: {
            status: 'ready',
            rows: [{
              grantId: 'anthropic',
              provider: 'anthropic',
              state: 'granted',
              code: null,
              owner: null,
              keyId: 'anthropic-old:ANTHROPIC_API_KEY',
              revision: 4,
              expiresAtMs: null,
              unsealable: false,
            }],
          },
          reload,
        }}
        agentsApi={api}
        credentialsApi={credentialsApi()}
        onNavigateConnections={() => {}}
      />,
    );

    expect(await screen.findByText('旧 Key（anthropic）')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    await user.click(screen.getByLabelText('Key'));
    await user.click(await screen.findByRole('option', { name: /新 Key/ }));
    await user.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => {
      expect(calls).toEqual([
        {
          method: 'agent.secret.grant',
          params: {
            actor: 'agent:alice:node:worker',
            entryId: 'anthropic-new',
            fieldName: 'ANTHROPIC_API_KEY',
            source: 'user-provider',
            grantId: 'anthropic',
            revision: 5,
            environmentNames: ['ANTHROPIC_API_KEY'],
          },
        },
      ]);
    });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑凭据' })).toBeNull());
    expect(screen.queryByText('凭据已保存。')).toBeNull();
    expect(reload).toHaveBeenCalled();
  });

  it('在编辑凭据里写入原生登录文件，不宣称已登录', async () => {
    const calls: { method: string; params: Record<string, unknown> }[] = [];
    const api = createTestAgentsApi({
      async request(method, params) {
        calls.push({ method, params });
        if (method === 'agent.provider.login') {
          return {
            ok: true,
            actor: 'agent:alice:node:worker',
            provider: 'claude',
            files: [{
              path: '/agent/.credentials.json',
              bytes: 3,
              replaced: false,
              unchanged: false,
            }],
          };
        }
        if (method === 'agent.provider.logout') {
          return {
            ok: true,
            actor: 'agent:alice:node:worker',
            provider: 'claude',
            removed: ['.credentials.json'],
            absent: [],
          };
        }
        throw new Error(`unexpected method ${method}`);
      },
    });
    const user = userEvent.setup();
    render(
      <CredentialsTab
        active
        actor="agent:alice:node:worker"
        preferredHarness="claude"
        expectedAgent={null}
        providerStatus={{ view: { status: 'ready', rows: [] }, reload: () => {} }}
        agentsApi={api}
        nativeLoginFiles={async () => [
          { filename: '.credentials.json', contentBase64: 'YWJj' },
        ]}
        onNavigateConnections={() => {}}
      />,
    );

    expect(screen.queryByText('暂无 Auth 状态查询。')).toBeNull();
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    await user.click(screen.getByRole('button', { name: '选择登录文件' }));
    await user.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      method: 'agent.provider.login',
      params: {
        actor: 'agent:alice:node:worker',
        provider: 'claude',
        files: [{ filename: '.credentials.json', contentBase64: 'YWJj' }],
      },
    });
  });
});

describe('运行段的运行时事实', () => {
  it('预览详情只有概览与所属组织页签，没有运行页签', async () => {
    const { user } = renderPage({ seed: M04 });
    await user.click(screen.getByTestId('contact-agent-squire'));
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['概览', '所属组织']);
    expect(screen.queryByRole('tab', { name: '运行' })).toBeNull();
  });


});
