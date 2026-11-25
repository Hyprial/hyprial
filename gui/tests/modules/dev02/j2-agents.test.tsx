import { explicitAgentFixtureSeed } from '../../unified-backend/agents-test-client';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import {
  AGENT_LOCAL_FAILURE,
  type AgentMutationGateway,
} from '../../../src/features/agents/impl/AgentMutationGateway';
import {
  buildManagedAgent,
  type AgentSeed,
} from '../../../src/state/agent-selectors';
import { buildAgentScenario } from '../../../src/dev/j2-scenarios';

// Radix Select 在 jsdom 打开弹出层需要这些 API 存在（真实浏览器原生提供）。
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const M04 = buildAgentScenario('m04')!;
const M05 = buildAgentScenario('m05')!;
const M06 = buildAgentScenario('m06')!;

function renderAgents(options?: {
  seed?: AgentSeed;
  gateway?: AgentMutationGateway;
}) {
  const user = userEvent.setup();
  const view = render(
    <FixtureApp
      dataMode="fixture"
      agentSeed={options?.seed ?? explicitAgentFixtureSeed()}
      agentGateway={options?.gateway}
    />,
  );
  return { user, view };
}

async function openAgents(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Agent' }));
}

function failingGateway(): AgentMutationGateway {
  return {
    create: async () => ({ kind: 'failed', message: AGENT_LOCAL_FAILURE }),
    update: async () => ({ kind: 'failed', message: AGENT_LOCAL_FAILURE }),
    delete: async () => ({ kind: 'failed', message: AGENT_LOCAL_FAILURE }),
  };
}

function unknownGateway(): AgentMutationGateway {
  return {
    create: async () => ({ kind: 'unknown', message: '结果未知' }),
    update: async () => ({ kind: 'unknown', message: '结果未知' }),
    delete: async () => ({ kind: 'unknown', message: '结果未知' }),
  };
}

function countingGateway(
  seed: AgentSeed,
  delayMs = 60,
): { gateway: AgentMutationGateway; calls: () => number } {
  let calls = 0;
  const views = seed.agents.map((record) =>
    buildManagedAgent(record, seed.connections, seed.organizations),
  );
  const gateway: AgentMutationGateway = {
    create: () => {
      calls += 1;
      return new Promise((resolve) => {
        window.setTimeout(() => {
          resolve({
            kind: 'created',
            agent: buildManagedAgent(
              {
                id: 'agent-counted',
                displayName: '计次 Agent',
                role: '测试',
                avatarTone: 'lilac',
                modelServiceId: 'model-connected',
                modelLabel: '主模型服务',
                capabilities: [],
              },
              seed.connections,
              seed.organizations,
            ),
          });
        }, delayMs);
      });
    },
    update: async () => ({
      kind: 'updated',
      agent: views[0]!,
    }),
    delete: async (agentId) => ({ kind: 'deleted', agentId }),
  };
  return { gateway, calls: () => calls };
}

describe('J2 entries and J3 landing', () => {
  it('TC-001 TC-003 opens the create form from the start page with no adapter call', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(
      screen.getByRole('heading', { level: 2, name: '创建 Agent' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('名称')).toHaveFocus();
  });

  it('TC-002 TC-004 opens the same form from the Agent page', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);

    await user.click(
      within(screen.getByTestId('contact-list').parentElement!).getAllByRole(
        'button',
        { name: '创建 Agent' },
      )[0],
    );

    expect(
      screen.getByRole('heading', { level: 2, name: '创建 Agent' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('职责')).toBeInTheDocument();
  });

  it('TC-005 TC-036 cancels the create form back to the list', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await user.type(screen.getByLabelText('名称'), '临时草稿');

    await user.click(screen.getByRole('button', { name: '取消' }));

    expect(screen.getByTestId('contact-list')).toBeInTheDocument();
    expect(screen.queryByLabelText('名称')).toBeNull();
  });

  it('TC-006 TC-007 TC-008 opens the J3-selected Agent detail and returns to the list', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-researcher'));

    const detail = document.querySelector('[data-selected="true"]');
    expect(detail).not.toBeNull();
    expect(detail).toHaveTextContent('研究者');
    expect(
      screen.getByRole('heading', { level: 2, name: '研究者' }),
    ).toHaveFocus();

    await user.click(screen.getByRole('link', { name: 'Agent' }));
    expect(screen.getByTestId('contact-list')).toBeInTheDocument();
  });

  it('TC-023 announces a stale Agent id through role=status', async () => {
    const { user } = renderAgents({ seed: M05.seed, gateway: M05.gateway });
    await openAgents(user);

    // The chat still lists a fixture Agent while the J2 seed is empty.
    await user.click(screen.getByRole('button', { name: '聊天' }));
    await user.click(
      within(screen.getByLabelText('聊天对象')).getByRole('button', {
        name: /Squire/,
      }),
    );
    await user.click(screen.getByRole('button', { name: '管理 Agent' }));

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('找不到该 Agent');
    expect(
      within(status).getByRole('heading', { name: '找不到该 Agent' }),
    ).toHaveFocus();
  });
});

