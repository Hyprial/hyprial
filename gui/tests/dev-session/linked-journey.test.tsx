import { useCallback, useMemo, useReducer, useRef } from 'react';
import { completeFirstRun } from '../support/first-run-journey';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../../src/App';
import { SessionToolbar } from '../../src/dev/SessionToolbar';
import { createInitialSession } from '../../src/dev/session/initial-session';
import { sessionReducer } from '../../src/dev/session/session-reducer';
import {
  selectAgentSeed,
  selectChatSeed,
  selectChatTargets,
  selectConnections,
  selectFirstRunState,
  selectMachineModel,
  selectOrgModel,
  selectTopologyModel,
} from '../../src/dev/session/session-selectors';
import { createSessionAgentGateway } from '../../src/dev/session/linked-gateways';
import { createFixtureFirstRunActions } from '../../src/dev/first-run-capabilities';
import { legacyOrgSource } from '../../src/dev/legacy-org-source';
import { buildOrgViewModel, type OrgViewModel } from '../../src/pages/org/org-view-model';
import { useChatContractRuntime } from '../../src/dev/mock/use-contract-runtime';
import {
  FIXTURE_AGENTS,
  FIXTURE_CONNECTIONS,
  FIXTURE_THREADS,
} from '../../src/dev/fixtures/demo';
import type { LinkedSession, SessionAction } from '../../src/dev/session/session-types';
import type { AppAction } from '../../src/state/types';

const DEMO_BASELINE = {
  targets: FIXTURE_AGENTS,
  connections: FIXTURE_CONNECTIONS,
  threads: FIXTURE_THREADS,
};

/**
 * The linked development composition (same contract as WP1's
 * LinkedPreviewComposition harness) plus the development SessionToolbar bound
 * to the same session, so identity switches, the machine add and the
 * communication toggle stay drivable without the floating tools layer.
 */
function LinkedJourneyComposition() {
  const [session, dispatch] = useReducer(sessionReducer, undefined, createInitialSession);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const dispatchSession = useCallback((action: SessionAction) => dispatch(action), [dispatch]);
  const sessionGateway = useMemo(
    () => createSessionAgentGateway(() => sessionRef.current as LinkedSession, dispatchSession),
    [dispatchSession],
  );
  const firstRunChat = useChatContractRuntime('success');
  // The onboarding controller resets its whole flow whenever the actions
  // identity changes, so the capability set and both adapters are built once
  // and read the live session through refs and getters instead.
  const firstRunActions = useMemo(
    () =>
      createFixtureFirstRunActions(
        'simulate',
        (action: AppAction) => dispatchSession(action as SessionAction),
      ),
    [dispatchSession],
  );
  const orgSource = useMemo(() => {
    const model = () =>
      sessionRef.current
        ? selectOrgModel(sessionRef.current)
        : buildOrgViewModel('unavailable');
    const live: OrgViewModel = {
      get scene() { return model().scene; },
      get createEnabled() { return model().createEnabled; },
      get inviteEnabled() { return model().inviteEnabled; },
      get joinEnabled() { return model().joinEnabled; },
      get members() { return model().members; },
      get membersEmptyText() { return model().membersEmptyText; },
      get showMembers() { return model().showMembers; },
      get showCreate() { return model().showCreate; },
      get showInvite() { return model().showInvite; },
      get showJoin() { return model().showJoin; },
      get invitation() { return model().invitation; },
      get join() { return model().join; },
      get actions() { return model().actions; },
    };
    return legacyOrgSource(
      live,
      (action) => dispatchSession(action as SessionAction),
      (action) => {
        const current = sessionRef.current;
        if (!current) return undefined;
        const next = sessionReducer(current, action);
        if (next === current) return '当前操作不可用';
        if (action.type === 'org-join' && next.firstRun.error) return next.firstRun.error;
        return undefined;
      },
      undefined,
      () => sessionRef.current,
    );
  }, [dispatchSession]);
  const mockCapabilitiesFactory = useCallback(
    () =>
      Object.freeze({
        firstRun: firstRunActions,
        firstRunWizardEnabled: true,
        orgSource,
        orgSourceKind: 'isolated' as const,
      }),
    [firstRunActions, orgSource],
  );
  return (
    <>
      <App
        baseline={DEMO_BASELINE}
        capabilitiesFactory={mockCapabilitiesFactory}
        firstRunChat={firstRunChat}
        stateSeed={{
          firstRun: selectFirstRunState(session),
          targets: selectChatTargets(session),
          connections: selectConnections(session),
          threads: selectChatSeed(session)?.threads,
        }}
        orgModel={selectOrgModel(session)}
        agentSeed={selectAgentSeed(session)}
        agentGateway={sessionGateway}
        machineModel={selectMachineModel(session)}
        topologyModel={selectTopologyModel(session)}
        onFirstRunAction={(action) => dispatchSession(action as SessionAction)}
      />
      <SessionToolbar session={session} onAction={dispatchSession} />
    </>
  );
}



