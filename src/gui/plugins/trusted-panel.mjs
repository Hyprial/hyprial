import { Service } from '@deepseek-ai/cordis';

export const PANEL_ID = 'panel:trusted-main';

class PanelRegistry extends Service {
  constructor(ctx) { super(ctx, 'panelRegistry'); this.entries = []; }
  add(id) { this.entries.push(id); }
}

// Arrow plugin shape makes Cordis treat this as a plugin function. Cordis owns
// effect cleanup and unregisters the service with the returned disposer.
export const trustedPanel = (ctx, { mount, cleanupLog, stats }) => {
  const registry = new PanelRegistry(ctx);
  registry.add(PANEL_ID);
  stats.hits = 0;
  const off = ctx.on('gui/panel-event', () => { stats.hits += 1; });
  const unmount = mount.mount(PANEL_ID, 'trusted-panel');
  return () => {
    off();
    unmount();
    cleanupLog.push('listener', 'panel');
  };
};
