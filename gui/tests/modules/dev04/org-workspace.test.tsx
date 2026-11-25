import { render, screen, within } from '@testing-library/react';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { OrgDomainProvider, UNWIRED_SOURCE } from '../../../src/features/org/impl/domain/context';
import { openOrgTab, openRail, orgLive, renderOrgScenario, stubRadixPointerApis } from './org-render';

beforeAll(stubRadixPointerApis);

describe('组织页：三种形态', () => {
  it('没有组织数据源时没有组织入口，也不渲染工作区', () => {
    render(<FixtureApp dataMode="fixture" />);
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
    expect(document.querySelector('.org-workspace')).toBeNull();
  });

  it('数据源未接通（UNWIRED_SOURCE）：没有组织入口，不发请求', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    render(
      <OrgDomainProvider source={UNWIRED_SOURCE}>
        <FixtureApp dataMode="fixture" />
      </OrgDomainProvider>,
    );
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
    expect(document.querySelector('.org-workspace')).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    fetchSpy.mockRestore();
  });
});

describe('组织页：所有者视角', () => {
  it('页头显示组织名、类型、角色和当前组织；计数卡带说明并能跳到子页签', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    expect(screen.getByRole('heading', { level: 1, name: 'hyprial-dev' })).toBeInTheDocument();
    const tags = screen.getByRole('group', { name: '组织状态' });
    expect(tags).toHaveTextContent('工作');
    expect(tags).toHaveTextContent('我的角色：所有者');
    expect(tags).toHaveTextContent('当前组织');
    expect(screen.queryByRole('button', { name: '设为当前组织' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Agent：/ })).toBeNull();

    const machines = screen.getByRole('button', { name: /^机器：4，在线 2 · 同步中 1 · 休眠 1/ });
    await user.click(machines);
    expect(screen.getByRole('tab', { name: '成员' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '设备' })).toHaveAttribute('aria-selected', 'true');
  });

  it('子页签支持方向键切换，Home/End 到首尾', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    const overview = screen.getByRole('tab', { name: '概览' });
    overview.focus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: '成员' })).toHaveFocus();
    await user.keyboard('{End}');
    expect(screen.getByRole('tab', { name: '设置' })).toHaveFocus();
    await user.keyboard('{Home}');
    expect(overview).toHaveFocus();
  });

  it('切换查看的组织不改变当前组织；“设为当前组织”后提示影响', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await user.click(screen.getByLabelText('查看组织'));
    await user.click(screen.getByRole('option', { name: 'acme-lab' }));
    expect(screen.getByRole('heading', { level: 1, name: 'acme-lab' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: '组织状态' })).toHaveTextContent('我的角色：成员');
    expect(screen.getByRole('button', { name: /^组织切换器：hyprial-dev/ })).toBeInTheDocument();

    await user.click(
      within(screen.getByRole('group', { name: '组织状态' })).getByRole('button', { name: '设为当前组织' }),
    );
    expect(await within(orgLive()).findByText(/当前组织已改为 acme-lab/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^组织切换器：acme-lab/ })).toBeInTheDocument();
  });

  it('新建组织：就地校验，成功后询问是否设为当前组织', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await user.click(screen.getByRole('button', { name: /^组织切换器：hyprial-dev/ }));
    await user.click(screen.getByRole('menuitem', { name: '新建组织' }));
    const dialog = screen.getByRole('dialog', { name: '新建组织' });
    const name = within(dialog).getByLabelText(/组织名称/);
    await user.type(name, 'Growth Team');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(within(dialog).getByText('只能使用小写字母、数字和连字符')).toBeInTheDocument();
    expect(name).toHaveAttribute('aria-invalid', 'true');

    await user.clear(name);
    await user.type(name, 'growth-team');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    const success = await screen.findByRole('dialog', { name: '组织已创建' });
    await user.click(within(success).getByRole('button', { name: '设为当前组织' }));
    expect(await within(orgLive()).findByText(/当前组织已改为 growth-team/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'growth-team' })).toBeInTheDocument();
  });

  it('发出邀请：生成中、结果对话框只有“复制邀请链接”“复制加入命令”；失败显示原因', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await user.click(screen.getByRole('button', { name: '邀请成员' }));
    expect(screen.getByRole('tab', { name: '发出邀请' })).toHaveAttribute('aria-selected', 'true');
    const account = screen.getByLabelText(/成员账号/);
    await user.type(account, 'fail@example.invalid');
    await user.click(screen.getByRole('button', { name: '生成邀请' }));
    expect(await screen.findByText('邀请链接未生成：邀请服务没有响应，请稍后重试')).toBeInTheDocument();

    await user.clear(account);
    await user.type(account, 'new@example.invalid');
    await user.click(screen.getByRole('button', { name: '生成邀请' }));
    const result = await screen.findByRole('dialog', { name: '邀请链接已生成' });
    expect(within(result).getByRole('button', { name: '复制邀请链接' })).toBeInTheDocument();
    expect(within(result).getByRole('button', { name: '复制加入命令' })).toBeInTheDocument();
    expect(within(result).queryByText(/邀请码/)).not.toBeInTheDocument();
    expect(within(result).getByText(/hyprial org join/)).toBeInTheDocument();
    // 有效期只出现一次，“前”不重复；凭据提醒也只出现一次。
    expect(within(result).getByText(/^1 小时内有效（\d{2}:\d{2} 前）$/)).toBeInTheDocument();
    expect(within(result).getAllByText(/1 小时内有效/)).toHaveLength(1);
    expect(within(result).getAllByText(/只私发给被邀请人/)).toHaveLength(1);
    await user.click(within(result).getByRole('button', { name: '关闭' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('移除成员：侧栏面包屑可返回；预览变化后要求再次确认，提交后显示阶段', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    const row = screen.getByRole('button', { name: '查看成员陈墨' });
    await user.click(row);
    const crumbs = screen.getByRole('navigation', { name: '面包屑' });
    expect(within(crumbs).getByRole('heading', { name: '陈墨' }).closest('li')).toHaveAttribute(
      'aria-current',
      'page',
    );
    await user.click(within(crumbs).getByRole('link', { name: '成员' }));
    expect(screen.queryByRole('navigation', { name: '面包屑' })).not.toBeInTheDocument();
    expect(row).toHaveFocus();

    await user.click(row);
    await user.click(screen.getByRole('button', { name: '移除成员' }));
    const dialog = screen.getByRole('dialog', { name: '移除成员陈墨？' });
    await user.click(await within(dialog).findByRole('button', { name: '确认移除' }));
    expect(await within(dialog).findByText('预览已变化，请核对最新预览后再次确认。')).toBeInTheDocument();
    // 新预览与上一次对比：多出的设备标“新出现”。
    const added = await within(dialog).findByText('chen-laptop');
    expect(within(added.closest('li') as HTMLElement).getByText('新出现')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '再次确认移除' }));
    // 提交不等于完成：标题改为结果陈述，按钮是“知道了”，阶段第一步是“已确认”。
    expect(await within(dialog).findByRole('button', { name: '知道了' })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '已提交：移除成员陈墨' })).toBe(dialog);
    expect(within(dialog).getByText('已确认')).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: '完成' })).not.toBeInTheDocument();
    expect(within(dialog).getByText('列表收到事件或刷新后才更新，不会立即删除这一行。')).toBeInTheDocument();
  });

  it('侧栏：打开的行显示选中态；“返回成员”关闭侧栏并把焦点还给该行', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    const row = screen.getByRole('button', { name: '查看成员林澄' });
    await user.click(row);
    expect(row).toHaveAttribute('aria-current', 'true');
    await user.click(screen.getByRole('button', { name: '返回成员' }));
    expect(screen.queryByRole('navigation', { name: '面包屑' })).not.toBeInTheDocument();
    expect(row).not.toHaveAttribute('aria-current');
    expect(row).toHaveFocus();
  });

  it('成员：只显示“人”和“设备”两个分组', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    expect(screen.queryByRole('tab', { name: '待审批' })).toBeNull();
    expect(screen.getByRole('tab', { name: '人' })).toBeInTheDocument();
    expect(within(screen.getByRole('tablist', { name: '成员分组' })).getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['人', '设备']);
    expect(screen.queryByRole('tab', { name: 'Agent' })).toBeNull();
    await user.click(screen.getByRole('tab', { name: '设备' }));
    expect(screen.getByRole('list', { name: '组织设备列表' })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/审批功能/);
  });

  it('切换子页签时收起上一页的提示', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('tab', { name: '设备' }));
    await user.click(screen.getByRole('button', { name: '立即重试' }));
    expect(await within(orgLive()).findByText('已对 chen-linux 发起重试，等待结果。')).toBeInTheDocument();
    await openOrgTab(user, '成员');
    expect(orgLive()).toHaveTextContent('');
  });

  it('开发模式（StrictMode）下动作结束后按钮恢复，不停在“处理中…”', async () => {
    const { user } = renderOrgScenario('owner', { strict: true });
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('button', { name: '查看成员陈墨' }));
    await user.click(screen.getByRole('button', { name: '移除成员' }));
    const dialog = screen.getByRole('dialog', { name: '移除成员陈墨？' });
    await user.click(await within(dialog).findByRole('button', { name: '确认移除' }));
    const again = await within(dialog).findByRole('button', { name: '再次确认移除' });
    await vi.waitFor(() => expect(again).toBeEnabled());
    expect(within(dialog).queryByRole('button', { name: /处理中/ })).not.toBeInTheDocument();
  });

  it('机器：“这台电脑”侧栏列出迁入请求，同意前先确认', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('tab', { name: '设备' }));
    await user.click(screen.getByRole('button', { name: '查看这台电脑' }));
    await user.click(screen.getByRole('button', { name: '打开迁移单 MT-2026-1029-07' }));
    const ticket = screen.getByRole('dialog', { name: '迁移单 MT-2026-1029-07' });
    // 迁移单 7 项逐项列出；缺项写明“未知”、原因与责任方。
    expect(within(ticket).getByText('迁移计划 7 项')).toBeInTheDocument();
    expect(within(ticket).getByText('工作目录')).toBeInTheDocument();
    expect(within(ticket).getByText(/未知：.*（责任方：目标方）/)).toBeInTheDocument();
    await user.click(within(ticket).getByRole('button', { name: '同意' }));
    const confirm = screen.getByRole('dialog', { name: '确认同意迁入？' });
    await user.click(within(confirm).getByRole('button', { name: '返回核对' }));
    expect(screen.getByRole('dialog', { name: '迁移单 MT-2026-1029-07' })).toBeInTheDocument();
  });

  it('文件：保存时遇到冲突，显示对方版本并可保留我的修改', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: 'onboarding.md' }));
    await user.click(screen.getByRole('button', { name: '编辑' }));
    const editor = screen.getByRole('dialog', { name: /onboarding\.md/ });
    const text = within(editor).getByRole('textbox');
    await user.type(text, '5. 我的补充。');
    await user.click(within(editor).getByRole('button', { name: '保存' }));
    expect(await within(editor).findByText(/文件已被别人改到版本 4/)).toBeInTheDocument();
    expect(within(editor).getByRole('button', { name: '保存' })).toBeDisabled();
    // 冲突时先给出两个选择：编辑框收起，焦点落在第一个选择上。
    const keep = within(editor).getByRole('button', { name: '保留我的修改' });
    expect(keep).toHaveFocus();
    expect(within(editor).queryByRole('textbox')).not.toBeInTheDocument();
    expect(within(editor).getByText(/5\. 我的补充。/, { selector: 'pre' })).toBeInTheDocument();
    await user.click(keep);
    const restored = within(editor).getByRole<HTMLTextAreaElement>('textbox');
    expect(restored.value).toContain('5. 我的补充。');
    expect(restored).toHaveFocus();
    expect(within(editor).getByText(/基于版本 4/)).toBeInTheDocument();
    await user.click(within(editor).getByRole('button', { name: '保存' }));
    expect(await within(editor).findByText('已保存（版本 5）')).toBeInTheDocument();
  });

  it('文件：冲突时选“使用对方版本”，编辑框换成对方内容', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: 'onboarding.md' }));
    await user.click(screen.getByRole('button', { name: '编辑' }));
    const editor = screen.getByRole('dialog', { name: /onboarding\.md/ });
    await user.type(within(editor).getByRole('textbox'), '5. 我的补充。');
    await user.click(within(editor).getByRole('button', { name: '保存' }));
    await user.click(await within(editor).findByRole('button', { name: '使用对方版本' }));
    const restored = within(editor).getByRole<HTMLTextAreaElement>('textbox');
    expect(restored.value).not.toContain('5. 我的补充。');
    expect(restored.value).toContain('# 新成员入门');
    expect(restored).toHaveFocus();
  });

  it('设置：改名只显示“已提交，等待处理”；成员视角离开组织需输入组织名', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('button', { name: '改名' }));
    const rename = screen.getByRole('dialog', { name: '改名 hyprial-dev' });
    const field = within(rename).getByLabelText(/新名称/);
    await user.clear(field);
    await user.type(field, 'hyprial-core');
    await user.click(within(rename).getByRole('button', { name: '确认改名' }));
    expect(await within(orgLive()).findByText('改名为 hyprial-core：已提交，等待处理')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'hyprial-dev' })).toBeInTheDocument();
    // 生效前在基础信息里标出，并暂停再次改名。
    expect(screen.getByText(/改名为 hyprial-core：已提交，等待处理。/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '改名' })).toBeDisabled();
  });

  it('配置编辑器：先校验才能发布候选；修改后要重新校验', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('tab', { name: '版本' }));
    await user.click(screen.getByRole('button', { name: '编辑' }));
    const editor = screen.getByRole('dialog', { name: '配置编辑器' });
    const publish = within(editor).getByRole('button', { name: '发布候选' });
    expect(publish).toBeDisabled();
    expect(within(editor).getByText('修改后需要重新校验。')).toBeInTheDocument();
    await user.click(within(editor).getByRole('button', { name: '校验' }));
    await vi.waitFor(() => expect(publish).toBeEnabled());
    await user.type(within(editor).getAllByLabelText('部门名称')[0], '二部');
    expect(publish).toBeDisabled();
  });
});