async function joinForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('tab', { name: '邀请' }));
  await user.click(screen.getByRole('tab', { name: '用邀请链接加入' }));
  return screen.getByLabelText(/^邀请链接/, { selector: 'input' });
}
async function createOrg(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /^组织切换器/ }));
  await user.click(screen.getByRole('menuitem', { name: '新建组织' }));
  await user.type(screen.getByLabelText(/^组织名称/), 'design-team');
  await user.click(screen.getByRole('button', { name: '创建' }));
  const dialog = await screen.findByRole('dialog', { name: '组织已创建' });
  await user.click(within(dialog).getByRole('button', { name: '设为当前组织' }));
}

describe('linked development session', () => {
  it('J1 V-02 completes in four steps and produces one service, Squire and this computer', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);

    await completeFirstRun(user);

    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '开始', level: 1 })).toBeInTheDocument();
    expect(
      screen.queryByText(/本地首次设置流程已完成/),
    ).not.toBeInTheDocument();
    // No fifth step and no extra button.
    expect(screen.queryByRole('heading', { name: '完成' })).toBeNull();
    // The session toolbar is present in development and switches identity.
    expect(
      screen.getByRole('button', { name: '切换为受邀成员' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '切换为创建者' })).toBeInTheDocument();

    // J1 products reach the product pages: one Agent (Squire) and one machine.
    await user.click(screen.getByRole('button', { name: 'Agent' }));
    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(1);
    expect(screen.getByText('Squire')).toBeInTheDocument();
  }, 20_000);

  it('J2 V-03 creates one more Agent from the linked session and shows both states', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);
    await completeFirstRun(user);

    await user.click(screen.getByRole('button', { name: 'Agent' }));
    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(1);
    expect(screen.queryByText(/显示 \d+ \/ \d+ 个 Agent/)).toBeNull();

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    await user.type(screen.getByLabelText('名称'), '整理助手');
    await user.type(screen.getByLabelText('职责'), '整理');
    expect(screen.queryByLabelText('模型服务')).toBeNull();
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    await screen.findByTestId('agent-detail-panel');
    await user.click(screen.getByRole('link', { name: 'Agent' }));
    expect(screen.getAllByTestId(/^contact-agent-/)).toHaveLength(2);
    expect(screen.queryByText(/显示 \d+ \/ \d+ 个 Agent/)).toBeNull();

    await user.click(screen.getByText('整理助手'));
    const detail = document.querySelector('[data-selected="true"]') as HTMLElement;
    expect(detail).not.toHaveTextContent('模型服务');
    expect(detail).not.toHaveTextContent('暂无 Auth 状态查询。');
    expect(detail).toHaveTextContent('未连接渠道');
  }, 20_000);

  it('J5 V-01b V-05 invitee joins the external org and never sees invite or create', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);
    await completeFirstRun(user);

    await user.click(screen.getByRole('button', { name: '切换为受邀成员' }));
    await user.click(screen.getByRole('button', { name: '组织' }));
    expect(screen.queryByRole('heading', { name: '创建组织' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '邀请成员' })).toBeNull();

    await user.type(await joinForm(user), 'GUI-LINK-001');
    await user.click(screen.getByRole('button', { name: '加入' }));

    expect(screen.queryByRole('heading', { name: '创建组织' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '邀请成员' })).toBeNull();
    await user.click(screen.getByRole('tab', { name: '成员' }));
    expect(screen.getByRole('list', { name: '组织成员' })).toBeInTheDocument();
  }, 20_000);

  it('J5 V-01a V-06 owner invites, invitee joins with the owner code, owner sees two members', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);
    await completeFirstRun(user);

    await user.click(screen.getByRole('button', { name: '组织' }));
    await createOrg(user);
    await user.click(screen.getByRole('tab', { name: '邀请' }));
    await user.type(screen.getByLabelText(/^成员账号/), 'invitee@example.invalid');
    await user.click(screen.getByRole('button', { name: '生成邀请' }));
    const invitation = await screen.findByRole('dialog');
    await user.click(within(invitation).getByRole('button', { name: '关闭' }));

    await user.click(screen.getByRole('button', { name: '切换为受邀成员' }));
    expect(screen.queryByRole('heading', { name: '邀请成员' })).toBeNull();
    await user.type(await joinForm(user), 'GUI-ORG-001');
    await user.click(screen.getByRole('button', { name: '加入' }));

    await user.click(screen.getByRole('button', { name: '切换为创建者' }));
    await user.click(screen.getByRole('tab', { name: '成员' }));
    const members = screen.getByRole('list', { name: '组织成员' });
    expect(within(members).getAllByRole('listitem')).toHaveLength(2);
  }, 20_000);

  it('J5 rejects an invalid code inline and keeps the input', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);
    await completeFirstRun(user);

    await user.click(screen.getByRole('button', { name: '组织' }));
    const input = await joinForm(user);
    await user.type(input, 'WRONG-CODE');
    await user.click(screen.getByRole('button', { name: '加入' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('邀请码无效');
    expect(input).toHaveValue('');
  }, 20_000);

  it('J6 V-07 V-08 V-09 continues into machines, topology and the recent record', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);
    await completeFirstRun(user);

    await user.click(screen.getByRole('button', { name: '组织' }));
    await createOrg(user);

    await user.click(screen.getByRole('tab', { name: '成员' }));
    await user.click(screen.getByRole('tab', { name: '设备' }));
    const machines = document.querySelector('.org-machines') as HTMLElement;
    expect(machines).toHaveTextContent('本机');
    expect(machines).toHaveTextContent('这台电脑');
    expect(within(machines).queryByRole('button', { name: /移除|删除/ })).toBeNull();

    await user.click(screen.getByRole('button', { name: '加入第二台电脑' }));
    expect(
      document.querySelectorAll('.org-machines [data-machine-id]'),
    ).toHaveLength(2);

    // Identity switch keeps the machines.
    await user.click(screen.getByRole('button', { name: '切换为受邀成员' }));
    expect(
      document.querySelectorAll('.org-machines [data-machine-id]'),
    ).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: '切换为创建者' }));

    // Topology: members + machines nodes, one edge, Inspect and Monitor.
    await user.click(screen.getByRole('tab', { name: '拓扑' }));
    const topology = document.querySelector('.org-topology') as HTMLElement;
    expect(topology.querySelectorAll('[data-topology-node]')).toHaveLength(3);
    await user.click(
      within(topology).getByRole('button', { name: '通信视图' }),
    );
    const edge = topology.querySelector('[data-topology-edge]') as HTMLElement;
    expect(edge).not.toBeNull();
    await user.click(edge);
    const inspect = await screen.findByRole('dialog', { name: /Inspect/ });
    for (const field of ['发起方', '接收方', '状态', '时间']) {
      expect(inspect).toHaveTextContent(field);
    }
    expect(inspect.textContent).not.toMatch(/https?:\/\//);
    await user.click(
      within(inspect).getByRole('button', { name: '关闭 Inspect' }),
    );

    await user.click(within(topology).getByRole('button', { name: 'Monitor' }));
    const monitor = await screen.findByRole('dialog', { name: '最近通信记录' });
    expect(monitor).toHaveTextContent('最近通信记录');
    expect(monitor).toHaveTextContent('发起方');
    expect(monitor).toHaveTextContent('接收方');
    await user.click(
      within(monitor).getByRole('button', { name: '关闭 Monitor' }),
    );

    // Communication off removes the communication toggle and Monitor; no note replaces them.
    await user.click(screen.getByRole('button', { name: '切换通信为不可用' }));
    const afterOff = document.querySelector('.org-topology') as HTMLElement;
    expect(within(afterOff).queryByRole('button', { name: '通信视图' })).toBeNull();
    expect(within(afterOff).queryByRole('button', { name: 'Monitor' })).toBeNull();
    expect(afterOff).not.toHaveTextContent('不可用');
  }, 20_000);

  it('V-05 has no Agent 设置 entry when the linked service has no port for it', async () => {
    const user = userEvent.setup({ delay: null });
    render(<LinkedJourneyComposition />);
    await completeFirstRun(user);

    await user.click(
      screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }),
    );
    expect(screen.getByRole('menuitem', { name: '用户设置' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: 'Agent 设置' })).toBeNull();
    expect(document.querySelector('[data-page="connections"]')).toBeNull();
    expect(document.querySelector('[data-module]')).toBeNull();
  }, 20_000);
});
