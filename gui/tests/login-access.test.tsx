import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { type AccountSnapshot } from '../src/features/onboarding';
import type { GuiCapabilities } from '../src/app/capabilities';
import { createFixtureFirstRunActions } from '../src/dev/first-run-capabilities';
import { OrgDomainProvider, UNWIRED_SOURCE, createUnwiredGateway, useOrgDomain, type OrgDomainSource } from '../src/features/org';
import { stubFirstRunActions } from './support/first-run-actions';

function accountCapabilities() {
  let authenticated = false;
  const snapshot = (): AccountSnapshot => ({
    account: authenticated ? { status: 'authenticated', owner: 'test@example.invalid' } : { status: 'unauthenticated' },
    device: { status: 'not-ready' },
  });
  const read = vi.fn(async () => []);
  const initializeDevice = vi.fn(async () => ({ status: 'ready' as const }));
  const capabilities: GuiCapabilities = {
    firstRunWizardEnabled: false,
    firstRun: {
      ...stubFirstRunActions(), getAccountState: async () => snapshot(), initializeDevice,
      authenticateAccount: async () => { authenticated = true; return snapshot().account; },
      logoutAccount: async () => { authenticated = false; return { status: 'logged-out', message: '', snapshot: snapshot() }; },
    },
    platformIntegrations: { list: read } as unknown as GuiCapabilities['platformIntegrations'],
    agents: { list: read } as unknown as GuiCapabilities['agents'],
    chatTargets: read,
  };
  return { capabilities, read, initializeDevice };
}

it('retains same-account device preparation across preview logout without exposing it while signed out', async () => {
  const actions = createFixtureFirstRunActions('complete', vi.fn(), 'ready');
  await actions.logoutAccount!();
  expect((await actions.getAccountState!()).device.status).toBe('not-ready');
  await actions.authenticateAccount!({ account: '', token: '' }, 'login');
  expect((await actions.getAccountState!()).device.status).toBe('ready');
});

it('retains the home card layout without showing injected business counts before login', async () => {
  const { capabilities, read } = accountCapabilities();
  const { container } = render(<App initialPage="start" capabilities={capabilities}
    startSummary={{ agents: { normal: 18, abnormal: 9 }, platformBindings: { normal: 7, abnormal: 3 } }} />);
  await screen.findByRole('button', { name: '创建 Agent' });
  expect(container.querySelector('.start-bento .recommendation-card')).toBeInTheDocument();
  expect(container.querySelector('.start-bento .start-status')).toBeInTheDocument();
  expect(screen.queryByText('18 / 9')).not.toBeInTheDocument();
  expect(read).not.toHaveBeenCalled();
});

it.each(['contacts', 'connections', 'chat', 'org'] as const)('keeps %s empty without mounting business controllers before login', async page => {
  const { capabilities, read } = accountCapabilities();
  const orgSource: OrgDomainSource = {
    initial: { ...UNWIRED_SOURCE.initial, status: 'loading' },
    createGateway: () => createUnwiredGateway(),
  };
  render(<App initialPage={page} capabilities={{ ...capabilities, orgSource }} />);
  expect(await screen.findByText('请先登录')).toBeInTheDocument();
  expect(read).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('button', { name: '去登录' }));
  expect(await screen.findByRole('button', { name: '登录 Hyprial' })).toBeInTheDocument();
});

it.each([['start', '创建 Agent'], ['device', '初始化当前设备']] as const)(
  'retains the requested %s entry with a login prompt', async (page, action) => {
  const { capabilities, read, initializeDevice } = accountCapabilities();
  render(<App initialPage={page} capabilities={capabilities} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: action }));
  expect(await screen.findByText('请先登录')).toBeInTheDocument();
  expect(read).not.toHaveBeenCalled();
  expect(initializeDevice).not.toHaveBeenCalled();
});

