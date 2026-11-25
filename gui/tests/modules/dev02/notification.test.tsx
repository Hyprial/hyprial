import { GuiCapabilitiesProvider } from '../../../src/app/capabilities';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useReducer } from 'react';
import { AppShell } from '../../../src/app/AppShell';
import { appReducer } from '../../../src/state/appReducer';
import { createFixtureState } from '../../../src/dev/fixtures/fixture-state';
import type { AppState } from '../../../src/state/types';
import { NotificationCenter } from '../../../src/components/NotificationCenter';
import { DEMO_BASELINE, AUTHENTICATED_TEST_CAPABILITIES } from '../../support/demo-baseline';
import { createMockSharedCredentialsApi } from '../../../src/dev/mock/agent-settings';

const CAPABILITIES = { ...AUTHENTICATED_TEST_CAPABILITIES, sharedCredentials: createMockSharedCredentialsApi() };

// Exercise notifications independently of an unrelated channel authorization page.
function NotificationHarness() {
  const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
  return <><button onClick={() => dispatch({ type: 'push-notice', tone: 'warning', text: '功能暂不可用。', source: 'connection.adapter.manage' })}>触发测试通知</button>
    <GuiCapabilitiesProvider value={CAPABILITIES}><AppShell state={state} dispatch={dispatch} /></GuiCapabilitiesProvider></>;
}

describe('统一通知：状态层', () => {
  it('push-notice 入队且同文案去重', () => {
    let state: AppState = createFixtureState(DEMO_BASELINE);
    state = appReducer(state, { type: 'push-notice', tone: 'info', text: 'A' });
    state = appReducer(state, { type: 'push-notice', tone: 'warning', text: 'B' });
    state = appReducer(state, { type: 'push-notice', tone: 'info', text: 'A' });
    expect(state.notices.map((n) => n.text)).toEqual(['B', 'A']);
  });

  it('warning/error 落通知中心并计未读，打开面板清零', () => {
    let state: AppState = createFixtureState(DEMO_BASELINE);
    state = appReducer(state, { type: 'push-notice', tone: 'warning', text: 'w' });
    state = appReducer(state, { type: 'push-notice', tone: 'error', text: 'e' });
    state = appReducer(state, { type: 'push-notice', tone: 'info', text: 'i' });
    expect(state.centerNotices.map((n) => n.text)).toEqual(['e', 'w']);
    expect(state.centerUnread).toBe(2);
    state = appReducer(state, { type: 'toggle-notification-center', open: true });
    expect(state.centerUnread).toBe(0);
    state = appReducer(state, { type: 'clear-center-notifications' });
    expect(state.centerNotices).toEqual([]);
  });

  it('session-identity 仅在确认身份变化时清空中心与 toast；登出期间的条目保留', () => {
    const raise = (state: AppState) => appReducer(state, { type: 'push-notice', tone: 'warning', text: 'w' });
    const identity = (state: AppState, value: string | null) => appReducer(state, { type: 'session-identity', identity: value });
    let state = identity(createFixtureState(DEMO_BASELINE), null);
    state = identity(raise(state), 'a');
    expect(state.centerNotices).toHaveLength(1);
    expect(identity(state, 'a')).toBe(state);
    expect(identity(state, '').centerNotices).toHaveLength(1);
    const switched = identity(state, 'b');
    expect([switched.centerNotices, switched.notices, switched.centerUnread]).toEqual([[], [], 0]);
    const out = identity(state, null);
    expect([out.centerNotices, out.notices]).toEqual([[], []]);
    state = identity(raise(out), 'a');
    expect(state.centerNotices).toHaveLength(1);
  });

  it('旧 set-notice 镜像进 toast 队列（含暂不可用文案落中心）', () => {
    let state: AppState = createFixtureState(DEMO_BASELINE);
    state = appReducer(state, { type: 'set-notice', text: '功能暂不可用。' });
    expect(state.notices.map((n) => n.text)).toEqual(['功能暂不可用。']);
    expect(state.centerUnread).toBe(1);
  });

  it('push-notice 携带来源，去重键为文案+来源', () => {
    let state: AppState = createFixtureState(DEMO_BASELINE);
    state = appReducer(state, {
      type: 'push-notice', tone: 'warning', text: '功能暂不可用。', source: 'agent.start',
    });
    state = appReducer(state, {
      type: 'push-notice', tone: 'warning', text: '功能暂不可用。', source: 'connection.adapter.add',
    });
    // 同文案不同来源是两条事件；同文案同来源仍互相顶替。
    state = appReducer(state, {
      type: 'push-notice', tone: 'warning', text: '功能暂不可用。', source: 'agent.start',
    });
    // toast 队列新的在末尾；同来源重发顶替旧条目。
    expect(state.notices.map((n) => n.source)).toEqual([
      'connection.adapter.add',
      'agent.start',
    ]);
    expect(state.centerNotices.map((n) => n.source)).toEqual([
      'connection.adapter.add',
      'agent.start',
    ]);
    expect(state.centerUnread).toBe(2);
  });

  it('旧 set-notice 镜像不携带来源', () => {
    let state: AppState = createFixtureState(DEMO_BASELINE);
    state = appReducer(state, { type: 'set-notice', text: '功能暂不可用。' });
    expect(state.notices[0].source).toBeUndefined();
    expect(state.centerNotices[0].source).toBeUndefined();
  });
});

