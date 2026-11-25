/*
 * Decision record (WP2 floating layer, gui-dev-product-parity-2026-11-09;
 * dev tools seam, PR 4a, 2026-11-14):
 * - This is the shell of the floating tool layer: portal host outside the
 *   product #root, collapsed floating ball, non-modal panel, Escape and
 *   outside pointerdown close, focus restore. It was the render half of
 *   DevPreviewApp and behaves exactly as before for the simulated
 *   composition (same DOM as #2727: notice, scenario tabs, tab panel).
 * - The shell owns no scenario or session state. Simulated-only tools arrive
 *   as components through the context, and the context's scope component
 *   stays mounted around the layer so they can keep state while the panel is
 *   closed; over the real backend the shell renders the module index only.
 * - Over the backend the panel shows the API docs entry (a plain link that
 *   opens the host's docs page in a new tab with the opener kept, so the new
 *   tab inherits the per-view proof) only when the context says the host
 *   serves it, then the module index. No other text is rendered.
 * - The layer never renders the product App and never touches product DOM.
 * - The portal host is created in a layout effect, so the layer is in the DOM
 *   when the mount commit finishes. With the former passive effect the layer
 *   appeared 50-100 ms after the product root (measured 2026-11-14) and the
 *   browser gates, which probe the layer as soon as the product root has
 *   children, raced it.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { DevModuleIndex } from '../../DevModuleIndex';
import type { DevToolsContext } from './context';
import '../../dev-module-index.css';
import './api-docs.css';

function DevToolsPanelBody({ context }: { readonly context: DevToolsContext }) {
  if (context.dataSource === 'simulated') {
    const { notice: Notice, scenarios: Scenarios } = context.contributions;
    return (
      <>
        <Notice />
        <Scenarios />
      </>
    );
  }
  return (
    <>
      {context.apiDocs ? (
        <a
          className="dev-tools-api-docs"
          data-dev-tools-api-docs=""
          href="docs"
          rel="opener"
          target="_blank"
        >
          API 文档
        </a>
      ) : null}
      <DevModuleIndex />
    </>
  );
}

export function DevToolsLayer({ context }: { readonly context: DevToolsContext }) {
  if (context.dataSource === 'simulated') {
    const { scope: Scope } = context.contributions;
    return (
      <Scope>
        <DevToolsShell context={context} />
      </Scope>
    );
  }
  return <DevToolsShell context={context} />;
}

function DevToolsShell({ context }: { readonly context: DevToolsContext }) {
  const [open, setOpen] = useState(false);
  const [host, setHost] = useState<HTMLElement | null>(null);
  const ballRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    const element = document.createElement('div');
    element.className = 'dev-tools-host';
    document.body.appendChild(element);
    setHost(element);
    return () => {
      element.remove();
      setHost(null);
    };
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (panelRef.current?.contains(target) || ballRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      panelRef.current?.focus();
    }
  }, [open]);

  const closeWithFocusRestore = () => {
    setOpen(false);
    ballRef.current?.focus();
  };

  if (!host) {
    return null;
  }

  return createPortal(
    <div
      className="dev-tools-layer"
      data-dev-tools=""
      data-dev-tools-source={context.dataSource}
      data-testid="dev-tools-layer"
    >
      {open ? (
        <div
          aria-label="开发工具面板"
          aria-modal="false"
          className="dev-tools-panel"
          data-testid="dev-tools-panel"
          id="dev-tools-panel"
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              closeWithFocusRestore();
            }
          }}
          ref={panelRef}
          role="dialog"
          tabIndex={-1}
          data-dev-tools-panel=""
        >
          <DevToolsPanelBody context={context} />
        </div>
      ) : null}
      <button
        aria-controls={open ? 'dev-tools-panel' : undefined}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label="开发工具"
        className="dev-tools-fab"
        data-dev-tools-toggle=""
        data-testid="dev-tools-fab"
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && open) {
            setOpen(false);
          }
        }}
        ref={ballRef}
        type="button"
      >
        <span aria-hidden="true">开发</span>
      </button>
    </div>,
    host,
  );
}
