/** Shared production App, canonical method examples, deterministic test operation IDs. */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '../../../../src/App';
import { connectContractMock } from '../../../../src/dev/contract-mock';
import '../../../../src/styles/tokens.css';
import '../../../../src/styles/app.css';
import '../../../../src/styles/workspace-pages.css';
import '../../../../src/styles/shadcn.css';

const preset = new URLSearchParams(location.search).get('pending') === '1' ? 'agent-model-pending' : 'agent-model-create';
const bootstrap = await connectContractMock({ preset });
createRoot(document.getElementById('root')!).render(<StrictMode><App backend={{ status: 'connected', connection: bootstrap.connection }} /></StrictMode>);
