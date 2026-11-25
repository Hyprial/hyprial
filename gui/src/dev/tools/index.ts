/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): public surface of the
 * floating tool layer. Entries import this module dynamically and call
 * mountDevTools; the simulated composition supplies its tools through the
 * context. Nothing under tools/ may import scenario, session or simulated-data
 * modules (gate: tests/dev-tools/import-graph.test.ts).
 */
export {
  type BackendDevToolsContext,
  type DevToolsContext,
  type SimulatedDevToolsContext,
  type SimulatedToolContributions,
} from './impl/context';
export { DevToolsLayer } from './impl/DevToolsLayer';
export {
  DEV_TOOLS_CONTAINER_ID,
  ensureDevToolsContainer,
  mountDevTools,
  type DevToolsHandle,
} from './impl/mount';
