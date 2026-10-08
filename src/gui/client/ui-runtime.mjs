import { Context } from '@deepseek-ai/cordis';

// Cordis Context and its registry stay behind this lifecycle facade. IDs and
// plugin implementations come only from the trusted composition root.
export function createUiRuntime({ trustedPlugins, configuration = {} }) {
  if (!trustedPlugins || typeof trustedPlugins !== 'object' || Array.isArray(trustedPlugins)) {
    throw new TypeError('trustedPlugins must be a fixed trusted registry');
  }
  const app = new Context();
  const mounted = new Map();
  const mounting = new Map();
  const slotEntries = new Map();
  const mountTable = Object.freeze({
    mount(id, owner) {
      slotEntries.set(id, owner);
      return () => slotEntries.delete(id);
    },
  });
  let disposed = false;

  function assertActive() { if (disposed) throw new Error('UI runtime is disposed'); }
  return Object.freeze({
    async mount(id) {
      assertActive();
      if (typeof id !== 'string' || !Object.hasOwn(trustedPlugins, id)) throw new Error(`Untrusted plugin id: ${id}`);
      if (mounted.has(id)) return;
      if (mounting.has(id)) return mounting.get(id);
      const plugin = trustedPlugins[id];
      if (typeof plugin !== 'function' || plugin.prototype !== undefined) throw new TypeError(`Trusted plugin must be an arrow function: ${id}`);
      const task = Promise.resolve(app.plugin(plugin, { ...(configuration[id] || {}), mount: mountTable }))
        .then(fiber => { mounted.set(id, fiber); })
        .finally(() => mounting.delete(id));
      mounting.set(id, task);
      return task;
    },
    async unmount(id) {
      assertActive();
      if (mounting.has(id)) await mounting.get(id);
      const fiber = mounted.get(id);
      if (!fiber) return;
      mounted.delete(id);
      await fiber.dispose();
    },
    async dispose() {
      if (disposed) return;
      disposed = true;
      const fibers = [...mounted.values()].reverse();
      mounted.clear();
      const pending = [...mounting.values()];
      await Promise.all(pending);
      const lateFibers = [...mounted.values()].reverse();
      mounted.clear();
      await Promise.all([...fibers, ...lateFibers].map(fiber => fiber.dispose()));
      slotEntries.clear();
      await app.fiber.dispose();
    },
    slots() { return Object.freeze([...slotEntries].map(([id, owner]) => Object.freeze({ id, owner }))); },
  });
}
