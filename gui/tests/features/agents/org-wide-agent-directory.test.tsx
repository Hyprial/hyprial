import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

import { GuiCapabilitiesProvider } from '../../../src/app/capabilities';
import { createFixtureState } from '../../../src/dev/fixtures/fixture-state';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import { createUnwiredGateway } from '../../../src/features/org/impl/domain/unwired';
import { ORG_TEAM, ownerSnapshot } from '../../../src/dev/org/fixture-data';
import { ContactsPage } from '../../../src/pages/ContactsPage';
import { CREATE_NEW_AGENT } from '../../fixtures/agents/agent-mutations';
import { createTestAgentsApi } from '../../unified-backend/agents-test-client';
import { stubFirstRunActions } from '../../support/first-run-actions';

const ids = ['a.' + 'a'.repeat(26), 'a.' + 'b'.repeat(26), 'a.' + 'c'.repeat(26), 'a.' + 'd'.repeat(26)];
const names = ['test-1', 'test-b-1', 'test-b-2', 'test-b-3'];
const records = names.map((name, index) => ({
  id: ids[index], name, ownerLabel: index === 0 ? '小A' : '小B',
  machineLabel: '状态未确认', runState: 'unknown' as const,
  orgIds: [ORG_TEAM], mine: index === 0,
}));

function renderDirectory(orgAgents: typeof records | null, detailId?: string, localReadFails = false, status?: 'ok' | 'partial', chatTargets?: () => Promise<readonly { targetUri: string; targetKind: string; deliverable: boolean; actor: string | null; agentId?: string }[]>) {
  const local = {
    ...(CREATE_NEW_AGENT.result!.agent as Record<string, unknown>),
    actor: names[0], displayName: names[0], agentId: ids[0],
  };
  const api = createTestAgentsApi({
    async request(method) {
      if (method === 'agent.list') {
        if (localReadFails) throw new Error('local agent.list unavailable');
        return { ok: true, agents: [local], agentsWithoutRoutine: [] };
      }
      throw new Error(`unexpected method ${method}`);
    },
  });
  const snapshot = {
    ...ownerSnapshot(), currentOrgId: ORG_TEAM,
    agentOrgs: Object.fromEntries(ids.map((id) => [id, [ORG_TEAM]])),
    orgAgentDirectory: { [ORG_TEAM]: orgAgents },
    orgAgentDirectoryStatus: status ? { [ORG_TEAM]: status } : undefined,
  };
  const dispatch = vi.fn();
  render(
    <GuiCapabilitiesProvider value={{ agents: api, firstRun: stubFirstRunActions(), chatTargets }}>
      <OrgDomainProvider source={{ initial: snapshot, createGateway: () => createUnwiredGateway() }}>
        <ContactsPage state={{ ...createFixtureState(), page: 'contacts', agentPanel: detailId ? { kind: 'detail', agentId: detailId } : { kind: 'list' } }} dispatch={dispatch} />
      </OrgDomainProvider>
    </GuiCapabilitiesProvider>,
  );
  return dispatch;
}

it('opens chat for a shared remote Agent returned by targets', async () => {
  const uri = `agent:bob:node-b:${ids[1]}`;
  const chatTargets = vi.fn(async () => [{ targetUri: uri, targetKind: 'agent', deliverable: true, actor: uri, agentId: ids[1] }]);
  const dispatch = renderDirectory(records, ids[1], false, 'ok', chatTargets);
  fireEvent.click(await screen.findByRole('button', { name: '开始对话' }));
  await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'open-agent-chat', targetId: uri }));
});

it('does not open a stale remote target after it leaves the directory', async () => {
  const uri = `agent:bob:node-b:${ids[1]}`;
  const chatTargets = vi.fn()
    .mockResolvedValueOnce([{ targetUri: uri, targetKind: 'agent', deliverable: true, actor: uri, agentId: ids[1] }])
    .mockResolvedValueOnce([]);
  const dispatch = renderDirectory(records, ids[1], false, 'ok', chatTargets);
  const button = await screen.findByRole('button', { name: '开始对话' });
  await act(async () => { fireEvent.click(button); });
  expect(chatTargets).toHaveBeenCalledTimes(2);
  expect(dispatch).not.toHaveBeenCalledWith({ type: 'open-agent-chat', targetId: uri });
});

it('lists all four confirmed organization Agents when only one is local', async () => {
  renderDirectory(records);
  expect(await screen.findByTestId('contact-test-1')).toBeInTheDocument();
  for (const name of names) expect(screen.getByText(name)).toBeInTheDocument();
  expect(screen.getByTestId(`contact-${ids[1]}`)).toBeEnabled();
  expect(screen.getByText('小B')).toBeInTheDocument();
});

it('does not claim the organization is empty when membership reading is unavailable', async () => {
  renderDirectory(null);
  expect(await screen.findByText('组织 Agent 列表尚未同步完整')).toBeInTheDocument();
  expect(screen.queryByText('当前组织还没有 Agent')).not.toBeInTheDocument();
});

it('does not offer local management controls for a remote organization Agent', async () => {
  renderDirectory(records, ids[1]);
  expect(await screen.findByText('远端 Agent 详情')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '启动' })).not.toBeInTheDocument();
});

it('keeps confirmed organization Agents visible when the local Agent directory fails', async () => {
  renderDirectory(records, undefined, true);
  expect(await screen.findByText('本机 Agent 列表读取失败')).toBeInTheDocument();
  for (const name of names) expect(screen.getByText(name)).toBeInTheDocument();
});

it('shows known member Agents and warns when another inventory is missing', async () => {
  renderDirectory(records.slice(0, 1), undefined, false, 'partial');
  expect(await screen.findByText('部分组织 Agent 列表未能确认最新状态，可能显示旧数据')).toBeInTheDocument();
});