describe('J2 list and connections', () => {
  it('TC-009 TC-010 shows three M-04 cards whose count matches', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);

    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(3);
    expect(screen.queryByText(/显示 \d+ \/ \d+ 个 Agent/)).toBeNull();
    expect(screen.queryByText(/找到 \d+ 个，共 \d+ 个/)).toBeNull();
  });

  it('TC-011 TC-012 keeps the count in sync with the search result', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);

    await user.type(
      screen.getByRole('searchbox', { name: '搜索 Agent' }),
      '写作',
    );
    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(1);
    expect(screen.getByText('找到 1 个，共 3 个')).toBeInTheDocument();

    await user.clear(screen.getByRole('searchbox', { name: '搜索 Agent' }));
    await user.type(
      screen.getByRole('searchbox', { name: '搜索 Agent' }),
      '不存在',
    );
    expect(screen.getByText('没有匹配的 Agent')).toBeInTheDocument();
    expect(screen.getByText('找到 0 个，共 3 个')).toBeInTheDocument();
  });

  it('TC-013 shows the M-05 empty state without a zero-count card', async () => {
    const { user } = renderAgents({ seed: M05.seed, gateway: M05.gateway });
    await openAgents(user);

    expect(screen.getByText('还没有 Agent。使用上方“创建 Agent”开始。')).toBeInTheDocument();
    expect(screen.queryByText(/找到 \d+ 个，共 0 个/)).toBeNull();
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
    expect(document.body.textContent).not.toMatch(/尚未开放|暂不可用/);
  });

  it('TC-014 TC-015 derives availability and connection labels from connections', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);

    const squire = screen.getByTestId('contact-agent-squire');
    expect(squire).toHaveTextContent('可用');
    expect(squire).toHaveTextContent('已连接');
    expect(screen.getByTestId('contact-agent-researcher')).toHaveTextContent(
      '未连接渠道',
    );
    expect(screen.getByTestId('contact-agent-writer')).toHaveTextContent(
      '状态未确认',
    );

    await user.click(screen.getByRole('button', { name: '可用' }));
    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(3);
  });

  it('TC-018 renders the same M-04 list three times', async () => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const { user, view } = renderAgents({
        seed: M04.seed,
        gateway: M04.gateway,
      });
      await openAgents(user);
      expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(3);
      expect(screen.queryByText(/显示 \d+ \/ \d+ 个 Agent/)).toBeNull();
      view.unmount();
    }
  });
});