describe('统一通知：界面', () => {
  it('铃铛未读数、面板列表与清空', async () => {
    const user = userEvent.setup();
    render(<NotificationHarness />);

    // 触发两条落中心的 toast：去授权（warning）+ 重复文案去重。
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    const authorize = screen.getByRole('button', { name: '触发测试通知' });
    await user.click(authorize);
    await user.click(authorize);

    const bell = await screen.findByRole('button', { name: /通知中心，1 条未读/ });
    await user.click(bell);
    const panel = screen.getByRole('dialog', { name: '通知中心' });
    expect(within(panel).getAllByText('功能暂不可用。').length).toBe(1);

    await user.click(within(panel).getByRole('button', { name: '清空' }));
    expect(within(panel).getByText('暂无通知')).toBeTruthy();
  });

  it('通知中心条目显示来源；无来源时诚实标注', async () => {
    const user = userEvent.setup();
    render(<NotificationHarness />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    await user.click(screen.getByRole('button', { name: '触发测试通知' }));

    const bell = await screen.findByRole('button', { name: /通知中心，1 条未读/ });
    await user.click(bell);
    const panel = screen.getByRole('dialog', { name: '通知中心' });
    expect(within(panel).getByText('来源 · connection.adapter.manage')).toBeTruthy();
  });

  it('NotificationCenter 直接渲染：来源行与来源未确认', () => {
    const noop = () => {};
    render(
      <NotificationCenter
        open
        unread={2}
        dispatch={noop}
        items={[
          { id: 1, tone: 'warning', text: '功能暂不可用。', source: 'agent.start' },
          { id: 2, tone: 'error', text: '操作失败：服务暂时无法连接。' },
        ]}
      />,
    );
    const panel = screen.getByRole('dialog', { name: '通知中心' });
    expect(within(panel).getByText('来源 · agent.start')).toBeTruthy();
    expect(within(panel).getByText('来源未确认')).toBeTruthy();
  });

  it('铃铛与账号触发器同排，无边框字符，未读为无数字红点', async () => {
    const user = userEvent.setup();
    render(<NotificationHarness />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    await user.click(screen.getByRole('button', { name: '触发测试通知' }));

    const bell = await screen.findByRole('button', { name: /通知中心，1 条未读/ });
    const foot = bell.closest('.rail-foot');
    expect(foot).not.toBeNull();
    expect(
      foot!.contains(
        screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }),
      ),
    ).toBe(true);
    // 图标化：不再渲染「铃」文字；红点无数字。
    expect(screen.queryByText('铃')).toBeNull();
    const dot = bell.querySelector('.notif-dot');
    expect(dot).not.toBeNull();
    expect(dot!.textContent).toBe('');

    await user.click(bell);
    await user.click(screen.getByRole('button', { name: '清空' }));
    const quietBell = screen.getByRole('button', { name: '通知中心' });
    expect(quietBell.querySelector('.notif-dot')).toBeNull();
  });

  it('toast 可手动关闭', async () => {
    const user = userEvent.setup();
    render(<NotificationHarness />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    await user.click(screen.getByRole('button', { name: '触发测试通知' }));
    const host = document.querySelector('.toast-host') as HTMLElement;
    await user.click(within(host).getByRole('button', { name: '关闭提示' }));
    expect(document.querySelector('.toast-host')).toBeNull();
  });
});
