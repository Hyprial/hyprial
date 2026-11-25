/**
 * Decision record (WP1 composition root, 2026-11-09; deep modules 2026-11-10):
- The entry is the only token-aware file, and it carries no mock code: the
  `__HYPRIAL_DEV_PREVIEW__` and `__HYPRIAL_PARITY__` branches dynamically
  import `dev/parity-composition`, which owns the scenario URL contract, the
  mock kit and both mock entry renderers. With both tokens false the formal
  `dist` runs the same-origin backend composition only, and the dynamic
  imports are dead code — the module, the URL scenario strings and the
  scenario data are all DCE'd out of the release artifact (verified by the
  production copy gate). The dynamic dev import is the single built-in
  exception of the import-boundary gate.
- Build shapes: `__HYPRIAL_DEV_PREVIEW__` mounts the development preview
  (product tree in `#root`, floating tools portal outside it) around the same
  `App`; `__HYPRIAL_PARITY__` builds the separate `dist-parity` TEST artifact
  with exactly the same mock composition and no development tooling. The
  parity artifact registers the chat mock port like the development entry;
  the formal artifact gate scans only the formal `dist`.
- Mock input contract (WP6), consumed inside the parity module by both mock
  entries: the scenario URL query and the first-run gate query;
  a present scenario id selects the pure URL-derived composition so identical
  ids render identical trees; absent parameters keep the default entry
  behaviour. Product components never read the environment or the URL.
- Developer tools (PR 4b-G, 2026-11-15): the formal entry loads the tool layer
  with one dynamic import of `./dev/tools`, only after the session response
  carries `devTools`. The host alone reports that capability; no URL
  parameter, storage key, global or build flag turns it on here. The import
  is a separate chunk emitted under `dist/dev-tools/`, so a launch without the
  capability never requests it. The layer is independent of the session
  generation and is not remounted on refresh. The second allowed specifier in
  the parity gate is this one.
- PR-F FIX-004: the production failure page keeps the owner-approved copy and
  the bootstrap code; bootstrap judgement and DCE/host logic are unchanged.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { Button, LoadingState } from './components/primitives';
import brandMark from './assets/brand/hyprial-mark.svg';
import { BrowserBootstrapError, connectBrowserBackend, watchBrowserSession, type BrowserBackendBootstrap, type BackendTransition } from './app/backend';
import type { GuiPage } from './state/gui-view-model';

/** Declared by the build (vite define, dev tooling group); module-local on purpose. */
declare const __HYPRIAL_PARITY__: boolean;

import './styles/tokens.css';
import './styles/app.css';
import './styles/workspace-pages.css';
import './styles/shadcn.css';
import './styles/bootstrap-unavailable.css';
import './styles/bootstrap-transition.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Missing #root element');
}
const root: HTMLElement = rootElement;

// PR-F FIX-004：production 无可信启动器会话时的正式失败页（v13 视觉 + owner 文案）。
// 只渲染 owner 批准的文案与问题编号；bootstrap 判定与 DCE/host 逻辑不变。
function BootstrapUnavailable({ error }: { readonly error: unknown }) {
  const code = error instanceof BrowserBootstrapError ? error.code : 'BOOTSTRAP_FAILED';
  const recoveryTimeout = code === 'SESSION_RECOVERY_TIMEOUT';
  return <main className="app app-first-run bootstrap-page">
    <section
      className="bootstrap-card"
      data-page="bootstrap-unavailable"
      aria-labelledby="bootstrap-title"
    >
      <img className="bootstrap-card__mark" src={brandMark} alt="Hyprial" />
      <h1 className="bootstrap-card__title" id="bootstrap-title">
        {recoveryTimeout ? '本机服务连接超时' : '这个页面不能直接打开'}
      </h1>
      <p className="bootstrap-card__body" role="alert">
        {recoveryTimeout
          ? '请重新检查；若仍未恢复，请重新打开 Hyprial。'
          : '请从 Hyprial 桌面应用或启动器打开界面，以连接本机服务。打开后，如果此页面仍未恢复，请点击“重新检查”。'}
      </p>
      <Button variant="primary" className="bootstrap-primary-layout" onClick={() => window.location.reload()}>
        重新检查
      </Button>
      <p className="bootstrap-card__code">
        问题编号：<code data-bootstrap-code={code}>{code}</code>
      </p>
    </section>
  </main>;
}

function BootstrapTransition({ reason }: { readonly reason: BackendTransition }) {
  const title = reason === 'logout' ? '正在退出本机登录'
    : reason === 'initialize' ? '正在启动本机服务' : '正在连接本机服务';
  return <main className="app app-first-run bootstrap-page bootstrap-transition">
    <section className="bootstrap-card bootstrap-transition__card" aria-label="Hyprial" data-page="bootstrap-transition">
      <img className="bootstrap-card__mark" src={brandMark} alt="Hyprial" />
      <LoadingState title={title} className="bootstrap-transition__status" />
    </section>
  </main>;
}

async function renderApp(): Promise<void> {
  if (__HYPRIAL_DEV_PREVIEW__) {
    const { renderDevEntry } = await import('./dev/parity-composition');
    await renderDevEntry(root);
    return;
  }
  if (__HYPRIAL_PARITY__) {
    const { renderParityEntry } = await import('./dev/parity-composition');
    await renderParityEntry(root);
    return;
  }

  const application = createRoot(root);
  const changing = (reason: BackendTransition) => application.render(<BootstrapTransition reason={reason} />);
  changing('reconnect');
  let page: GuiPage | undefined;
  const rememberPage = (next: GuiPage) => { page = next; };
  const connected = (bootstrap: BrowserBackendBootstrap) => {
    application.render(
      <StrictMode>
        <App
          key={bootstrap.session.generation}
          backend={{ status: 'connected', connection: bootstrap.connection }}
          initialPage={page}
          onPageChange={rememberPage}
        />
      </StrictMode>,
    );
  };
  const failed = (error: unknown) => application.render(<StrictMode><BootstrapUnavailable error={error} /></StrictMode>);
  try {
    const bootstrap = await connectBrowserBackend();
    connected(bootstrap);
    const devTools = bootstrap.session.devTools;
    if (devTools) {
      const { ensureDevToolsContainer, mountDevTools } = await import('./dev/tools');
      mountDevTools(ensureDevToolsContainer(root), { dataSource: 'backend', apiDocs: devTools.apiDocs });
    }
    const stop = watchBrowserSession(bootstrap, {
      connected,
      changing,
      failed,
    });
    window.addEventListener('pagehide', stop, { once: true });
    window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });
  } catch (error) {
    failed(error);
  }
}

void renderApp();
