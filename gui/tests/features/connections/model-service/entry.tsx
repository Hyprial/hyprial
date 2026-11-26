/** Browser acceptance entry: the production App and canonical Contract Mock only. */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '../../../../src/App';
import { connectContractMock } from '../../../../src/dev/contract-mock';
import '../../../../src/styles/tokens.css';
import '../../../../src/styles/app.css';
import '../../../../src/styles/workspace-pages.css';
import '../../../../src/styles/shadcn.css';

const bootstrap=await connectContractMock({preset:'model-connections'});
createRoot(document.getElementById('root')!).render(<StrictMode><App backend={{status:'connected',connection:bootstrap.connection}}/></StrictMode>);
