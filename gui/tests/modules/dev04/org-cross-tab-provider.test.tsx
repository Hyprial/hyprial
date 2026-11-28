import { render, waitFor } from '@testing-library/react';
import { useReducer } from 'react';
import { AppShell } from '../../../src/app/AppShell';
import { GuiCapabilitiesProvider } from '../../../src/app/capabilities';
import { buildAgentScenario } from '../../../src/dev/j2-scenarios';
import { authenticatedTestActions } from '../../support/demo-baseline';
import type { OrgDomainSource } from '../../../src/features/org/impl/domain/context';
import { UNWIRED_SNAPSHOT, createUnwiredGateway } from '../../../src/features/org/impl/domain/unwired';
import type { OrgDetail } from '../../../src/features/org/impl/domain/types';
import { appReducer } from '../../../src/state/appReducer';
import { createFixtureState } from '../../../src/dev/fixtures/fixture-state';
import { DEMO_BASELINE } from '../../support/demo-baseline';

const ORG_ID = 'group-x';
const SEED = buildAgentScenario('m04')!.seed;
const AGENT_ID = SEED.agents[0].id;

const DETAIL: OrgDetail = {
  people: [],
  agents: null,
  machines: [],
  spaces: [],
  files: {},
  trash: {},
  config: null,
  routes: null,
  stats: null,
  updatedAt: null,
};

const SOURCE: OrgDomainSource = {
  initial: {
    ...UNWIRED_SNAPSHOT,
    status: 'ready',
    orgs: [
      {
        id: ORG_ID,
        name: '测试组织',
        kind: 'work',
        role: 'owner',
        lifecycle: 'active',
        ownerLabel: null,
        authorityDevice: null,
        deviceCount: 1,
      },
    ],
    currentOrgId: ORG_ID,
    details: { [ORG_ID]: DETAIL },
    receivedInvites: [],
  },
  createGateway: () => createUnwiredGateway(),
};

function Harness() {
  const [state, dispatch] = useReducer(appReducer, {
    ...createFixtureState(DEMO_BASELINE),
    page: 'contacts',
    agentPanel: { kind: 'detail', agentId: AGENT_ID },
  });
  return (
    <GuiCapabilitiesProvider
      value={{ firstRun: authenticatedTestActions(), orgSource: SOURCE }}
    >
      <AppShell state={state} dispatch={dispatch} agentSeed={SEED} />
    </GuiCapabilitiesProvider>
  );
}

describe('组织 Provider 的跨页生命周期', () => {
  it('组织数据源注入后，Agent 详情仍消费同一上下文并渲染组织面板', async () => {
    render(<Harness />);
    // The org page is not mounted here; the shared host provider must still
    // reach the Agent cross-tab panel instead of dropping it on navigation.
    await waitFor(() => expect(document.querySelector('[data-dev-ui="DEV-UI-ORG-015"]')).toBeTruthy());
  });
});