it('shares login and logout receipts with navigation without retaining protected content', async () => {
  const { capabilities, read } = accountCapabilities();
  const user = userEvent.setup();
  render(<App initialPage="settings" capabilities={capabilities} />);
  await screen.findByText('未登录');
  await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  expect(screen.queryByRole('menuitem', { name: '退出本机登录' })).not.toBeInTheDocument();
  await user.keyboard('{Escape}');
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await screen.findByText('test@example.invalid');
  await user.click(screen.getByRole('button', { name: '设备' }));
  expect(await screen.findByRole('button', { name: '初始化当前设备' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  expect(screen.getByRole('menuitem', { name: '退出本机登录' })).toBeInTheDocument();
  await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
  await user.click(await screen.findByRole('button', { name: '退出本机登录' }));
  await user.click(screen.getByRole('button', { name: '确认退出' }));
  await waitFor(() => expect(screen.queryByText('test@example.invalid')).not.toBeInTheDocument());
  await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  expect(screen.queryByRole('menuitem', { name: '退出本机登录' })).not.toBeInTheDocument();
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: 'Agent' }));
  expect(await screen.findByText('请先登录')).toBeInTheDocument();
  expect(read).not.toHaveBeenCalled();
});

it('refreshes organization reads after each confirmed login', async () => {
  const { capabilities } = accountCapabilities();
  const refresh = vi.fn(createUnwiredGateway().refresh);
  const orgSource = {
    initial: UNWIRED_SOURCE.initial, autoload: true,
    createGateway: () => ({ ...createUnwiredGateway(), refresh }),
  };
  const user = userEvent.setup();
  render(<App initialPage="settings" capabilities={{ ...capabilities, orgSource }} />);
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole('button', { name: '退出本机登录' }));
  await user.click(screen.getByRole('button', { name: '确认退出' }));
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await screen.findByText('test@example.invalid');
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(2));
});

it('drops late organization updates from an earlier login even when the source is reused', () => {
  const deliveries: Array<() => void> = [];
  const source: OrgDomainSource = {
    initial: UNWIRED_SOURCE.initial,
    createGateway: store => {
      deliveries.push(() => store.update(value => ({ ...value, currentOrgId: 'late-old-account' })));
      return createUnwiredGateway();
    },
  };
  function Probe() { return <p>{useOrgDomain().snapshot.currentOrgId ?? 'empty'}</p>; }
  const view = render(<OrgDomainProvider source={source} externalSnapshot={source.initial}><Probe /></OrgDomainProvider>);
  view.rerender(<OrgDomainProvider source={UNWIRED_SOURCE} externalSnapshot={UNWIRED_SOURCE.initial}><Probe /></OrgDomainProvider>);
  view.rerender(<OrgDomainProvider source={source} externalSnapshot={source.initial}><Probe /></OrgDomainProvider>);
  act(() => deliveries[0]());
  expect(screen.getByText('empty')).toBeInTheDocument();
  expect(screen.queryByText('late-old-account')).toBeNull();
});

it('never drops the 组织 entry during sign-out then sign-in with a working org.list', async () => {
  const { capabilities } = accountCapabilities();
  const orgSource: OrgDomainSource = {
    initial: { ...UNWIRED_SOURCE.initial, status: 'loading' }, autoload: true,
    createGateway: () => ({ ...createUnwiredGateway(), refresh: async () => ({ kind: 'ok' as const, value: undefined }) }),
  };
  const user = userEvent.setup();
  render(<App initialPage="settings" capabilities={{ ...capabilities, orgSource }} />);
  const entry = () => screen.queryByRole('button', { name: '组织' });
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await screen.findByText('test@example.invalid');
  await waitFor(() => expect(entry()).toBeInTheDocument());
  await user.click(screen.getByRole('button', { name: '退出本机登录' }));
  await user.click(screen.getByRole('button', { name: '确认退出' }));
  const login = await screen.findByRole('button', { name: '登录 Hyprial' });

  let missing = 0;
  let watching = false;
  const observer = new MutationObserver(() => { if (watching && !entry()) missing += 1; });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
  watching = true;
  await user.click(login);
  await screen.findByText('test@example.invalid');
  await waitFor(() => expect(entry()).toBeInTheDocument());
  await new Promise(resolve => setTimeout(resolve, 50));
  observer.disconnect();
  expect(missing).toBe(0);
  expect(entry()).toBeInTheDocument();
});