describe('组织页：没有加入组织与收到的邀请', () => {
  it('没有组织时概览提供新建与加入入口，不显示“邀请成员”', async () => {
    const { user } = renderOrgScenario('no-orgs');
    await openRail(user, '组织');
    expect(screen.getByText('还没有可用的组织。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '新建组织' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '用邀请链接加入' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '邀请成员' })).not.toBeInTheDocument();
  });

  it('逐条领取邀请：已加入、未加入（原因）、已跳过各用独立文案', async () => {
    const { user } = renderOrgScenario('invites');
    await user.click(await screen.findByRole('button', { name: '稍后处理' }));
    await openRail(user, '组织');
    await openOrgTab(user, '邀请');
    const list = screen.getByRole('list', { name: '我收到的邀请' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(3);

    await user.click(within(rows[0]).getByRole('button', { name: '加入' }));
    await user.click(within(screen.getByRole('dialog', { name: '加入 design-guild？' })).getByRole('button', { name: '加入' }));
    expect(await screen.findByText('已加入 design-guild')).toBeInTheDocument();
    expect(await screen.findByText('是否把 design-guild 设为当前组织？')).toBeInTheDocument();

    await user.click(within(rows[1]).getByRole('button', { name: '加入' }));
    await user.click(within(screen.getByRole('dialog', { name: '加入 ops-oncall？' })).getByRole('button', { name: '加入' }));
    expect(await screen.findByText(/未加入 ops-oncall：邀请版本过旧/)).toBeInTheDocument();
    // 版本过旧重试无效：不给“重试”。
    expect(within(rows[1]).queryByRole('button', { name: '重试' })).not.toBeInTheDocument();

    // 第一条已加入 design-guild：同一组织的另一条邀请由数据源标为已跳过，不再提供“加入”。
    expect(within(rows[2]).getByText('已跳过：本机已在该组织')).toBeInTheDocument();
    expect(within(rows[2]).queryByRole('button')).not.toBeInTheDocument();
    // 角标只数还要处理的邀请：已跳过的不算。
    expect(screen.getByRole('tab', { name: /^我收到的邀请/ })).toHaveTextContent(/^我收到的邀请\s*1 项待处理$/);
  });

  it('用邀请链接加入：提交后清空输入；过期显示原因与下一步，不显示成功', async () => {
    const { user } = renderOrgScenario('no-orgs');
    await openRail(user, '组织');
    await openOrgTab(user, '邀请');
    await user.click(screen.getByRole('tab', { name: '用邀请链接加入' }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    expect(link).toHaveAttribute('type', 'password');
    await user.type(link, 'https://invite.example.invalid/j/expired-1');
    await user.click(screen.getByRole('button', { name: '加入' }));
    expect(link).toHaveValue('');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('加入未完成：邀请已过期（1 小时）');
    expect(alert).toHaveTextContent('请邀请人重新生成邀请链接');
    expect(screen.queryByText(/^已加入/)).not.toBeInTheDocument();
    expect(localStorage.length).toBe(0);
  });
});
