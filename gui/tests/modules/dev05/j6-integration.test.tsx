import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { buildOrgViewModel } from '../../../src/pages/org/org-view-model';
import {
  buildHiddenMachineSectionModel,
} from '../../../src/features/network/impl/machine-view-model';
import { buildMachineScenario } from '../../../src/dev/machine-scenarios';

function m14() {
  const model = buildMachineScenario('m14');
  if (!model) {
    throw new Error('missing m14');
  }
  return model;
}

function m15() {
  const model = buildMachineScenario('m15');
  if (!model) {
    throw new Error('missing m15');
  }
  return model;
}

function renderOrgPage(options?: {
  orgScene?: 'created-empty' | 'invite-pending' | 'join-failed' | 'member-joined' | 'unavailable';
  machineModel?: ReturnType<typeof m14>;
}) {
  const user = userEvent.setup();
  const view = render(
    <FixtureApp
      dataMode="fixture"
      orgModel={buildOrgViewModel(options?.orgScene ?? 'member-joined')}
      machineModel={options?.machineModel}
    />,
  );
  return { user, view };
}

async function openOrg(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '组织' }));
  await user.click(screen.getByRole('tab', { name: '成员' }));
  await user.click(screen.getByRole('tab', { name: '设备' }));
}

function machineSection(): HTMLElement | null {
  const card = document.querySelector('.org-machines');
  return card instanceof HTMLElement ? card : null;
}

describe('J6 integration: mounting and scenes', () => {
  it('TC-027 TC-035 mounts the machine section after the member section', async () => {
    const { user } = renderOrgPage({ orgScene: 'member-joined', machineModel: m14() });
    await openOrg(user);

    expect(machineSection()).not.toBeNull();
    await user.click(screen.getByRole('tab', { name: '成员' }));
    expect(screen.getByText('新成员一')).toBeInTheDocument();
    expect(machineSection()).toBeNull();
  });

  it('TC-030 shows no J6 model and no unavailable note without a machine model', async () => {
    const { user } = renderOrgPage({ orgScene: 'member-joined' });
    await openOrg(user);

    expect(machineSection()).toBeNull();
    const section = document.querySelector('[data-org-section="machines"]');
    expect(section).not.toBeNull();
    expect(document.body).not.toHaveTextContent('机器网络暂不可用');
    expect(within(section as HTMLElement).queryByRole('list')).toBeNull();
  });

  it('TC-031 shows two M-14 machines with the current one marked', async () => {
    const { user } = renderOrgPage({ orgScene: 'member-joined', machineModel: m14() });
    await openOrg(user);

    const section = machineSection() as HTMLElement;
    const rows = within(section).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('studio-mac');
    expect(rows[0]).toHaveTextContent('这台电脑');
    expect(rows[0]).toHaveTextContent('在线');
    expect(rows[0]).toHaveTextContent('macOS 合成系统');
    expect(rows[0]).toHaveTextContent('2026-10-20 09:30');
    expect(rows[1]).toHaveTextContent('travel-mac');
    expect(within(section).queryByRole('button', { name: /移除|删除/ })).toBeNull();
  });

  it('TC-032 shows the empty state for M-15', async () => {
    const { user } = renderOrgPage({ orgScene: 'member-joined', machineModel: m15() });
    await openOrg(user);

    const section = machineSection() as HTMLElement;
    expect(section).toHaveTextContent('还没有机器记录');
    expect(within(section).queryByRole('list')).toBeNull();
    expect(within(section).queryByRole('listitem')).toBeNull();
  });

  it('TC-036 opens the organization page without entering the first-run flow', async () => {
    const { user } = renderOrgPage({ orgScene: 'member-joined', machineModel: m14() });
    await openOrg(user);

    expect(document.querySelector('[data-page="org"]')).not.toBeNull();
    expect(
      screen.queryByRole('heading', { name: '登录或注册' }),
    ).toBeNull();
  });

  it('TC-037 TC-068 keeps the machine section free of state, OS and identity access', () => {
    const productSource = readFileSync(
      resolve(process.cwd(), 'src/features/network/impl/machine-view-model.ts'),
      'utf8',
    );
    const componentSource = readFileSync(
      resolve(process.cwd(), 'src/features/network/impl/OrgMachines.tsx'),
      'utf8',
    );
    for (const source of [productSource, componentSource]) {
      expect(source).not.toMatch(/fetch\(|localStorage|sessionStorage/);
      expect(source).not.toMatch(/navigator\./);
      expect(source).not.toMatch(/process\.env/);
      expect(source).not.toMatch(/from 'node:os'|require\('os'\)/);
      expect(source).not.toMatch(/hostname\s*\(/);
      expect(source).not.toMatch(/studio-mac|travel-mac|合成系统/);
    }
  });
});

describe('J6 integration: unjoined and joined state', () => {
  it('does not reveal supplied machines before membership and restores them after joining', async () => {
    const { user, view } = renderOrgPage({ orgScene: 'join-failed', machineModel: m14() });
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(machineSection()).toBeNull();
    expect(document.body).not.toHaveTextContent('studio-mac');
    view.rerender(<FixtureApp dataMode="fixture" orgModel={buildOrgViewModel('member-joined')} machineModel={m14()} />);
    await user.click(screen.getByRole('tab', { name: '成员' }));
    await user.click(screen.getByRole('tab', { name: '设备' }));
    await screen.findByText('studio-mac');
    expect(within(machineSection()!).getAllByRole('listitem')).toHaveLength(2);
  });

  it('builds the hidden model without machines for a failed join', () => {
    const hidden = buildHiddenMachineSectionModel();
    expect(hidden.scene).toBe('hidden');
    expect(hidden.machines).toHaveLength(0);

  });
});
