import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { createGuiCapabilities } from './app/capabilities';
import './styles/tokens.css';
import './styles/app.css';
import './styles/workspace-pages.css';
import './styles/shadcn.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Missing #root element');
}
const root: HTMLElement = rootElement;

async function renderApp(): Promise<void> {
  if (__HYPRIAL_DEV_PREVIEW__ && !__HYPRIAL_LIVE_ENABLED__) {
    const { DevPreviewApp } = await import('./dev-mode/DevPreviewApp');
    createRoot(root).render(
      <StrictMode>
        <DevPreviewApp />
      </StrictMode>,
    );
    return;
  }

  createRoot(root).render(
    <StrictMode>
      <App firstRunMode="unavailable" capabilities={createGuiCapabilities(window.__hyprialAccount)} />
    </StrictMode>,
  );
}

void renderApp();
