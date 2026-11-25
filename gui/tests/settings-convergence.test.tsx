import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../src/dev/FixtureApp';
import { AppShell } from '../src/app/AppShell';
import { createEmptyState } from '../src/state/appReducer';
import { GuiCapabilitiesProvider } from '../src/app/capabilities';
import { stubFirstRunActions } from './support/first-run-actions';

describe('settings user/org convergence', () => {
  it('keeps only account access on the settings page', () => {
    const dispatch = vi.fn();
    const state = { ...createEmptyState(), page: 'settings' as const };
    const { container } = render(<GuiCapabilitiesProvider value={{ firstRun: stubFirstRunActions() }}>
      <AppShell state={state} dispatch={dispatch} />
    </GuiCapabilitiesProvider>);

    expect(container.querySelector('[data-page="settings"]')).not.toBeNull();
    expect(screen.getByRole('heading', { name: '用户设置' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '账号登录' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开设备' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开组织' })).not.toBeInTheDocument();

    for (const name of [
      '当前设备',
      '用户与平台身份绑定',
      '查询身份绑定',
      '管理本地用户与绑定',
      '模型服务',
      '本机服务与维护',
      '服务更新状态',
      '主机列表',
      '在线诊断',
      '日志事件摘要',
      '本机服务管理',
      '本机诊断',
      '个人消息代理',
      '网络配置',
      '自动升级策略',
    ]) {
      expect(screen.queryByRole('heading', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByLabelText('DeepSeek API Key')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '打开首次设置' })).not.toBeInTheDocument();
  });

  it('exposes device as an external main tab', async () => {
    const user = userEvent.setup();
    render(<FixtureApp dataMode="fixture" />);

    const deviceNav = screen.getByRole('button', { name: '设备' });
    await user.click(deviceNav);

    expect(deviceNav).toHaveAttribute('aria-current', 'page');
    expect(document.querySelector('[data-page="device"]')).not.toBeNull();
    expect(screen.getByRole('heading', { level: 1, name: '设备' })).toBeInTheDocument();
  });
});
