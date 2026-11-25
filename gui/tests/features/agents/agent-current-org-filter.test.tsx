import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { AgentList } from '../../../src/features/agents/impl/AgentList';
import { AgentForm } from '../../../src/features/agents/impl/AgentForm';
import { OrgDomainProvider } from '../../../src/features/org/impl/domain/context';
import { createUnwiredGateway } from '../../../src/features/org/impl/domain/unwired';
import { ORG_PARTNER, ORG_TEAM, ownerSnapshot } from '../../../src/dev/org/fixture-data';
import { buildManagedAgent } from '../../../src/state/agent-selectors';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const agents = [
  buildManagedAgent({ id: 'agent-1', displayName: 'agent-1', role: '', avatarTone: 'green', modelLabel: '', capabilities: [] }, [], []),
  buildManagedAgent({ id: 'agent-2', displayName: 'agent-2', role: '', avatarTone: 'lilac', modelLabel: '', capabilities: [] }, [], []),
];
const initial = {
  ...ownerSnapshot(),
  agentOrgs: { 'agent-1': [ORG_TEAM], 'agent-2': [ORG_PARTNER] },
};
const source = { initial, createGateway: () => createUnwiredGateway() };
const props = {
  agents, total: agents.length, query: '', filter: 'all' as const,
  ownerFilter: '', ownerOptions: [], onOwnerFilter: () => {},
  hostFilter: '', hostOptions: [], onHostFilter: () => {},
  onCreate: () => {}, onOpen: () => {}, onQuery: () => {}, onFilter: () => {},
  onClearFilters: () => {},
};

it('creates a live Agent without an organization choice or assignment', async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  render(
    <OrgDomainProvider source={source}>
      <AgentForm mode="create" initial={{ displayName: '', role: '', modelServiceId: '', organizationId: null }}
        pending={false} message={null} onSubmit={onSubmit} onCancel={() => {}} />
    </OrgDomainProvider>,
  );
  expect(screen.queryByLabelText('所属组织')).not.toBeInTheDocument();
  await user.type(screen.getByLabelText('名称'), 'worker');
  await user.click(screen.getByLabelText('harness'));
  await user.click(await screen.findByRole('option', { name: 'claude' }));
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ organizationId: null }));
});

it('hides the organization choice in the preview creation form too', async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  render(
    <OrgDomainProvider source={source}>
      <AgentForm mode="create" initial={{ displayName: '', role: '', modelServiceId: '', organizationId: null }}
        previewMode pending={false} message={null} onSubmit={onSubmit} onCancel={() => {}} />
    </OrgDomainProvider>,
  );
  await user.type(screen.getByLabelText('名称'), 'unassigned');
  await user.type(screen.getByLabelText('职责'), 'worker');
  expect(screen.queryByLabelText('所属组织')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '创建 Agent' }));
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ organizationId: null }));
});

it('shows all local agents by default and filters only after selecting an organization', async () => {
  const user = userEvent.setup();
  const { rerender } = render(
    <OrgDomainProvider source={source}><AgentList {...props} /></OrgDomainProvider>,
  );
  expect(screen.getByTestId('contact-agent-1')).toBeInTheDocument();
  expect(screen.getByTestId('contact-agent-2')).toBeInTheDocument();

  await user.click(screen.getByLabelText('组织'));
  await user.click(await screen.findByRole('option', { name: 'hyprial-dev' }));
  expect(screen.queryByTestId('contact-agent-2')).not.toBeInTheDocument();

  rerender(
    <OrgDomainProvider source={source} externalSnapshot={{ ...initial, currentOrgId: ORG_PARTNER }}>
      <AgentList {...props} />
    </OrgDomainProvider>,
  );
  await waitFor(() => expect(screen.getByTestId('contact-agent-2')).toBeInTheDocument());
  expect(screen.getByTestId('contact-agent-1')).toBeInTheDocument();
});

it('shows unassigned agents in the local list with an explicit label', () => {
  const emptyCurrentOrg = { ...initial, agentOrgs: {} };
  render(
    <OrgDomainProvider source={{ initial: emptyCurrentOrg, createGateway: () => createUnwiredGateway() }}>
      <AgentList {...props} agents={[agents[1]]} total={1} />
    </OrgDomainProvider>,
  );
  expect(screen.getByTestId('contact-agent-2')).toBeInTheDocument();
  expect(screen.getByText('未关联组织')).toBeInTheDocument();
});

it('filters a live directory row by its immutable agentId while keeping actor as the route key', () => {
  const stableId = 'a.' + 'a'.repeat(26);
  const named = { ...agents[0], id: 'researcher', agentId: stableId };
  const snapshot = { ...initial, agentOrgs: { [stableId]: [ORG_TEAM] } };
  render(
    <OrgDomainProvider source={{ initial: snapshot, createGateway: () => createUnwiredGateway() }}>
      <AgentList {...props} agents={[named]} total={1} />
    </OrgDomainProvider>,
  );
  expect(screen.getByTestId('contact-researcher')).toBeInTheDocument();
  expect(screen.getByText('组织')).toBeInTheDocument();
});

it('shows confirmed current organization members even when another organization read failed', async () => {
  const user = userEvent.setup();
  const snapshot = {
    ...initial,
    agentOrgs: null,
    orgAgentDirectory: {
      [ORG_TEAM]: [{ id: 'agent-1', name: 'agent-1', ownerLabel: '我', machineLabel: '本机', runState: 'unknown' as const, orgIds: [ORG_TEAM], mine: true }],
      [ORG_PARTNER]: null,
    },
  };
  render(
    <OrgDomainProvider source={{ initial: snapshot, createGateway: () => createUnwiredGateway() }}>
      <AgentList {...props} />
    </OrgDomainProvider>,
  );
  expect(screen.getByTestId('contact-agent-1')).toBeInTheDocument();
  expect(screen.queryByTestId('contact-agent-2')).not.toBeInTheDocument();
  await user.click(screen.getByLabelText('组织'));
  await user.click(await screen.findByRole('option', { name: '全部' }));
  expect(screen.getByTestId('contact-agent-2')).toBeInTheDocument();
  expect(screen.getByText('部分组织 Agent 列表未能确认最新状态，可能显示旧数据')).toBeInTheDocument();
});

it('keeps confirmed current organization scope while organization data refreshes', () => {
  const snapshot = {
    ...initial,
    status: 'loading' as const,
    orgAgentDirectory: {
      [ORG_TEAM]: [{ id: 'agent-1', name: 'agent-1', ownerLabel: '我', machineLabel: '本机', runState: 'unknown' as const, orgIds: [ORG_TEAM], mine: true }],
      [ORG_PARTNER]: null,
    },
  };
  render(
    <OrgDomainProvider source={{ initial: snapshot, createGateway: () => createUnwiredGateway() }}>
      <AgentList {...props} />
    </OrgDomainProvider>,
  );
  expect(screen.getByTestId('contact-agent-1')).toBeInTheDocument();
  expect(screen.queryByTestId('contact-agent-2')).not.toBeInTheDocument();
});
