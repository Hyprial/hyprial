import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { ORGS, ORG_TEAM, joinedOrgDetail, ownerSnapshot } from '../../../src/dev/org/fixture-data';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import { createUnwiredGateway } from '../../../src/features/org/impl/domain/unwired';
import { openOrgTab, openRail, renderOrgScenario, stubRadixPointerApis } from './org-render';

function renderUnconfirmedOrgDetails() {
  const base = ownerSnapshot();
  const initial = {
    ...base,
    orgs: [ORGS[ORG_TEAM]],
    currentOrgId: ORG_TEAM,
    details: { [ORG_TEAM]: joinedOrgDetail() },
  };
  const user = userEvent.setup();
  render(
    <OrgDomainProvider source={{ initial, createGateway: () => createUnwiredGateway() }}>
      <FixtureApp dataMode="fixture" firstRunMode="complete" />
    </OrgDomainProvider>,
  );
  return { user };
}

beforeAll(stubRadixPointerApis);

type User = ReturnType<typeof renderOrgScenario>['user'];

async function openTopology(user: User, view?: '通信视图'): Promise<void> {
  await openRail(user, '组织');
  await openOrgTab(user, '拓扑');
  if (view) await user.click(screen.getByRole('tab', { name: view }));
}

/** Inspect 里某一项的值（dt 后面的 dd）。 */
function fact(panel: HTMLElement, label: string): string {
  const term = within(panel).getByText(label, { selector: 'dt' });
  return term.nextElementSibling?.textContent ?? '';
}

it('拓扑页保留结构与通信视图，并提供只读网络状态', async () => {
  const { user } = renderOrgScenario('owner');
  await openTopology(user);
  expect(screen.getByRole('tab', { name: '结构视图' })).toBeInTheDocument();
  expect(screen.getByRole('tab', { name: '通信视图' })).toBeInTheDocument();
  await user.click(screen.getByRole('tab', { name: '网络状态' }));
  expect(screen.getByRole('list', { name: '网络数据状态' })).toHaveTextContent('设备目录');
  expect(screen.getByRole('heading', { name: 'Sidecar 状态' })).toBeInTheDocument();
});

describe('拓扑：通信视图', () => {
  it('只显示路由约定（静态），不再渲染 link 层；点边打开 Inspect，返回后焦点回到这条边', async () => {
    const { user } = renderOrgScenario('owner');
    await openTopology(user, '通信视图');
    expect(screen.getByRole('tab', { name: '通信视图' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('region', { name: 'link（发送方 → 接收方）' })).toBeNull();
    expect(screen.getByRole('list', { name: '图例' })).not.toHaveTextContent('实线箭头：link');

    const routes = screen.getByRole('region', { name: '路由约定（静态）' });
    const edge = within(routes).getByRole('button', { name: '路由约定：部门 · 研发部，范围：全部成员' });
    await user.click(edge);
    const routeInspect = screen.getByRole('complementary', { name: 'Inspect：路由约定（部门 · 研发部）' });
    expect(edge).toHaveAttribute('aria-pressed', 'true');
    expect(fact(routeInspect, '方向')).toBe('未定义');
    expect(fact(routeInspect, '范围')).toBe('全部成员');
    expect(routeInspect).toHaveTextContent('路由约定只是组织配置里的静态约定，不代表可以发送。');

    await user.click(within(routeInspect).getByRole('link', { name: '拓扑' }));
    expect(screen.queryByRole('complementary', { name: /^Inspect：/ })).not.toBeInTheDocument();
    expect(edge).toHaveFocus();
    expect(edge).toHaveAttribute('aria-pressed', 'false');
  });

  it('设置中的通信权限暂不可用', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    const tab = screen.getByRole('tab', { name: '通信权限' });
    expect(tab).toHaveAttribute('aria-disabled', 'true');
    await user.click(tab);
    expect(screen.queryByRole('button', { name: '在拓扑中查看' })).not.toBeInTheDocument();
  });

  it('数据源没有提供路由约定时写明“状态未确认”，不画边', async () => {
    const { user } = renderUnconfirmedOrgDetails();
    await openTopology(user, '通信视图');
    expect(screen.queryByRole('region', { name: 'link（发送方 → 接收方）' })).toBeNull();
    const routes = screen.getByRole('region', { name: '路由约定（静态）' });
    expect(within(routes).getByText('状态未确认')).toBeInTheDocument();
    expect(within(routes).queryByRole('button', { name: /^路由约定：/ })).not.toBeInTheDocument();
  });
});

describe('拓扑：结构视图', () => {
  it('部门按汇报关系分层（上级 → 下级）；点部门看负责人、上级与成员', async () => {
    const { user } = renderOrgScenario('owner');
    await openTopology(user);
    expect(screen.getByRole('tab', { name: '结构视图' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText(/来自本节点已采信的组织配置（版本 3）/)).toBeInTheDocument();
    const depts = screen.getByRole('list', { name: '部门与汇报关系' });
    const eng = within(depts).getByRole('button', { name: /^研发部，部门/ });
    const gui = within(depts).getByRole('button', { name: /^GUI 组，部门/ });
    expect(eng.closest('li')).toContainElement(gui);
    await user.click(gui);
    const inspect = screen.getByRole('complementary', { name: 'Inspect：GUI 组' });
    expect(fact(inspect, '负责人')).toBe('林澄');
    expect(fact(inspect, '上级部门')).toBe('研发部');
    expect(fact(inspect, '成员')).toBe('林澄、周屿');

    await user.click(screen.getByRole('button', { name: /^GUI 初始化，业务线/ }));
    const line = screen.getByRole('complementary', { name: 'Inspect：GUI 初始化' });
    expect(fact(line, '协调人')).toBe('Squire');
    await user.click(screen.getByRole('button', { name: /^lin-mbp（评审），常驻角色/ }));
    expect(fact(screen.getByRole('complementary', { name: 'Inspect：lin-mbp（评审）' }), '服务对象')).toBe('GUI 初始化');
  });

  it('没有采信的配置时说明去哪里设置', async () => {
    const { user } = renderOrgScenario('template');
    await openTopology(user);
    expect(screen.getByText(/还没有采信的组织配置/)).toBeInTheDocument();
  });

  it('数据源没有提供组织配置时显示“状态未确认”', async () => {
    const { user } = renderUnconfirmedOrgDetails();
    await openTopology(user);
    expect(screen.getByRole('tabpanel', { name: '结构视图' })).toHaveTextContent('状态未确认');
    expect(screen.queryByRole('list', { name: '部门与汇报关系' })).not.toBeInTheDocument();
  });
});
