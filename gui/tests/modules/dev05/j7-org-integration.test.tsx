import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { buildOrgViewModel } from '../../../src/pages/org/org-view-model';
import { buildMachineScenario } from '../../../src/dev/machine-scenarios';
import {
  J7_OBSERVED_AT,
  buildTopologyScenario,
} from '../../../src/dev/topology-scenarios';
import {
  buildHiddenTopologyModel,
} from '../../../src/features/network/impl/topology-view-model';

function orgModel() {
  return buildOrgViewModel('member-joined');
}

function machineModel() {
  const model = buildMachineScenario('m14');
  if (!model) {
    throw new Error('missing m14');
  }
  return model;
}

function topologyModel(scenario: 'm16' | 'm17') {
  const model = buildTopologyScenario(scenario, {
    members: orgModel().members,
    machines: machineModel().machines,
  });
  if (!model) {
    throw new Error(`missing topology scenario ${scenario}`);
  }
  return model;
}

function renderOrg(options?: {
  orgScene?: 'member-joined' | 'join-failed' | 'invite-pending';
  topology?: ReturnType<typeof topologyModel>;
}) {
  const user = userEvent.setup();
  const view = render(
    <FixtureApp
      dataMode="fixture"
      orgModel={buildOrgViewModel(options?.orgScene ?? 'member-joined')}
      machineModel={machineModel()}
      topologyModel={options?.topology}
    />,
  );
  return { user, view };
}

async function openOrg(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '组织' }));
  await user.click(screen.getByRole('tab', { name: '拓扑' }));
}

function topologySection(): HTMLElement | null {
  const section = document.querySelector('.org-topology');
  return section instanceof HTMLElement ? section : null;
}

describe('J7 integration: mounting and product defaults', () => {
  it('TC-001 mounts the topology section after the machine section', async () => {
    const { user } = renderOrg({ topology: topologyModel('m17') });
    await openOrg(user);

    expect(topologySection()).not.toBeNull();
    await user.click(screen.getByRole('tab', { name: '成员' }));
    await user.click(screen.getByRole('tab', { name: '设备' }));
    expect(document.querySelector('.org-machines')).not.toBeNull();
    expect(topologySection()).toBeNull();
  });

  it('TC-002 renders no J7 section and no unavailable note without a model', async () => {
    const { user } = renderOrg();
    await openOrg(user);

    expect(topologySection()).toBeNull();
    expect(document.querySelector('[data-org-section="topology"]')).not.toBeNull();
    expect(document.body).not.toHaveTextContent('拓扑数据暂不可用');
  });

  it('TC-003 hides the section on a failed join and keeps the J5 failure', async () => {
    const { user } = renderOrg({ orgScene: 'join-failed', topology: topologyModel('m17') });
    await openOrg(user);

    expect(topologySection()).toBeNull();
    expect(document.body).not.toHaveTextContent('studio-mac');
    await user.click(screen.getByRole('tab', { name: '邀请' }));
    await user.click(screen.getByRole('tab', { name: '用邀请链接加入' }));
    expect(screen.getByLabelText(/^邀请链接/, { selector: 'input' })).toBeInTheDocument();
  });

  it('TC-004 keeps the J5 invitation state while the topology renders', async () => {
    const { user } = renderOrg({ orgScene: 'invite-pending', topology: topologyModel('m16') });
    await openOrg(user);

    expect(screen.getByRole('tab', { name: '邀请' })).toBeInTheDocument();
    expect(topologySection()).not.toBeNull();
  });
});

describe('J7 integration: M-16 and M-17', () => {
  it('TC-005 derives one node per member and machine for M-16', async () => {
    const { user } = renderOrg({ topology: topologyModel('m16') });
    await openOrg(user);

    const section = topologySection() as HTMLElement;
    expect(section).toHaveAttribute('data-topology-mode', 'structure-only');
    const nodes = section.querySelectorAll('[data-topology-node]');
    expect(nodes).toHaveLength(3);
    expect(section.textContent).toContain('新成员一');
    expect(section.textContent).toContain('studio-mac');
    expect(section.textContent).toContain('travel-mac');

    expect(within(section).queryByRole('button', { name: '通信视图' })).toBeNull();
    expect(within(section).queryByRole('group', { name: '拓扑视图' })).toBeNull();
    expect(within(section).queryByRole('button', { name: /Monitor/ })).toBeNull();
    expect(section).not.toHaveTextContent('不可用');
    expect(section.querySelectorAll('[data-topology-edge]')).toHaveLength(0);
  });

  it('TC-006 opens Inspect for the M-17 edge with four fields and no endpoint', async () => {
    const { user } = renderOrg({ topology: topologyModel('m17') });
    await openOrg(user);

    const section = topologySection() as HTMLElement;
    await user.click(within(section).getByRole('button', { name: '通信视图' }));
    const edge = section.querySelector('[data-topology-edge]') as HTMLElement;
    expect(edge).not.toBeNull();
    await user.click(edge);

    const panel = await screen.findByRole('dialog', { name: /Inspect/ });
    expect(panel).toHaveTextContent('发起方');
    expect(panel).toHaveTextContent('接收方');
    expect(panel).toHaveTextContent('状态');
    expect(panel).toHaveTextContent('时间');
    expect(panel).toHaveTextContent(J7_OBSERVED_AT);
    expect(panel.textContent).not.toMatch(/https?:\/\/|ws:\/\//);

    await user.click(
      within(panel).getByRole('button', { name: '关闭 Inspect' }),
    );
    expect(screen.queryByRole('dialog', { name: /Inspect/ })).toBeNull();
  });

  it('TC-007 shows the static Monitor note without timers or requests', async () => {
    const { user } = renderOrg({ topology: topologyModel('m17') });
    await openOrg(user);

    const section = topologySection() as HTMLElement;
    await user.click(within(section).getByRole('button', { name: 'Monitor' }));

    const panel = await screen.findByRole('dialog', { name: '最近通信记录' });
    expect(panel).toHaveTextContent('最近通信记录');
    expect(panel).toHaveTextContent('发起方');
    expect(panel).toHaveTextContent('接收方');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);

    await user.click(
      within(panel).getByRole('button', { name: '关闭 Monitor' }),
    );
    expect(screen.queryByRole('dialog', { name: '最近通信记录' })).toBeNull();
  });

  it('TC-008 retries an unconfirmed model without claiming success', async () => {
    const { user } = renderOrg();
    await openOrg(user);

    const hidden = buildHiddenTopologyModel();
    expect(hidden.dataMode).toBe('hidden');
  });
});
