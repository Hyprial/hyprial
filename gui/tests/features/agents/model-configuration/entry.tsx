/** Shared production App, canonical method examples, deterministic test operation IDs. */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '../../../../src/App';
import { connectContractMock } from '../../../../src/dev/contract-mock';
import { createEmptyState } from '../../../../src/state/appReducer';
import { createStartupMockAgents } from '../../../../src/dev/mock/startup-agents';
import { createFixtureFirstRunActions } from '../../../../src/dev/first-run-capabilities';
import { createMockSharedCredentialsApi } from '../../../../src/dev/mock/agent-settings';
import '../../../../src/styles/tokens.css';
import '../../../../src/styles/app.css';
import '../../../../src/styles/workspace-pages.css';
import '../../../../src/styles/shadcn.css';

const query = new URLSearchParams(location.search);
const root = createRoot(document.getElementById('root')!);
if (query.get('source') === 'startup') {
  const runtime = createStartupMockAgents();
  root.render(<StrictMode><App initialState={createEmptyState('complete')} capabilitiesFactory={dispatch => ({
    firstRun: createFixtureFirstRunActions('complete', dispatch, 'ready'), firstRunWizardEnabled: false,
    agents: runtime.api, sharedCredentials: createMockSharedCredentialsApi(),
  })} /></StrictMode>);
} else {
  const preset = query.get('pending') === '1' ? 'agent-model-pending' : 'agent-model-create';
  const bootstrap = await connectContractMock({ preset });
  root.render(<StrictMode><App backend={{ status: 'connected', connection: bootstrap.connection }} /></StrictMode>);
}
