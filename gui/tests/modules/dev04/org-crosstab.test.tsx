import { explicitAgentFixtureSeed } from '../../unified-backend/agents-test-client';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../../../src/App';
import { createLocalAgentGateway } from '../../../src/features/agents';
import { openOrgTab, openRail, orgLive, renderOrgScenario, stubRadixPointerApis } from './org-render';
import { DEMO_BASELINE, AUTHENTICATED_TEST_CAPABILITIES } from '../../support/demo-baseline';
import { expectUnavailableToast } from '../../support/notify-harness';

beforeAll(stubRadixPointerApis);

function card(agentId: string): HTMLElement {
  return screen.getByTestId(`contact-${agentId}`);
}

describe('Agent 页的组织入口', () => {
  it('没有组织数据源时 Agent 页没有组织筛选，创建表单也不提供组织选项', async () => {
    const user = userEvent.setup();
    const seed = explicitAgentFixtureSeed();
    render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} baseline={DEMO_BASELINE} agentSeed={seed}
      agentGateway={createLocalAgentGateway({ agents: [], connections: seed.connections, organizations: [] })} />);
    await openRail(user, 'Agent');
    expect(screen.queryByLabelText('组织')).not.toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '创建 Agent' })[0]);
    expect(screen.queryByLabelText('所属组织')).not.toBeInTheDocument();
  });

  it('卡片显示所属组织；按组织筛选时命中的组织排第一，清除筛选一并清除', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, 'Agent');
    const writer = card('agent-writer');
    expect(writer).toHaveTextContent('hyprial-dev');
    expect(writer).toHaveTextContent('acme-lab');
    expect(writer).not.toHaveTextContent('我的组织（个人）');

    await user.click(screen.getByLabelText('组织'));
    await user.click(screen.getByRole('option', { name: 'acme-lab' }));
    expect(screen.getByText('找到 2 个，共 6 个')).toBeInTheDocument();
    expect(screen.queryByTestId('contact-agent-researcher')).not.toBeInTheDocument();
    const badges = Array.from(card('agent-writer').querySelectorAll('.badge'));
    const names = badges.map((badge) => badge.textContent);
    expect(names.indexOf('acme-lab')).toBeLessThan(names.indexOf('hyprial-dev'));
    expect(badges[names.indexOf('acme-lab')]).toHaveClass('badge-positive');

    await user.click(screen.getByRole('button', { name: '清除筛选' }));
    expect(screen.queryByText(/找到 \d+ 个，共 \d+ 个/)).toBeNull();
    expect(screen.getByLabelText('组织')).toHaveTextContent('全部');
  });

  it('组织概览不重复提供创建 Agent 操作', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await user.click(screen.getByLabelText('查看组织'));
    await user.click(screen.getByRole('option', { name: 'acme-lab' }));
    expect(screen.queryByRole('button', { name: '在此组织新建 Agent' })).toBeNull();
  });

  it('新建的 Agent 不会自动获得组织归属标签', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, 'Agent');
    await user.click(screen.getAllByRole('button', { name: '创建 Agent' })[0]);
    await user.type(screen.getByLabelText(/Agent 名称|名称/), '周报助手');
    await user.type(screen.getByLabelText(/职责/), '整理周报');
    await user.click(screen.getByRole('button', { name: /^创建/ }));
    await screen.findByTestId('agent-detail-panel');
    await user.click(screen.getByRole('tab', { name: '所属组织' }));
    expect(screen.getByText('未关联组织。')).toBeInTheDocument();
    await user.click(screen.getByRole('link', { name: 'Agent' }));
    const created = screen.getByText('周报助手').closest('button');
    expect(created).toHaveTextContent('未关联组织');
  });

  it('详情：所属组织一行列出全部组织；允许移出最后一个组织；加入工作组织时由操作结果通知', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, 'Agent');
    await user.click(screen.getByLabelText('组织'));
    await user.click(screen.getByRole('option', { name: '全部' }));
    await user.click(card('agent-ops'));
    await user.click(screen.getByRole('tab', { name: '所属组织' }));
    expect(within(screen.getByRole('list', { name: '所属组织列表' })).getByText('hyprial-dev')).toBeInTheDocument();
    const orgs = screen.getByRole('list', { name: '所属组织列表' });
    expect(within(orgs).getByRole('button', { name: '移出' })).toBeEnabled();
    expect(screen.queryByText('每个 Agent 至少属于一个组织。')).not.toBeInTheDocument();
    await user.click(within(orgs).getByRole('button', { name: '移出' }));
    const leave = screen.getByRole('dialog', { name: /把运营执行移出/ });
    await user.click(within(leave).getByRole('button', { name: '移出' }));
    expect(await screen.findByText('未关联组织。')).toBeInTheDocument();
    expect(screen.queryByText('当前版本暂不支持移出最后一个已关联组织。')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '加入组织' }));
    const dialog = screen.getByRole('dialog', { name: '把运营执行加入组织' });
    const select = within(dialog).getByLabelText('组织');
    expect(within(select).queryByRole('option', { name: '我的组织（个人）' })).not.toBeInTheDocument();
    await user.selectOptions(select, 'org-hyprial-dev');
    expect(within(dialog).queryByRole('note')).toBeNull();
    expect(dialog).not.toHaveTextContent('审批功能');
    const submit = within(dialog).getByRole('button', { name: '加入组织' });
    expect(submit).toBeEnabled();
    await user.click(submit);
    expectUnavailableToast('加入组织暂不可用。');
    expect(submit).toBeEnabled();
    expect(screen.getByRole('dialog', { name: '把运营执行加入组织' })).toBeInTheDocument();
  });

  it('详情 → 所属组织：不再渲染发送 link 管理与最近被拒的消息', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, 'Agent');
    await user.click(card('agent-squire'));
    await user.click(screen.getByRole('tab', { name: '所属组织' }));
    expect(screen.queryByRole('list', { name: '可以发送的对象' })).toBeNull();
    expect(screen.queryByRole('button', { name: '添加可以发送的对象' })).toBeNull();
    expect(screen.queryByRole('button', { name: '收回' })).toBeNull();
    expect(screen.queryByRole('list', { name: '最近被拒的消息' })).toBeNull();
    expect(screen.getByRole('button', { name: '查看统计' })).toBeInTheDocument();
  });

  it('详情 → 迁移：检查通过后核对 7 项迁移单再发起；刷新后才能开始', async () => {
    const { user } = renderOrgScenario('migration');
    await openRail(user, 'Agent');
    await user.click(card('agent-writer'));
    await user.click(screen.getByRole('tab', { name: '所属组织' }));
    await user.click(screen.getByRole('button', { name: '迁移到组织内的机器' }));
    const wizard = screen.getByRole('dialog', { name: '迁移写作者' });
    expect(within(wizard).queryByRole('radio', { name: /lin-mbp/ })).toBeNull();
    expect(wizard).not.toHaveTextContent('暂不支持迁移到他人的机器');
    await user.click(within(wizard).getByRole('radio', { name: /yao-mini/ }));
    await user.click(within(wizard).getByRole('button', { name: '下一步：检查' }));
    expect(await within(wizard).findByText(/通过：目标机器在线/)).toBeInTheDocument();
    await user.click(within(wizard).getByRole('button', { name: '下一步：核对计划' }));
    expect(await within(wizard).findByText('工作目录')).toBeInTheDocument();
    // 发起前没有后端状态：显示“未发起”，不显示“等待目标方同意”。
    expect(within(wizard).getByText('未发起')).toBeInTheDocument();
    expect(within(wizard).queryByText('等待目标方同意')).not.toBeInTheDocument();
    await user.click(within(wizard).getByRole('button', { name: '发起' }));
    // 新迁移单的编号与已有的迁移单（MT-2026-1029-09）不同。
    expect(await screen.findByText(/迁移单 MT-2026-1030-\d+ 已发起：等待目标方同意/)).toBeInTheDocument();
    expect(screen.queryByText(/迁移单 MT-2026-1029-09/)).not.toBeInTheDocument();
    const start = screen.getByRole('button', { name: '开始迁移' });
    expect(start).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '刷新' }));
    expect(await screen.findByText(/迁移单 MT-2026-1030-\d+ · 可以开始/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始迁移' })).toBeEnabled();
  });
});