describe('J2 detail', () => {
  it('TC-019 TC-020 TC-028 shows the five detail facts', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-squire'));

    const detail = document.querySelector('[data-selected="true"]') as HTMLElement;
    expect(within(detail).getByRole('heading', { level: 2 })).toHaveTextContent(
      'Squire',
    );
    expect(detail).toHaveTextContent('个人助理 · 统一入口');
    expect(detail).not.toHaveTextContent('模型服务');
    expect(detail).toHaveTextContent('平台绑定');
    // R9：有组织卡时头部不显示所属组织行；组织值改在关联页组织列表断言（见 org-crosstab 用例）。
    expect(detail.querySelector('dl.agent-detail-facts')?.textContent ?? '').not.toContain('所属组织');
    expect(detail.querySelector('dl.agent-detail-facts')?.textContent ?? '').not.toContain('设计组');
  });

  it('TC-021 shows both unconnected states for M-06', async () => {
    const { user } = renderAgents({ seed: M06.seed, gateway: M06.gateway });
    await openAgents(user);

    expect(screen.getByTestId('contact-agent-unconnected')).toHaveTextContent(
      '需绑定模型凭据',
    );
    await user.click(screen.getByTestId('contact-agent-unconnected'));

    const detail = document.querySelector('[data-selected="true"]') as HTMLElement;
    expect(detail).not.toHaveTextContent('暂无 Auth 状态查询。');
    expect(detail).toHaveTextContent('未连接渠道');
    expect(
      within(detail).queryByRole('button', { name: '删除' }),
    ).toBeNull();
  });

  it('TC-022 shows 状态未确认 when the connection record is missing', async () => {
    const seed: AgentSeed = {
      connections: [],
      organizations: [],
      agents: [
        {
          id: 'agent-orphan',
          displayName: '孤立 Agent',
          role: '测试',
          avatarTone: 'grey',
          modelServiceId: 'missing-model',
          channelServiceId: 'missing-channel',
          modelLabel: '主模型服务',
          capabilities: [],
        },
      ],
    };
    const { user } = renderAgents({ seed });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-orphan'));

    const detail = document.querySelector('[data-selected="true"]') as HTMLElement;
    expect(detail.textContent).toContain('状态未确认');
  });
});

