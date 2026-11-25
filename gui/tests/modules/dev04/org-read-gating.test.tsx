/*
 * Decision record (unavailable copy to notifications, PR 1f/F6, 2026-11-15):
 * in the formal build a detail read that the served catalog refuses is no
 * port, not a failure. The tab built only on that read is not rendered (and
 * nothing links into it): 机器 needs org.network, 成员 needs org.network or
 * orgfs.members, 文件 needs orgfs.spaces, orgfs.ls and orgfs.read. A read that
 * was dispatched and failed keeps its text and its 刷新. 拓扑 and 统计 are gated
 * on the source instead of the catalog: the live source never provides their
 * data, so it does not declare them (`provides`) and the formal build has no
 * such tab, no 查看统计 link and no 在拓扑中查看 button; a source that does not
 * restrict `provides` (fixture, preview) keeps both tabs. 设置 is not gated.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../../../src/App';
import { createBackendConnection } from '../../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../../src/app/capabilities';
import { HttpError, type RequestDiagnostics } from '../../../src/client';
import { authenticatedTestActions } from '../../support/demo-baseline';
import { AgentOrgPanel, OrgDomainProvider, OrgWorkspace, useOrgDomain } from '../../../src/features/org';
import { buildOrgDevPreview } from '../../../src/dev/org/scenarios';
import { openOrgTab, openRail, renderOrgScenario, stubRadixPointerApis } from './org-render';

beforeAll(stubRadixPointerApis);

const ORG_LIST = {
  orgs: [{ org: 'group-hyprial-dev', spaceId: 'space-team', role: 'owner', devices: 1, state: 'active' }],
  leftOrgs: [],
};

function diagnostics(method: string): RequestDiagnostics {
  return { method, requestId: null, status: 503, dispatch: 'dispatched', catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 0 };
}

function formalApp(extraMethods: readonly string[]) {
  const methods = [
    { name: 'org.list', callable: true, state: 'available', mutates: false },
    ...extraMethods.map((name) => ({ name, callable: true, state: 'available', mutates: false })),
  ] as const;
  const connection = createBackendConnection({
    async invoke<TResult>(method: string): Promise<TResult> {
      if (method === 'org.list') return ORG_LIST as TResult;
      throw new HttpError('SERVICE_UNAVAILABLE', 'read failed', 503, diagnostics(method));
    },
  }, {
    interfaceVersion: 1, driver: 'contract-mock', phase: 'ready', generation: 'g1', catalogDigest: 'sha256:c',
    methods, verifiedWrites: [],
  });
  return {
    ...createConnectedGuiCapabilities(connection),
    firstRun: authenticatedTestActions(),
    firstRunWizardEnabled: false,
  };
}

function tabNames(): string[] {
  const tabs = screen.getByRole('tablist', { name: '组织子页签' });
  return within(tabs).getAllByRole('tab').map((tab) => (tab.textContent ?? '').replace(/\d+$/, '').trim());
}

describe('formal organization detail reads follow the served catalog', () => {
  it('keeps unfinished settings unavailable and removes the top-level machine tab', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    expect(tabNames()).not.toContain('机器');
    await openOrgTab(user, '设置');
    const settings = screen.getByRole('tablist', { name: '设置分组' });
    for (const name of ['结构与部门', '通信权限', '规范']) {
      const tab = within(settings).getByRole('tab', { name });
      expect(tab).toHaveAttribute('aria-disabled', 'true');
      await user.click(tab);
      expect(tab).toHaveAttribute('aria-selected', 'false');
    }
    expect(within(settings).getByRole('tab', { name: '基础信息' })).toHaveAttribute('aria-selected', 'true');
  });

  it('renders no 机器, 成员 or 文件 tab, and no link into them, when their reads are refused', async () => {
    render(<App capabilities={formalApp([])} initialPage="org" />);
    await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' });
    expect(tabNames()).toEqual(['概览', '邀请', '设置']);
    expect(screen.queryByRole('button', { name: /^成员：/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^机器：/ })).toBeNull();
    expect(document.body.textContent).not.toMatch(/文件列表读取失败/);
  });

  it('keeps 成员 through org.network without restoring the removed 机器 tab', async () => {
    render(<App capabilities={formalApp(['org.network'])} initialPage="org" />);
    await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' });
    expect(tabNames()).toEqual(['概览', '成员', '邀请', '设置']);
  });

  it('keeps 成员 through orgfs.members alone and drops 机器', async () => {
    render(<App capabilities={formalApp(['orgfs.members'])} initialPage="org" />);
    await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' });
    expect(tabNames()).toEqual(['概览', '成员', '邀请', '设置']);
  });

  it('keeps a dispatched file-list failure as its text with a refresh control', async () => {
    const user = userEvent.setup();
    render(<App capabilities={formalApp(['org.network', 'orgfs.spaces', 'orgfs.ls', 'orgfs.read'])} initialPage="org" />);
    await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' });
    expect(tabNames()).toContain('文件');
    await openOrgTab(user, '文件');
    expect((await screen.findAllByText(/文件列表读取失败/)).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /刷新/ }).length).toBeGreaterThan(0);
  });

  it('hides 文件 when any one of its three reads is refused', async () => {
    render(<App capabilities={formalApp(['org.network', 'orgfs.spaces', 'orgfs.ls'])} initialPage="org" />);
    await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' });
    expect(tabNames()).not.toContain('文件');
  });
});

function previewSource(provides?: (tab: string) => boolean) {
  const preview = buildOrgDevPreview('owner', { latencyMs: 0 });
  if (!preview) throw new Error('no preview');
  return { ...preview.source, ...(provides ? { provides } : {}) } as typeof preview.source;
}

function IntentButton({ tab }: { readonly tab: 'stats' | 'topology' }) {
  const domain = useOrgDomain();
  return <button type="button" onClick={() => domain.requestOrgView({ tab })}>去{tab}</button>;
}

describe('拓扑 and 统计 follow what the org source provides', () => {
  it('keeps both tabs in the preview, whose source does not restrict them', async () => {
    const { user } = renderOrgScenario('owner');
    await openRail(user, '组织');
    expect(tabNames()).toEqual(expect.arrayContaining(['拓扑', '统计']));
  });

  it('renders no tab, and no jump into it, for a source that does not provide them', async () => {
    const user = userEvent.setup();
    render(<OrgDomainProvider source={previewSource((tab) => tab !== 'stats' && tab !== 'topology')}>
      <IntentButton tab="stats" />
      <OrgWorkspace dispatch={() => undefined} />
    </OrgDomainProvider>);
    await screen.findByRole('tablist', { name: '组织子页签' });
    expect(tabNames()).not.toContain('统计');
    expect(tabNames()).not.toContain('拓扑');
    await user.click(screen.getByRole('button', { name: '去stats' }));
    const tabs = screen.getByRole('tablist', { name: '组织子页签' });
    expect(within(tabs).getByRole('tab', { name: /^概览/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('drops 在拓扑中查看 with the topology tab', async () => {
    const user = userEvent.setup();
    render(<OrgDomainProvider source={previewSource((tab) => tab !== 'topology')}>
      <OrgWorkspace dispatch={() => undefined} />
    </OrgDomainProvider>);
    await screen.findByRole('tablist', { name: '组织子页签' });
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('tab', { name: '通信权限' }));
    expect(screen.queryByRole('button', { name: '在拓扑中查看' })).toBeNull();
  });

  it('drops 查看统计 from the Agent org panel when 统计 is not provided', async () => {
    const shown = (provides?: (tab: string) => boolean) => render(
      <OrgDomainProvider source={previewSource(provides)}>
        <AgentOrgPanel agentId="agent-x" agentName="X" dispatch={() => undefined} />
      </OrgDomainProvider>,
    );
    const first = shown((tab) => tab !== 'stats');
    await screen.findByRole('button', { name: '加入组织' });
    expect(screen.queryByRole('button', { name: '查看统计' })).toBeNull();
    expect(screen.queryByRole('button', { name: '添加可以发送的对象' })).toBeNull();
    first.unmount();
    shown();
    await screen.findByRole('button', { name: '加入组织' });
    expect(screen.getByRole('button', { name: '查看统计' })).toBeInTheDocument();
  });
});