describe('聊天被拒提示与开始页提醒', () => {
  it('没有共同组织：修复入口跳到 Agent 详情并预选对方所在组织；所有者另见邀请入口', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '聊天');
    expect(screen.getByText('你和许沐没有共同组织。')).toBeInTheDocument();
    // 与输入区的“重试”配合：先修复，再手动重试。
    expect(screen.getByText('修复后点下方“重试”重新发送；不会自动重发。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '邀请许沐加入组织' }));
    expect(screen.getByRole('tab', { name: '发出邀请' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByLabelText(/成员账号/)).toHaveValue('xu@example.invalid');

    await openRail(user, '聊天');
    await user.click(screen.getByRole('button', { name: '把 Squire 加入许沐所在的组织' }));
    const dialog = await screen.findByRole('dialog', { name: '把 Squire 加入组织' });
    expect(within(dialog).getByLabelText('组织')).toHaveValue('org-acme-lab');
  });

  it('观察期的组织策略提示：照常发送，入口跳到设置且通信权限保持禁用', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '聊天');
    expect(
      screen.getByText(/观察期：本条已照常送达；进入强制期后，同类消息会被拒绝。原因：组织 hyprial-dev 的通信设置不允许这条消息。/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看 hyprial-dev 的通信权限' }));
    expect(screen.getByRole('tab', { name: '设置' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '通信权限' })).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('tab', { name: '基础信息' })).toHaveAttribute('aria-selected', 'true');
  });

  it('开始页：迁入请求提醒卡进入成员 → 设备 → 这台电脑侧栏', async () => {
    const { user } = renderOrgScenario('owner');
    expect(await screen.findByText('有 1 个迁入请求等待处理')).toBeInTheDocument();
    expect(screen.queryByText(/你有 \d+ 条组织邀请/)).not.toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '查看迁入请求' }));
    expect(screen.getByRole('tab', { name: /^成员/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '设备' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('navigation', { name: '面包屑' })).toHaveTextContent('这台电脑');
  });

  it('登录后的邀请确认只出现一次；稍后处理后从提醒卡进入我收到的邀请', async () => {
    const { user } = renderOrgScenario('invites');
    const dialog = await screen.findByRole('dialog', { name: '你有待处理的组织邀请' });
    expect(within(dialog).getAllByRole('button', { name: '加入' })).toHaveLength(3);
    await user.click(within(dialog).getByRole('button', { name: '稍后处理' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await openRail(user, 'Agent');
    await openRail(user, '开始');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看邀请' }));
    expect(screen.getByRole('tab', { name: /^我收到的邀请/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('组织子页签的角标只来自后端数据', async () => {
    const { user } = renderOrgScenario('invites');
    await user.click(await screen.findByRole('button', { name: '稍后处理' }));
    await openRail(user, '组织');
    expect(screen.getByRole('tab', { name: /^邀请/ })).toHaveTextContent('3');
    await openOrgTab(user, '邀请');
    expect(screen.getByRole('tab', { name: /^我收到的邀请/ })).toHaveTextContent('3');
  });
});

describe('迁移：参与方在完成前都可以取消', () => {
  it('目标方同意后仍可取消：在同一对话框内确认；取消后迁移单只剩“关闭”', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('tab', { name: '设备' }));
    await user.click(screen.getByRole('button', { name: '查看这台电脑' }));
    await user.click(screen.getByRole('button', { name: '打开迁移单 MT-2026-1029-07' }));
    await user.click(within(screen.getByRole('dialog', { name: '迁移单 MT-2026-1029-07' })).getByRole('button', { name: '同意' }));
    await user.click(within(screen.getByRole('dialog', { name: '确认同意迁入？' })).getByRole('button', { name: '确认同意' }));
    await vi.waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: '打开迁移单 MT-2026-1029-07' }));
    const ticket = screen.getByRole('dialog', { name: '迁移单 MT-2026-1029-07' });
    expect(within(ticket).getByText('待验证')).toBeInTheDocument();
    await user.click(within(ticket).getByRole('button', { name: '取消迁移' }));
    const confirm = screen.getByRole('dialog', { name: '确认取消迁移？' });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    // 确认框里的退出按钮不叫“取消”，避免和“取消迁移”混淆。
    expect(within(confirm).queryByRole('button', { name: '取消' })).not.toBeInTheDocument();
    await user.click(within(confirm).getByRole('button', { name: '确认取消迁移' }));
    expect(await within(orgLive()).findByText('迁移单 MT-2026-1029-07 已取消。')).toBeInTheDocument();
    const row = screen.getByRole('button', { name: '打开迁移单 MT-2026-1029-07' }).closest('li');
    expect(row).toHaveTextContent('已取消');

    await user.click(screen.getByRole('button', { name: '打开迁移单 MT-2026-1029-07' }));
    const closed = screen.getByRole('dialog', { name: '迁移单 MT-2026-1029-07' });
    expect(within(closed).queryByRole('button', { name: '取消迁移' })).not.toBeInTheDocument();
    expect(within(closed).getByRole('button', { name: '关闭' })).toBeInTheDocument();
  });

  it('迁移中也可以取消；失败时留在确认步骤显示原因并可重试；已完成的迁移单不显示取消', async () => {
    const { user } = renderOrgScenario('migration');
    await user.click(await screen.findByRole('button', { name: '查看迁入请求' }));
    await user.click(screen.getByRole('button', { name: '打开迁移单 MT-2026-1029-05' }));
    const ticket = screen.getByRole('dialog', { name: '迁移单 MT-2026-1029-05' });
    expect(within(ticket).getByText('迁移中')).toBeInTheDocument();
    expect(within(ticket).getByLabelText('迁移进度')).toBeInTheDocument();
    await user.click(within(ticket).getByRole('button', { name: '取消迁移' }));
    const confirm = screen.getByRole('dialog', { name: '确认取消迁移？' });
    await user.click(within(confirm).getByRole('button', { name: '确认取消迁移' }));
    expect(await within(confirm).findByRole('alert')).toHaveTextContent('取消没有送达');
    await user.click(within(confirm).getByRole('button', { name: '确认取消迁移' }));
    expect(await within(orgLive()).findByText('迁移单 MT-2026-1029-05 已取消。')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开迁移单 MT-2026-1028-03' }));
    const done = screen.getByRole('dialog', { name: '迁移单 MT-2026-1028-03' });
    expect(within(done).getByText('已完成')).toBeInTheDocument();
    expect(within(done).queryByRole('button', { name: '取消迁移' })).not.toBeInTheDocument();
  });

  it('源端取消迁移先确认；返回不改变状态，确认后显示已取消', async () => {
    const { user } = renderOrgScenario('migration');
    await openRail(user, 'Agent');
    await user.click(card('agent-researcher'));
    await user.click(screen.getByRole('tab', { name: '所属组织' }));
    await user.click(screen.getByRole('button', { name: '取消迁移' }));
    const confirm = screen.getByRole('dialog', { name: '取消迁移单 MT-2026-1029-09？' });
    expect(within(confirm).queryByRole('button', { name: '取消' })).not.toBeInTheDocument();
    await user.click(within(confirm).getByRole('button', { name: '返回' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText(/迁移单 MT-2026-1029-09 · 等待目标方同意/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '取消迁移' }));
    await user.click(
      within(screen.getByRole('dialog', { name: '取消迁移单 MT-2026-1029-09？' })).getByRole('button', { name: '确认取消迁移' }),
    );
    expect(await screen.findByText('上一张迁移单 MT-2026-1029-09：已取消')).toBeInTheDocument();
  });
});
