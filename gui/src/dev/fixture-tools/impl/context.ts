/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): the fixture
 * composition describes itself to the floating tool layer with this context.
 * It is the only place that pairs the layer with fixture tools; the import of
 * the tools module is type-only so the layer stays a separately loaded chunk.
 */
import type { SimulatedDevToolsContext } from '../../tools';
import { DevDataSourceNotice } from './DevDataSourceNotice';
import { FixtureScenarioBrowser, FixtureScenarioScope } from './FixtureScenarioTools';

const FIXTURE_DEV_TOOLS_CONTEXT: SimulatedDevToolsContext = Object.freeze({
  dataSource: 'simulated',
  contributions: Object.freeze({
    scope: FixtureScenarioScope,
    notice: DevDataSourceNotice,
    scenarios: FixtureScenarioBrowser,
  }),
});

export function createFixtureDevToolsContext(): SimulatedDevToolsContext {
  return FIXTURE_DEV_TOOLS_CONTEXT;
}