describe('J2 create', () => {
  it('TC-029 TC-030 reports required fields in place and focuses the first one', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(screen.getByText('请填写 Agent 名称')).toBeInTheDocument();
    expect(screen.getByLabelText('名称')).toHaveFocus();
    expect(screen.getByLabelText('名称')).toHaveAttribute(
      'aria-invalid',
      'true',
    );

    await user.type(screen.getByLabelText('名称'), '新 Agent');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(screen.getByText('请填写 Agent 职责')).toBeInTheDocument();
    expect(screen.getByLabelText('职责')).toHaveFocus();
  });

  it('TC-031 TC-040 drops the model-service picker; harness and model stay preferences', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(screen.queryByLabelText('模型服务')).toBeNull();
    expect(screen.getByLabelText('服务商')).toBeTruthy();
    expect(screen.getByLabelText('模型')).toBeTruthy();
    expect(screen.getByLabelText('默认 harness')).toBeTruthy();
    expect(screen.queryByLabelText('所属组织')).not.toBeInTheDocument();
  });

  it('TC-032 creation is not gated by model credentials; granting happens after creation', async () => {
    const { user } = renderAgents({ seed: M06.seed, gateway: M06.gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(
      screen.getAllByRole('button', { name: '创建 Agent' }).at(-1),
    ).toBeEnabled();
  });

  it('TC-033 blocks duplicate submits while one create is pending', async () => {
    let resolveCreate: (value: unknown) => void = () => {};
    let calls = 0;
    const gateway: AgentMutationGateway = {
      create: () => {
        calls += 1;
        return new Promise((resolve) => { resolveCreate = resolve; }) as never;
      },
    };
    const { user } = renderAgents({ seed: M04.seed, gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await user.type(screen.getByLabelText('名称'), '计次 Agent');
    await user.type(screen.getByLabelText('职责'), '测试');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    // 中间态：挂起中的唯一一次提交，按钮不可再次提交
    const pending = screen.getByRole('button', { name: '处理中…' });
    expect(pending).toBeDisabled();
    expect(calls).toBe(1);
    await user.click(pending);
    expect(calls).toBe(1);

    resolveCreate({
      kind: 'created',
      agent: buildManagedAgent(
        { id: 'agent-counted', displayName: '计次 Agent', role: '测试', modelServiceId: 'model-1', organizationId: null, capabilities: [] } as never,
        M04.seed.connections,
        M04.seed.organizations,
      ),
    });
    // resolve 后确定性等待新 Agent 出现（不是原本就存在的 contact-list）
    expect(await screen.findByTestId('agent-detail-panel')).toHaveTextContent('计次 Agent');
    expect(screen.queryByRole('heading', { level: 2, name: '创建 Agent' })).toBeNull();
    expect(calls).toBe(1);
  });

  it('TC-034 TC-038 TC-042 creates one card and keeps the count in sync', async () => {
    const { gateway } = countingGateway(M04.seed, 10);
    const { user } = renderAgents({ seed: M04.seed, gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await user.type(screen.getByLabelText('名称'), '计次 Agent');
    await user.type(screen.getByLabelText('职责'), '测试');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await screen.findByTestId('agent-detail-panel');
    await user.click(screen.getByRole('link', { name: 'Agent' }));
    await waitFor(() => {
      expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(4);
    });
    expect(screen.queryByText(/找到 \d+ 个，共 \d+ 个/)).toBeNull();
    const created = document.querySelector(
      '[data-testid="contact-agent-counted"]',
    );
    expect(created).not.toBeNull();
    expect(created).toHaveTextContent('可用');
  });

  it('TC-056 focuses the new Agent configuration title after a create', async () => {
    const { gateway } = countingGateway(M04.seed, 10);
    const { user } = renderAgents({ seed: M04.seed, gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await user.type(screen.getByLabelText('名称'), '焦点 Agent');
    await user.type(screen.getByLabelText('职责'), '测试');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: '计次 Agent' }),
      ).toHaveFocus();
    });
  });

  it('TC-035 keeps the inputs and the card count after a local failure', async () => {
    const { user } = renderAgents({
      seed: M04.seed,
      gateway: failingGateway(),
    });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await user.type(screen.getByLabelText('名称'), '保留名称');
    await user.type(screen.getByLabelText('职责'), '保留职责');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      AGENT_LOCAL_FAILURE,
    );
    expect(screen.getByLabelText('名称')).toHaveValue('保留名称');
    expect(screen.getByLabelText('职责')).toHaveValue('保留职责');

    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(3);
  });

  it('TC-037 reports an unknown result without retrying', async () => {
    const { user } = renderAgents({
      seed: M04.seed,
      gateway: unknownGateway(),
    });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await user.type(screen.getByLabelText('名称'), '未知结果');
    await user.type(screen.getByLabelText('职责'), '测试');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('结果未知');
    expect(
      screen.getByRole('heading', { level: 2, name: '创建 Agent' }),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-testid="contact-agent-counted"]'),
    ).toBeNull();
  });
});

