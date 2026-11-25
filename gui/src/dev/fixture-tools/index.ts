/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): public surface of the
 * fixture-only tools and of the development preview store. The fixture
 * composition root imports this module statically; the floating tool layer
 * never does.
 */
export { createFixtureDevToolsContext } from './impl/context';
export { DevDataSourceNotice } from './impl/DevDataSourceNotice';
export {
  encodeFixtureId,
  getDevPreviewStore,
  resetDevPreviewStore,
  type DevAuthInitState,
  type DevPreviewStore,
  type DevScenarioState,
} from './impl/preview-store';
