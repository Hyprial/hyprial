/*
 * Decision record (PR 1f/F2): the organization area reports an `unavailable`
 * result as one notification (subject = the control label, warning tone,
 * center source `method · CODE`) and never as page text; actions the source
 * does not implement have no control; the 组织 destination exists only when
 * the composition has an org source; links to other destinations render only
 * when that destination is available.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { App } from '../../../src/App';
import { PageAvailabilityProvider } from '../../../src/app/page-availability';
import { createBackendConnection } from '../../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../../src/app/capabilities';
import { HttpError, TransportError, type RequestDiagnostics } from '../../../src/client';
import { buildOrgDevPreview, type OrgDevScenario } from '../../../src/dev/org/scenarios';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { OrgDomainProvider, type OrgDomainSource } from '../../../src/features/org/impl/domain/context';
import { createOrgLiveSource } from '../../../src/features/org/impl/domain/live-source';
import { orgMethodRefusal } from '../../../src/features/org/impl/api/method-access';
import { UNWIRED_SOURCE } from '../../../src/features/org/impl/domain/context';
import type { OrgGateway, OrgResult } from '../../../src/features/org/impl/domain/types';
import { OrgReminderCards, OrgWorkspace } from '../../../src/features/org';
import { authenticatedTestActions } from '../../support/demo-baseline';
import { expectUnavailableToast } from '../../support/notify-harness';
import { ORG_COPY } from '../../../src/features/org/impl/domain/copy';
import { orgBackendFor } from '../../support/org-backend';
import { openOrgTab, openRail, orgLive, stubRadixPointerApis } from './org-render';

beforeAll(stubRadixPointerApis);

const NEVER_IMPLEMENTED = [
  'claimInvite', 'renameOrg', 'listTemplates', 'previewImport',
  'validateConfig', 'publishCandidate', 'adoptCandidate', 'decideMigration',
  'checkMigration', 'planMigration',
  'submitMigration', 'startMigration', 'cancelMigration', 'rollbackMigration',
] as const;

type Override = Partial<Record<keyof OrgGateway, unknown>>;
type Wrap = (base: OrgGateway, store: Parameters<OrgDomainSource['createGateway']>[0]) => Override;

function previewSource(
  scenario: Exclude<OrgDevScenario, 'off'>,
  { strip = false, override = {}, refusal, wrap }: {
    readonly strip?: boolean;
    readonly override?: Override;
    readonly wrap?: Wrap;
    readonly refusal?: OrgDomainSource['refusal'];
  } = {},
): OrgDomainSource {
  const preview = buildOrgDevPreview(scenario, { latencyMs: 0 });
  if (!preview) throw new Error(`no preview for ${scenario}`);
  return {
    ...preview.source,
    ...(refusal ? { refusal } : {}),
    createGateway: (store) => {
      const gateway: Record<string, unknown> = { ...preview.source.createGateway(store) };
      if (strip) for (const name of ['setCurrentOrg', ...NEVER_IMPLEMENTED]) delete gateway[name];
      const base = gateway as unknown as OrgGateway;
      return { ...gateway, ...override, ...(wrap ? wrap(base, store) : {}) } as unknown as OrgGateway;
    },
  };
}

function renderPreview(source: OrgDomainSource, scenario: Exclude<OrgDevScenario, 'off'> = 'owner') {
  const preview = buildOrgDevPreview(scenario, { latencyMs: 0 });
  if (!preview) throw new Error(`no preview for ${scenario}`);
  const user = userEvent.setup();
  const view = render(
    <OrgDomainProvider source={source}>
      <FixtureApp
        dataMode="fixture"
        firstRunMode="complete"
        agentSeed={preview.agentSeed}
        agentGateway={preview.agentGateway}
        chatSeed={preview.chatSeed}
      />
    </OrgDomainProvider>,
  );
  return { user, view };
}

const unavailable = async (): Promise<OrgResult<never>> => ({ kind: 'unavailable' });

describe('an unavailable result is one notification and nothing else', () => {
  it('createOrg: toast with the control label, control enabled, dialog stays, banner and dialog empty', async () => {
    const { user } = renderPreview(previewSource('owner', { override: { createOrg: unavailable } }));
    await openRail(user, '组织');
    await user.click(screen.getByRole('button', { name: /^组织切换器：hyprial-dev/ }));
    await user.click(screen.getByRole('menuitem', { name: '新建组织' }));
    const dialog = screen.getByRole('dialog', { name: '新建组织' });
    await user.type(within(dialog).getByLabelText(/^组织名称/), 'design-team');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));

    await waitFor(() => expectUnavailableToast('创建暂不可用。'));
    expect(within(dialog).getByRole('button', { name: '创建' })).toBeEnabled();
    expect(within(dialog).queryByRole('alert')).toBeNull();
    expect(orgLive()).toBeEmptyDOMElement();
    expect(screen.queryByRole('dialog', { name: '组织已创建' })).toBeNull();
  });

  it('a refused refresh keeps the data, raises one toast with the catalog source and no inline line', async () => {
    const refusal = { code: 'METHOD_NOT_AVAILABLE', source: 'org.list' };
    const { user } = renderPreview(
      previewSource('owner', { override: { refresh: async () => ({ kind: 'unavailable', refusal }) } }),
    );
    await openRail(user, '组织');
    const heading = screen.getByRole('heading', { level: 1, name: 'hyprial-dev' });
    await user.click(within(document.querySelector('[data-org-section="overview"]') as HTMLElement)
      .getByRole('button', { name: /刷新/ }));

    await waitFor(() => expectUnavailableToast('刷新暂不可用。'));
    expect(heading).toBeInTheDocument();
    expect(orgLive()).toBeEmptyDOMElement();
    expect(document.body).not.toHaveTextContent(/读取失败|读取组织列表未完成/);
    await user.click(screen.getByRole('button', { name: /通知中心/ }));
    expect(within(screen.getByRole('dialog', { name: '通知中心' })).getByText(/org\.list · METHOD_NOT_AVAILABLE/))
      .toBeInTheDocument();
  });

  it('a failed result still lands in the page banner and raises no toast', async () => {
    const { user } = renderPreview(
      previewSource('owner', { override: { refresh: async () => ({ kind: 'failed', message: '读取组织列表未完成：网络请求失败。' }) } }),
    );
    await openRail(user, '组织');
    await user.click(within(document.querySelector('[data-org-section="overview"]') as HTMLElement)
      .getByRole('button', { name: /刷新/ }));

    expect(await within(orgLive()).findByText('读取组织列表未完成：网络请求失败。')).toBeInTheDocument();
    expect(document.querySelectorAll('.toast-host .toast')).toHaveLength(0);
  });

  it('a flow refused by the catalog is decided before its dialog opens: 新建 in 文件', async () => {
    const { user } = renderPreview(previewSource('owner', {
      refusal: (method) => method === 'createEntry' ? { code: 'WRITE_METHOD_UNVERIFIED', source: 'orgfs.write' } : null,
    }));
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: '新建' }));

    await waitFor(() => expectUnavailableToast('新建暂不可用。'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: '新建' })).toBeEnabled();
    expect(orgLive()).toBeEmptyDOMElement();
  });

  it('a refused 设为当前组织 (defined but refused) notifies and leaves the current organization alone', async () => {
    const { user } = renderPreview(previewSource('owner', { override: { setCurrentOrg: unavailable } }));
    await openRail(user, '组织');
    await user.click(screen.getByLabelText('查看组织'));
    await user.click(screen.getByRole('option', { name: 'acme-lab' }));
    const tags = screen.getByRole('group', { name: '组织状态' });
    const button = within(tags).getByRole('button', { name: '设为当前组织' });
    await user.click(button);

    await waitFor(() => expectUnavailableToast('设为当前组织暂不可用。'));
    expect(within(screen.getByRole('group', { name: '组织状态' })).getByRole('button', { name: '设为当前组织' })).toBeEnabled();
    expect(screen.getByRole('button', { name: /^组织切换器：hyprial-dev/ })).toBeInTheDocument();
    expect(orgLive()).toBeEmptyDOMElement();
  });
});

const ORG_LIST = {
  orgs: [{ org: 'group-hyprial-dev', spaceId: 'space-team', role: 'owner', devices: 1, state: 'active' }],
  leftOrgs: [],
};

function diagnostics(method: string, dispatch: RequestDiagnostics['dispatch']): RequestDiagnostics {
  return { method, requestId: null, status: null, dispatch, catalogDigest: null, attempts: 0, startedAt: 0, durationMs: 0 };
}

function formalApp(extraMethods: readonly string[] = [], listError?: () => Error) {
  const calls: string[] = [];
  const methods = [
    { name: 'org.list', callable: true, state: 'available', mutates: false },
    { name: 'org.create', callable: true, state: 'available', mutates: true },
    ...extraMethods.map((name) => ({ name, callable: true, state: 'available', mutates: false })),
  ] as const;
  const connection = createBackendConnection({
    async invoke<TResult>(method: string): Promise<TResult> {
      calls.push(method);
      if (method === 'org.list') {
        if (listError) throw listError();
        return ORG_LIST as TResult;
      }
      throw new TransportError('configuration', 'METHOD_NOT_FOUND', 'unexpected', {
        method, requestId: null, status: null, dispatch: 'not-dispatched', catalogDigest: null,
        attempts: 0, startedAt: 0, durationMs: 0,
      });
    },
  }, {
    interfaceVersion: 1, driver: 'contract-mock', phase: 'ready', generation: 'g1', catalogDigest: 'sha256:c',
    methods, verifiedWrites: [],
  });
  const capabilities = {
    ...createConnectedGuiCapabilities(connection),
    firstRun: authenticatedTestActions(),
    firstRunWizardEnabled: false,
  };
  return { connection, capabilities, calls };
}

describe('formal composition over the live org source', () => {
  it('a refused write opens no dialog, raises exactly one toast, keeps the control enabled and the page non-empty', async () => {
    const { capabilities, calls } = formalApp();
    const user = userEvent.setup();
    render(<App capabilities={capabilities} initialPage="org" />);

    expect(await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^组织切换器/ }));
    const create = screen.getByRole('menuitem', { name: '新建组织' });
    await user.click(create);

    await waitFor(() => expectUnavailableToast('新建组织暂不可用。'));
    expect(screen.queryByRole('dialog', { name: '新建组织' })).toBeNull();
    expect(orgLive()).toBeEmptyDOMElement();
    expect(screen.queryByTestId('org-write-unavailable')).toBeNull();
    expect(document.body).not.toHaveTextContent(/WRITE_METHOD_UNVERIFIED|org\.create/);
    expect(calls).not.toContain('org.create');
    expect(screen.getByRole('tablist', { name: '组织子页签' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^组织切换器/ }));
    expect(screen.getByRole('menuitem', { name: '新建组织' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /通知中心/ }));
    expect(within(screen.getByRole('dialog', { name: '通知中心' })).getByText(/org\.create · WRITE_METHOD_UNVERIFIED/))
      .toBeInTheDocument();
  });

  it('renders none of the controls the live source never implements', async () => {
    const { capabilities } = formalApp(['org.network']);
    const user = userEvent.setup();
    render(<App capabilities={capabilities} initialPage="org" />);
    await screen.findByRole('heading', { level: 1, name: 'hyprial-dev' });

    for (const name of ['设为当前组织', '采信', '校验', '发布候选', '从模板导入', '改名']) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
    await openOrgTab(user, '成员');
    expect(screen.queryByRole('tab', { name: '待审批' })).toBeNull();
    await openOrgTab(user, '设置');
    expect(screen.queryByRole('button', { name: '改名' })).toBeNull();
    await user.click(screen.getByRole('tab', { name: '版本' }));
    for (const name of ['采信', '从模板导入', '发布候选', '校验']) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
    await openOrgTab(user, '邀请');
    expect(screen.queryByRole('button', { name: '加入' })).toBeNull();
    await user.click(screen.getByRole('button', { name: /^组织切换器/ }));
    expect(screen.getByRole('menuitemradio', { name: /hyprial-dev/ })).toBeInTheDocument();
  });

  it('the live gateway defines none of the 14 unsupported actions and gates every defined write on the catalog', () => {
    const access = (method: string) => method === 'org.invite'
      ? { available: false as const, code: 'WRITE_METHOD_UNVERIFIED' }
      : { available: true as const };
    const source = createOrgLiveSource(orgBackendFor({ request: async () => ({}) }), access);
    const gateway = source.createGateway({ get: () => source.initial, update: () => undefined });
    for (const name of NEVER_IMPLEMENTED) expect(gateway[name], name).toBeUndefined();
    expect(source.refusal?.('createInvite')).toEqual({ code: 'WRITE_METHOD_UNVERIFIED', source: 'org.invite' });
    expect(source.refusal?.('createOrg')).toBeNull();
    expect(orgMethodRefusal(undefined, 'createInvite')).toBeNull();
  });

  it('a refresh refused before dispatch sends nothing and leaves the snapshot untouched', async () => {
    const requests: string[] = [];
    const access = () => ({ available: false as const, code: 'METHOD_NOT_FOUND' });
    const source = createOrgLiveSource(
      orgBackendFor({ request: async (method) => { requests.push(method); return {}; } }),
      access,
    );
    let snapshot = source.initial;
    const gateway = source.createGateway({ get: () => snapshot, update: (next) => { snapshot = next(snapshot); } });
    await expect(gateway.refresh(null)).resolves.toEqual({
      kind: 'unavailable',
      refusal: { code: 'METHOD_NOT_FOUND', source: 'org.list' },
    });
    expect(requests).toEqual([]);
    expect(snapshot).toBe(source.initial);
  });

  it('no 组织 entry when the catalog refuses org.list', () => {
    const connection = createBackendConnection({ invoke: async () => ({}) as never }, {
      interfaceVersion: 1, driver: 'contract-mock', phase: 'ready', generation: 'g1', catalogDigest: 'sha256:c',
      methods: [{ name: 'org.create', callable: true, state: 'available', mutates: true }],
    });
    const capabilities = {
      ...createConnectedGuiCapabilities(connection),
      firstRun: authenticatedTestActions(),
      firstRunWizardEnabled: false,
    };
    expect(capabilities.orgSource).toBeUndefined();
    render(<App capabilities={capabilities} />);
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
  });
});

describe('the first read of the organization list', () => {
  it('refused before dispatch: no 组织 entry, no loading text, no toast', async () => {
    const { capabilities, calls } = formalApp([], () => new TransportError(
      'configuration', 'METHOD_NOT_FOUND', 'unexpected', diagnostics('org.list', 'not-dispatched'),
    ));
    render(<App capabilities={capabilities} />);

    await waitFor(() => expect(calls).toContain('org.list'));
    await waitFor(() => expect(screen.queryByRole('button', { name: '组织' })).toBeNull());
    expect(document.body).not.toHaveTextContent('正在读取组织列表');
    expect(document.querySelectorAll('.toast-host .toast')).toHaveLength(0);
  });

  it('dispatched and failed: the page says so and offers 刷新', async () => {
    const { capabilities } = formalApp([], () => new HttpError(
      'INTERNAL', 'server error', 500, diagnostics('org.list', 'dispatched'),
    ));
    render(<App capabilities={capabilities} initialPage="org" />);

    expect(await screen.findByText(/组织列表读取失败/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /刷新/ })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('正在读取组织列表');
  });
});

describe('controls follow the source: absent when the action is not implemented, present when it is', () => {
  async function renderOwner(strip: boolean) {
    const { user } = renderPreview(previewSource('owner', { strip }));
    await openRail(user, '组织');
    return user;
  }

  it('设为当前组织 and 改名 and the config actions', async () => {
    const withActions = await renderOwner(false);
    await withActions.click(screen.getByLabelText('查看组织'));
    await withActions.click(screen.getByRole('option', { name: 'acme-lab' }));
    expect(within(screen.getByRole('group', { name: '组织状态' })).getByRole('button', { name: '设为当前组织' })).toBeInTheDocument();
    await withActions.click(screen.getByLabelText('查看组织'));
    await withActions.click(screen.getByRole('option', { name: 'hyprial-dev' }));
    await openOrgTab(withActions, '设置');
    expect(screen.getByRole('button', { name: '改名' })).toBeInTheDocument();
    await withActions.click(screen.getByRole('tab', { name: '版本' }));
    expect(screen.getByRole('button', { name: '从模板导入' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '采信' })).toBeInTheDocument();
  });

  it('the same screens without the optional actions render none of them', async () => {
    const user = await renderOwner(true);
    await user.click(screen.getByLabelText('查看组织'));
    await user.click(screen.getByRole('option', { name: 'acme-lab' }));
    expect(within(screen.getByRole('group', { name: '组织状态' })).queryByRole('button', { name: '设为当前组织' })).toBeNull();
    await user.click(screen.getByLabelText('查看组织'));
    await user.click(screen.getByRole('option', { name: 'hyprial-dev' }));
    await openOrgTab(user, '设置');
    expect(screen.queryByRole('button', { name: '改名' })).toBeNull();
    await user.click(screen.getByRole('tab', { name: '版本' }));
    for (const name of ['从模板导入', '采信', '发布候选', '校验', '编辑']) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
  });

  it('inbound migration decisions are absent without decideMigration and present with it', async () => {
    for (const strip of [false, true]) {
      const { user, view } = renderPreview(previewSource('migration', { strip }), 'migration');
      await openRail(user, '组织');
      await openOrgTab(user, '成员');
      await user.click(screen.getByRole('tab', { name: '设备' }));
      await user.click(screen.getByRole('button', { name: /^查看这台电脑/ }));
      const rows = document.querySelectorAll('[data-org-inbound]');
      expect(rows.length).toBeGreaterThan(0);
      await user.click(within(rows[0] as HTMLElement).getByRole('button', { name: /打开迁移单/ }));
      const dialog = screen.getByRole('dialog', { name: /迁移单/ });
      if (strip) {
        expect(within(dialog).queryByRole('button', { name: '同意' })).toBeNull();
        expect(within(dialog).queryByRole('button', { name: '拒绝' })).toBeNull();
      } else {
        expect(within(dialog).getByRole('button', { name: '同意' })).toBeInTheDocument();
      }
      view.unmount();
    }
  });

  it('received-invite entry is absent without claimInvite', async () => {
    const { user } = renderPreview(previewSource('invites', { strip: true }), 'invites');
    await openRail(user, '组织');
    await openOrgTab(user, '邀请');
    expect(screen.queryByRole('tab', { name: /^我收到的邀请/ })).toBeNull();
  });

  it('the Agent block of the Agent page renders only the blocks that have controls or data', async () => {
    const { user } = renderPreview(previewSource('owner', { strip: true }));
    await openRail(user, 'Agent');
    await user.click(screen.getByLabelText('组织'));
    await user.click(screen.getByRole('option', { name: '全部' }));
    await user.click(screen.getByTestId('contact-agent-ops'));
    await user.click(screen.getByRole('tab', { name: '所属组织' }));
    expect(screen.getByRole('button', { name: '加入组织' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '迁移到组织内的机器' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '迁移' })).toBeNull();
  });
});

describe('the workspace renders nothing without a source', () => {
  it('OrgWorkspace under UNWIRED_SOURCE is empty and the gateway answers without text', async () => {
    const { container } = render(
      <OrgDomainProvider source={UNWIRED_SOURCE}>
        <OrgWorkspace dispatch={vi.fn()} />
      </OrgDomainProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe('links to other destinations render only when the target is available', () => {
  function renderWorkspace(hidden: readonly ('contacts' | 'chat' | 'org')[]) {
    const user = userEvent.setup();
    render(
      <OrgDomainProvider source={previewSource('owner')}>
        <PageAvailabilityProvider hiddenPages={new Set(hidden)}>
          <OrgWorkspace dispatch={vi.fn()} />
        </PageAvailabilityProvider>
      </OrgDomainProvider>,
    );
    return user;
  }

  it('overview and members: unavailable destinations hide their links', async () => {
    const hiddenUser = renderWorkspace(['contacts', 'chat']);
    expect(screen.queryByRole('button', { name: '在此组织新建 Agent' })).toBeNull();
    await openOrgTab(hiddenUser, '成员');
    expect(screen.queryByRole('button', { name: '在此组织新建' })).toBeNull();
    await hiddenUser.click(screen.getByRole('button', { name: '查看成员陈墨' }));
    expect(screen.queryByRole('button', { name: '发消息' })).toBeNull();
    expect(screen.queryByRole('button', { name: '在 Agent 页打开' })).toBeNull();
  });

  it('the same screens keep communication links when the targets are available', async () => {
    const user = renderWorkspace([]);
    expect(screen.queryByRole('button', { name: '在此组织新建 Agent' })).toBeNull();
    await openOrgTab(user, '成员');
    expect(screen.queryByRole('button', { name: '在此组织新建' })).toBeNull();
    await user.click(screen.getByRole('button', { name: '查看成员陈墨' }));
    expect(screen.getByRole('button', { name: '发消息' })).toBeInTheDocument();
  });

  it('start-page reminder cards need the 组织 destination', () => {
    render(
      <OrgDomainProvider source={previewSource('invites')}>
        <PageAvailabilityProvider hiddenPages={new Set(['org'])}>
          <OrgReminderProbe />
        </PageAvailabilityProvider>
      </OrgDomainProvider>,
    );
    expect(screen.queryByRole('button', { name: '查看邀请' })).toBeNull();
  });
});

function OrgReminderProbe() {
  return <OrgReminderCards dispatch={vi.fn()} />;
}

describe('rewritten org strings', () => {
  it('the member invite line is the product rule, without a reason', async () => {
    const { user } = renderPreview(previewSource('member'), 'member');
    await openRail(user, '组织');
    await openOrgTab(user, '邀请');
    expect(screen.getByText('只有工作组织的所有者可以发出邀请。')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('暂不支持');
  });
});


const nthCallUnavailable = (n: number, run: (...args: never[]) => Promise<OrgResult<unknown>>) => {
  let calls = 0;
  return async (...args: never[]): Promise<OrgResult<unknown>> => {
    calls += 1;
    return calls >= n ? { kind: 'unavailable' } : run(...args);
  };
};

describe('the toast subject is the label of the button that was pressed', () => {
  it('改名: 确认改名', async () => {
    const { user } = renderPreview(previewSource('owner', { override: { renameOrg: unavailable } }));
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('button', { name: '改名' }));
    const dialog = screen.getByRole('dialog', { name: '改名 hyprial-dev' });
    const field = within(dialog).getByLabelText(/新名称/);
    await user.clear(field);
    await user.type(field, 'hyprial-core');
    await user.click(within(dialog).getByRole('button', { name: '确认改名' }));
    await waitFor(() => expectUnavailableToast('确认改名暂不可用。'));
    expect(within(dialog).getByRole('button', { name: '确认改名' })).toBeEnabled();
  });

  it('采信: 确认采信', async () => {
    const { user } = renderPreview(previewSource('owner', { override: { adoptCandidate: unavailable } }));
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('tab', { name: '版本' }));
    await user.click(screen.getByRole('button', { name: '采信' }));
    const dialog = screen.getByRole('dialog', { name: /^采信版本/ });
    await user.click(within(dialog).getByRole('button', { name: '确认采信' }));
    await waitFor(() => expectUnavailableToast('确认采信暂不可用。'));
  });

  it('从模板导入: 下一步：预览', async () => {
    const { user } = renderPreview(
      previewSource('template', { override: { previewImport: unavailable } }),
      'template',
    );
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('tab', { name: '版本' }));
    await user.click(screen.getByRole('button', { name: '从模板导入' }));
    const dialog = screen.getByRole('dialog', { name: '从模板导入' });
    await user.click(within(dialog).getAllByRole('radio')[0] as HTMLElement);
    await user.click(within(dialog).getByRole('button', { name: '下一步：预览' }));
    await waitFor(() => expectUnavailableToast('下一步：预览暂不可用。'));
    expect(screen.getByRole('dialog', { name: '从模板导入' })).toBeInTheDocument();
  });

  it.each([
    ['同意', '确认同意'],
    ['拒绝', '确认拒绝'],
  ])('迁入请求 %s: %s', async (opener, confirm) => {
    const { user } = renderPreview(
      previewSource('migration', { override: { decideMigration: unavailable } }),
      'migration',
    );
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('tab', { name: '设备' }));
    await user.click(screen.getByRole('button', { name: /^查看这台电脑/ }));
    const rows = document.querySelectorAll('[data-org-inbound]');
    await user.click(within(rows[0] as HTMLElement).getByRole('button', { name: /打开迁移单/ }));
    await user.click(within(screen.getByRole('dialog', { name: /迁移单/ })).getByRole('button', { name: opener }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: confirm }));
    await waitFor(() => expectUnavailableToast(`${confirm}暂不可用。`));
  });

  it('移除成员: 再次确认移除', async () => {
    const { user } = renderPreview(previewSource('owner', {
      wrap: (base) => ({ removeMember: nthCallUnavailable(2, base.removeMember as never) }),
    }));
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('button', { name: '查看成员陈墨' }));
    await user.click(screen.getByRole('button', { name: '移除成员' }));
    const dialog = screen.getByRole('dialog', { name: '移除成员陈墨？' });
    await user.click(await within(dialog).findByRole('button', { name: '确认移除' }));
    await user.click(await within(dialog).findByRole('button', { name: '再次确认移除' }));
    await waitFor(() => expectUnavailableToast('再次确认移除暂不可用。'));
  });

  it('离开组织: 再次确认离开', async () => {
    const { user } = renderPreview(previewSource('member', {
      wrap: () => ({
        leaveOrg: nthCallUnavailable(2, async () => ({ kind: 'failed', code: 'stale', message: '预览已变化' })),
      }),
    }), 'member');
    await openRail(user, '组织');
    await openOrgTab(user, '设置');
    await user.click(screen.getByRole('tab', { name: '危险操作' }));
    await user.click(screen.getByRole('button', { name: '离开组织' }));
    const dialog = screen.getByRole('dialog', { name: /^离开/ });
    await user.type(within(dialog).getByRole('textbox'), 'acme-lab');
    await user.click(await within(dialog).findByRole('button', { name: '确认离开' }));
    await user.type(within(dialog).getByRole('textbox'), 'acme-lab');
    await user.click(await within(dialog).findByRole('button', { name: '再次确认离开' }));
    await waitFor(() => expectUnavailableToast('再次确认离开暂不可用。'));
  });
});

describe('a refused read or retry leaves no false state behind', () => {
  async function openFile(user: ReturnType<typeof userEvent.setup>, name: string) {
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name }));
  }

  it('历史 refused at dispatch with nothing cached: one toast, no panel, no empty text', async () => {
    const { user } = renderPreview(previewSource('owner', { override: { loadHistory: unavailable } }));
    await openFile(user, 'README.md');
    await user.click(screen.getByRole('button', { name: '历史' }));

    await waitFor(() => expectUnavailableToast('历史暂不可用。'));
    expect(screen.queryByRole('complementary', { name: /^历史/ })).toBeNull();
    expect(document.body).not.toHaveTextContent('没有版本记录');
  });

  it('重新读取 refused keeps the versions on screen and names itself in the toast', async () => {
    let calls = 0;
    const { user } = renderPreview(previewSource('owner', {
      wrap: (_base, store) => ({
        loadHistory: async (orgId: string, spaceId: string, path: string): Promise<OrgResult<unknown>> => {
          calls += 1;
          if (calls > 1) return { kind: 'unavailable' };
          store.update((state) => {
            const detail = state.details[orgId]!;
            const version = { version: 3, modifiedBy: '姚', modifiedAt: '10-30', content: '', contentStatus: 'error' as const };
            return {
              ...state,
              details: { ...state.details, [orgId]: { ...detail, history: { ...detail.history, [`${spaceId}:${path}`]: [version] } } },
            };
          });
          return { kind: 'ok', value: [] };
        },
      }),
    }));
    await openFile(user, 'README.md');
    await user.click(screen.getByRole('button', { name: '历史' }));
    await user.click(within(await screen.findByRole('list', { name: '版本记录' })).getAllByRole('button')[0] as HTMLElement);
    await user.click(screen.getByRole('button', { name: '重新读取' }));

    await waitFor(() => expectUnavailableToast('重新读取暂不可用。'));
    expect(screen.getByRole('list', { name: '版本记录' })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('没有版本记录');
  });

  it('a version whose content read was refused shows the row and neither text nor a body', async () => {
    const { user } = renderPreview(previewSource('owner', {
      wrap: (_base, store) => ({
        loadHistory: async (orgId: string, spaceId: string, path: string): Promise<OrgResult<unknown>> => {
          store.update((state) => {
            const detail = state.details[orgId]!;
            const version = { version: 3, modifiedBy: '姚', modifiedAt: '10-30', content: '', contentStatus: 'refused' as const };
            return {
              ...state,
              details: { ...state.details, [orgId]: { ...detail, history: { ...detail.history, [`${spaceId}:${path}`]: [version] } } },
            };
          });
          return { kind: 'ok', value: [] };
        },
      }),
    }));
    await openFile(user, 'README.md');
    await user.click(screen.getByRole('button', { name: '历史' }));
    await user.click(within(await screen.findByRole('list', { name: '版本记录' })).getAllByRole('button')[0] as HTMLElement);

    expect(screen.getByRole('button', { name: /版本 3/ })).toHaveAttribute('aria-pressed', 'true');
    expect(document.body).not.toHaveTextContent('读取失败');
    expect(screen.queryByRole('button', { name: '重新读取' })).toBeNull();
    expect(screen.queryByLabelText('版本 3 的内容')).toBeNull();
  });

  it('a join refused at dispatch gives the typed link back', async () => {
    const { user } = renderPreview(previewSource('owner', { override: { joinByLink: unavailable } }));
    await openRail(user, '组织');
    await user.click(screen.getByRole('button', { name: '邀请成员' }));
    await user.click(screen.getByRole('tab', { name: ORG_COPY.joinByLink }));
    const link = screen.getByLabelText(/^邀请链接/, { selector: 'input' });
    await user.type(link, 'https://hyprial.ai/join#v2.example-only');
    await user.click(screen.getByRole('button', { name: '加入' }));

    await waitFor(() => expectUnavailableToast('加入暂不可用。'));
    expect(screen.getByLabelText(/^邀请链接/, { selector: 'input' })).toHaveValue('https://hyprial.ai/join#v2.example-only');
  });

  it('新建 offers only the kinds whose method is admitted', async () => {
    const refused = (method: string) => ({ code: 'WRITE_METHOD_UNVERIFIED', source: method });
    const fileRefused = renderPreview(previewSource('owner', {
      refusal: (method) => method === 'createFile' ? refused('orgfs.write') : null,
    }));
    await openRail(fileRefused.user, '组织');
    await openOrgTab(fileRefused.user, '文件');
    await fileRefused.user.click(screen.getByRole('button', { name: '新建' }));
    const folderOnly = screen.getByRole('dialog', { name: '新建' });
    expect(within(folderOnly).queryByRole('radio', { name: '文件' })).toBeNull();
    expect(within(folderOnly).queryByRole('radio', { name: '文件夹' })).toBeNull();
    expect(within(folderOnly).queryByRole('group', { name: '类型' })).toBeNull();
    await fileRefused.user.type(within(folderOnly).getByRole('textbox', { name: /^路径/ }), 'notes');
    await fileRefused.user.click(within(folderOnly).getByRole('button', { name: '新建' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建' })).toBeNull());
    expect(document.querySelectorAll('.toast-host .toast')).toHaveLength(0);
    fileRefused.view.unmount();

    const folderRefused = renderPreview(previewSource('owner', {
      refusal: (method) => method === 'createFolder' ? refused('orgfs.mkdir') : null,
    }));
    await openRail(folderRefused.user, '组织');
    await openOrgTab(folderRefused.user, '文件');
    await folderRefused.user.click(screen.getByRole('button', { name: '新建' }));
    const fileOnly = screen.getByRole('dialog', { name: '新建' });
    expect(within(fileOnly).queryByRole('radio', { name: '文件夹' })).toBeNull();
    expect(within(fileOnly).queryByRole('radio', { name: '文件' })).toBeNull();
    expect(within(fileOnly).queryByRole('group', { name: '类型' })).toBeNull();
    await folderRefused.user.type(within(fileOnly).getByRole('textbox', { name: /^路径/ }), 'notes.md');
    await folderRefused.user.click(within(fileOnly).getByRole('button', { name: '新建' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建' })).toBeNull());
    expect(document.querySelectorAll('.toast-host .toast')).toHaveLength(0);
  });

  it('新建 keeps both kinds when both are admitted', async () => {
    const { user } = renderPreview(previewSource('owner'));
    await openRail(user, '组织');
    await openOrgTab(user, '文件');
    await user.click(screen.getByRole('button', { name: '新建' }));
    const dialog = screen.getByRole('dialog', { name: '新建' });
    expect(within(dialog).getByRole('radio', { name: '文件' })).toBeChecked();
    expect(within(dialog).getByRole('radio', { name: '文件夹' })).not.toBeChecked();
  });

  it('立即重试 refused by the catalog sets nothing busy and toasts once', async () => {
    const { user } = renderPreview(previewSource('owner', {
      refusal: (method) => method === 'retryMachine' ? { code: 'METHOD_NOT_FOUND', source: 'org.publish' } : null,
    }));
    await openRail(user, '组织');
    await openOrgTab(user, '成员');
    await user.click(screen.getByRole('tab', { name: '设备' }));
    const retry = screen.getAllByRole('button', { name: '立即重试' })[0] as HTMLElement;
    await user.click(retry);

    await waitFor(() => expectUnavailableToast('立即重试暂不可用。'));
    expect(retry).toBeEnabled();
    expect(retry).not.toHaveAttribute('aria-busy', 'true');
  });
});

type User = ReturnType<typeof userEvent.setup>;

interface OpenerCase {
  readonly name: string;
  readonly scenario: Exclude<OrgDevScenario, 'off'>;
  readonly methods: readonly string[];
  readonly subject: string;
  readonly open: (user: User) => Promise<void>;
}

const inFiles = (file: string) => async (user: User) => {
  await openRail(user, '组织');
  await openOrgTab(user, '文件');
  await user.click(screen.getByRole('button', { name: file }));
};

const inSettings = (segment: string) => async (user: User) => {
  await openRail(user, '组织');
  await openOrgTab(user, '设置');
  await user.click(screen.getByRole('tab', { name: segment }));
};

const OPENERS: readonly OpenerCase[] = [
  { name: 'leaveOrg', scenario: 'member', methods: ['previewLeave'], subject: '离开组织',
    open: async (user) => { await inSettings('危险操作')(user); await user.click(screen.getByRole('button', { name: '离开组织' })); } },
  { name: 'removeMember', scenario: 'owner', methods: ['previewRemoveMember'], subject: '移除成员',
    open: async (user) => {
      await openRail(user, '组织');
      await openOrgTab(user, '成员');
      await user.click(screen.getByRole('button', { name: '查看成员陈墨' }));
      await user.click(screen.getByRole('button', { name: '移除成员' }));
    } },
  { name: 'createInvite', scenario: 'owner', methods: ['createInvite'], subject: '生成邀请',
    open: async (user) => {
      await openRail(user, '组织');
      await user.click(screen.getByRole('button', { name: '邀请成员' }));
      await user.type(screen.getByLabelText(/成员账号/), 'new@example.invalid');
      await user.click(screen.getByRole('button', { name: '生成邀请' }));
    } },
  { name: 'joinByLink', scenario: 'owner', methods: ['joinByLink'], subject: '加入',
    open: async (user) => {
      await openRail(user, '组织');
      await user.click(screen.getByRole('button', { name: '邀请成员' }));
      await user.click(screen.getByRole('tab', { name: ORG_COPY.joinByLink }));
      await user.type(screen.getByLabelText(/^邀请链接/, { selector: 'input' }), 'https://hyprial.ai/join#v2.x');
      await user.click(screen.getByRole('button', { name: '加入' }));
    } },
  { name: 'repair', scenario: 'partial', methods: ['previewRepair'], subject: '修复',
    open: async (user) => { await openRail(user, '组织'); await user.click(screen.getByRole('button', { name: '修复' })); } },
  { name: 'syncContext', scenario: 'owner', methods: ['syncContext'], subject: '同步组织配置',
    open: async (user) => { await inSettings('版本')(user); await user.click(screen.getByRole('button', { name: '同步组织配置' })); } },
  { name: 'createEntry', scenario: 'owner', methods: ['createEntry'], subject: '新建',
    open: async (user) => { await openRail(user, '组织'); await openOrgTab(user, '文件'); await user.click(screen.getByRole('button', { name: '新建' })); } },
  { name: 'saveFile', scenario: 'owner', methods: ['saveFile'], subject: '编辑',
    open: async (user) => { await inFiles('onboarding.md')(user); await user.click(screen.getByRole('button', { name: '编辑' })); } },
  { name: 'moveEntry', scenario: 'owner', methods: ['moveEntry'], subject: '移动',
    open: async (user) => { await inFiles('onboarding.md')(user); await user.click(screen.getByRole('button', { name: '移动' })); } },
  { name: 'deleteEntry', scenario: 'owner', methods: ['deleteEntry'], subject: '删除',
    open: async (user) => { await inFiles('onboarding.md')(user); await user.click(screen.getByRole('button', { name: '删除' })); } },
  { name: 'loadHistory', scenario: 'owner', methods: ['loadHistory'], subject: '历史',
    open: async (user) => { await inFiles('onboarding.md')(user); await user.click(screen.getByRole('button', { name: '历史' })); } },
];

describe('every opener decides a refusal before it opens anything', () => {
  it.each(OPENERS)('$name: one toast named after the control, no dialog, no panel', async (item) => {
    const refused = new Set<string>(item.methods);
    const { user } = renderPreview(previewSource(item.scenario, {
      refusal: (method) => refused.has(method) ? { code: 'METHOD_NOT_FOUND', source: `catalog.${method}` } : null,
    }), item.scenario);
    await item.open(user);

    await waitFor(() => expectUnavailableToast(`${item.subject}暂不可用。`));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('complementary', { name: /^历史/ })).toBeNull();
  });
});
