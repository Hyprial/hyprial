/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14):
 * - mountDevTools is the single function that mounts the floating tool layer
 *   over any composition. Entries reach it through a dynamic import of the
 *   tools module so the layer stays a separately loaded chunk.
 * - The container must live outside the product #root; mounting inside it is
 *   refused instead of silently changing product layout. Mounting twice into
 *   the same container is refused; callers unmount first.
 * - ensureDevToolsContainer creates the sibling container right after the
 *   product root, the position the development entry has always used.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import type { DevToolsContext } from './context';
import { DevToolsLayer } from './DevToolsLayer';

export const DEV_TOOLS_CONTAINER_ID = 'hyprial-dev-tools';
const PRODUCT_ROOT_ID = 'root';

export interface DevToolsHandle {
  unmount(): void;
}

const mounted = new WeakMap<HTMLElement, DevToolsHandle>();

export function ensureDevToolsContainer(productRoot: HTMLElement): HTMLElement {
  const existing = document.getElementById(DEV_TOOLS_CONTAINER_ID);
  if (existing) {
    return existing;
  }
  const container = document.createElement('div');
  container.id = DEV_TOOLS_CONTAINER_ID;
  productRoot.insertAdjacentElement('afterend', container);
  return container;
}

export function mountDevTools(
  container: HTMLElement,
  context: DevToolsContext,
): DevToolsHandle {
  const productRoot = document.getElementById(PRODUCT_ROOT_ID);
  if (productRoot && (productRoot === container || productRoot.contains(container))) {
    throw new Error('dev tools must be mounted outside the product #root');
  }
  if (mounted.has(container)) {
    throw new Error('dev tools are already mounted in this container; unmount them first');
  }
  const reactRoot = createRoot(container);
  reactRoot.render(
    <StrictMode>
      <DevToolsLayer context={context} />
    </StrictMode>,
  );
  const handle: DevToolsHandle = {
    unmount: () => {
      if (mounted.get(container) !== handle) {
        return;
      }
      mounted.delete(container);
      reactRoot.unmount();
    },
  };
  mounted.set(container, handle);
  return handle;
}