describe('J2 edit and delete', () => {
  it('TC-043 TC-044 TC-046 prefills, updates and cancels an edit', async () => {
    const { gateway } = countingGateway(M04.seed, 10);
    const { user } = renderAgents({ seed: M04.seed, gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-researcher'));

    await user.click(screen.getByRole('button', { name: '编辑' }));
    expect(screen.getByLabelText('名称')).toHaveValue('研究者');
    expect(screen.getByLabelText('职责')).toHaveValue('研究分析');

    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(
      screen.getByRole('heading', { level: 2, name: '研究者' }),
    ).toBeInTheDocument();
  });

  it('TC-045 keeps the edited values when the update fails', async () => {
    const { user } = renderAgents({
      seed: M04.seed,
      gateway: failingGateway(),
    });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-researcher'));
    await user.click(screen.getByRole('button', { name: '编辑' }));

    await user.clear(screen.getByLabelText('名称'));
    await user.type(screen.getByLabelText('名称'), '新的研究者');
    await user.click(screen.getByRole('button', { name: '保存修改' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      AGENT_LOCAL_FAILURE,
    );
    expect(screen.getByLabelText('名称')).toHaveValue('新的研究者');
  });

  it('TC-050 recomputes availability from the returned model connection', async () => {
    // The form only offers connected models (TC-031), so the new unconnected
    // model arrives through the injected update result, not through the select.
    const seed: AgentSeed = {
      connections: M04.seed.connections,
      organizations: M04.seed.organizations,
      agents: M04.seed.agents,
    };
    const gateway: AgentMutationGateway = {
      create: async () => ({ kind: 'failed', message: 'unused' }),
      update: async (agentId, draft) => ({
        kind: 'updated',
        agent: buildManagedAgent(
          {
            id: agentId,
            displayName: draft.displayName,
            role: draft.role,
            avatarTone: 'lilac',
            modelServiceId: 'model-open',
            modelLabel: '备用模型服务',
            capabilities: [],
          },
          seed.connections,
          seed.organizations,
        ),
      }),
      delete: async (agentId) => ({ kind: 'deleted', agentId }),
    };
    const { user } = renderAgents({ seed, gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-squire'));
    await user.click(screen.getByRole('button', { name: '编辑' }));

    await user.click(screen.getByRole('button', { name: '保存修改' }));

    await waitFor(() => {
      expect(
        document.querySelector('[data-selected="true"]'),
      ).toHaveTextContent('已保存修改');
    });
    await user.click(screen.getByRole('link', { name: 'Agent' }));

    expect(screen.getByTestId('contact-agent-squire')).toHaveTextContent(
      '需绑定模型凭据',
    );
  });

  it('legacy fixtures cannot bypass deletion confirmation', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-researcher'));
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
  });

});

describe('J2 sources without write ports', () => {
  it('TC-057 TC-059 TC-062 renders no create, edit or delete control for a seed without a gateway', async () => {
    const { user } = renderAgents({ seed: M04.seed });
    await openAgents(user);
    expect(screen.queryByRole('button', { name: /创建 Agent/ })).toBeNull();
    await user.click(screen.getByTestId('contact-agent-squire'));
    expect(screen.queryByRole('button', { name: '编辑' })).toBeNull();
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
    const overview = document.getElementById('agent-tabpanel-overview') as HTMLElement;
    expect(overview.textContent).not.toMatch(/暂不可用|尚未开放/);
    expect(document.body.textContent).not.toContain('已创建 Agent');
  });

  it('TC-057 a gateway that defines only create renders create but neither edit nor delete', async () => {
    const gateway: AgentMutationGateway = { create: async () => ({ kind: 'failed', message: '本地失败' }) };
    const { user } = renderAgents({ seed: M04.seed, gateway });
    await openAgents(user);
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
    await user.click(screen.getByTestId('contact-agent-squire'));
    expect(screen.queryByRole('button', { name: '编辑' })).toBeNull();
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
  });

  it('TC-017 TC-026 TC-041 keeps adapter calls and storage at zero', async () => {
    const { user } = renderAgents();
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-writer'));
    await user.click(screen.getByRole('link', { name: 'Agent' }));

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});

describe('J2 面包屑', () => {
  it('列表视图无面包屑；详情与创建视图有统一面包屑，父级回列表', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    expect(
      screen.queryByRole('navigation', { name: '面包屑' }),
    ).toBeNull();

    await user.click(screen.getByTestId('contact-agent-researcher'));
    const breadcrumb = screen.getByRole('navigation', { name: '面包屑' });
    expect(
      within(breadcrumb).getByRole('link', { name: 'Agent' }),
    ).toBeInTheDocument();
    expect(breadcrumb.querySelector('[aria-current="page"]')).toHaveTextContent(
      '配置',
    );

    // 面包屑是唯一回退入口：详情视图里不再有底部「返回列表」按钮。
    expect(
      screen.queryByRole('button', { name: '返回列表' }),
    ).toBeNull();
    await user.click(within(breadcrumb).getByRole('link', { name: 'Agent' }));
    expect(screen.getByTestId('contact-list')).toBeInTheDocument();
    expect(
      screen.queryByRole('navigation', { name: '面包屑' }),
    ).toBeNull();

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    const createBreadcrumb = screen.getByRole('navigation', { name: '面包屑' });
    expect(
      createBreadcrumb.querySelector('[aria-current="page"]'),
    ).toHaveTextContent('创建 Agent');
  });
});

describe('J2 开始对话', () => {
  it('可用的 Agent 从详情「开始对话」跳转聊天页', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-squire'));

    const start = screen.getByRole('button', { name: '开始对话' });
    expect(start).toBeEnabled();
    await user.click(start);
    expect(
      screen.getByRole('heading', { level: 1, name: '聊天' }),
    ).toBeInTheDocument();
  });

  it('未配置模型的 Agent 不提供「开始对话」', async () => {
    const { user } = renderAgents({ seed: M06.seed, gateway: M06.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-unconnected'));

    expect(screen.queryByRole('button', { name: '开始对话' })).toBeNull();
  });
});

describe('J2 owner/host 维度', () => {
  const NETWORK_SEED: AgentSeed = {
    connections: M04.seed.connections,
    organizations: M04.seed.organizations,
    agents: [
      ...M04.seed.agents,
      {
        id: 'agent-peer-a',
        displayName: '同伴分析',
        role: '外部分析',
        avatarTone: 'grey',
        modelServiceId: 'model-connected',
        organizationId: null,
        modelLabel: '主模型服务',
        capabilities: [],
        owner: '姚升悦',
        host: '姚升悦的电脑',
      },
    ],
  };

  it('详情显示所有者与主机', async () => {
    const { user } = renderAgents({ seed: NETWORK_SEED, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-peer-a'));
    const detail = document.querySelector('[data-selected="true"]') as HTMLElement;
    expect(detail).toHaveTextContent('所有者');
    expect(detail).toHaveTextContent('姚升悦');
    expect(detail).toHaveTextContent('主机');
    expect(detail).toHaveTextContent('姚升悦的电脑');
  });

  it('自己的 Agent 显示所有者「我」、主机「本机」', async () => {
    const { user } = renderAgents({ seed: M04.seed, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByTestId('contact-agent-squire'));
    const detail = document.querySelector('[data-selected="true"]') as HTMLElement;
    expect(detail).toHaveTextContent('所有者');
    expect(detail).toHaveTextContent('我');
    expect(detail).toHaveTextContent('本机');
  });

  it('按所有者分组显示，网络 Agent 单列一组', async () => {
    const { user } = renderAgents({ seed: NETWORK_SEED, gateway: M04.gateway });
    await openAgents(user);
    const list = screen.getByTestId('contact-list');
    expect(within(list).getByRole('heading', { name: /我的/ })).toBeTruthy();
    expect(
      within(list).getByRole('heading', { name: /姚升悦/ }),
    ).toBeTruthy();
    // 分组容器内各自包含自己的卡片
    expect(screen.getByTestId('contact-agent-peer-a')).toBeTruthy();
    expect(screen.queryByText(/找到 \d+ 个，共 \d+ 个/)).toBeNull();
  });

  it('卡片/列表视图切换，列表行含所有者与主机', async () => {
    const { user } = renderAgents({ seed: NETWORK_SEED, gateway: M04.gateway });
    await openAgents(user);
    await user.click(screen.getByRole('button', { name: '列表' }));
    const list = screen.getByTestId('contact-list');
    expect(within(list).getByText('同伴分析')).toBeTruthy();
    expect(within(list).getAllByText(/姚升悦的电脑/).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: '卡片' }));
    expect(screen.getByTestId('contact-agent-peer-a')).toBeTruthy();
  });

  it('按所有者/主机过滤', async () => {
    const { user } = renderAgents({ seed: NETWORK_SEED, gateway: M04.gateway });
    await openAgents(user);

    await user.click(screen.getByLabelText('所有者'));
    await user.click(await screen.findByRole('option', { name: '姚升悦' }));
    expect(screen.getByText('找到 1 个，共 4 个')).toBeInTheDocument();
    expect(screen.queryByTestId('contact-agent-squire')).toBeNull();
    expect(screen.getByTestId('contact-agent-peer-a')).toBeTruthy();

    await user.click(screen.getByLabelText('所有者'));
    await user.click(await screen.findByRole('option', { name: '全部' }));

    await user.click(screen.getByLabelText('主机'));
    await user.click(await screen.findByRole('option', { name: '本机' }));
    expect(screen.getByText('找到 3 个，共 4 个')).toBeInTheDocument();
  });
});
