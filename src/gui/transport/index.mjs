import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createMockSessionAdapter } from '../session/index.mjs';

const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };

// Fixture files the mock driver must find in an explicitly named directory.
// Falling back to module-bundled fixtures is deliberate only inside the
// session module's own tests; a product composition root must say where its
// development data lives.
const MOCK_FIXTURE_FILES = Object.freeze([
  'sessions.json', 'history-s-1.json', 'history-s-2.json',
  'progress-stream.json', 'terminal-state.json', 'cancel-receipt.json', 'capabilities.json',
]);

async function mockFixturesDriver(config) {
  const directory = config?.fixturesDir;
  if (typeof directory !== 'string' || !directory.trim()) {
    fail('GUI_TRANSPORT_CONFIG', "The 'mock-fixtures' driver requires an explicit config.fixturesDir");
  }
  let present;
  try {
    present = new Set(await readdir(directory));
  } catch {
    fail('GUI_TRANSPORT_CONFIG', `Transport fixture directory is unreadable: ${directory}`);
  }
  const missing = MOCK_FIXTURE_FILES.filter(name => !present.has(name));
  if (missing.length) {
    fail('GUI_TRANSPORT_CONFIG', `Transport fixture directory ${directory} is missing: ${missing.join(', ')}`);
  }
  const fixtures = {};
  for (const name of MOCK_FIXTURE_FILES) {
    fixtures[name] = JSON.parse(await readFile(join(directory, name), 'utf8'));
  }
  return createMockSessionAdapter({ fixtures });
}

// Built-in drivers. `mock-fixtures` is an explicit development/test selection;
// nothing selects it by default, and it is never a stand-in for the real
// Hyprial backend adapter (that adapter plugs in through `drivers`).
const builtins = Object.freeze({ 'mock-fixtures': mockFixturesDriver });
export const BUILTIN_TRANSPORT_DRIVERS = Object.freeze(Object.keys(builtins));

/**
 * Resolve one transport driver into a session port for the composition root.
 *
 * `driver` names either a built-in or an injected factory in `drivers`
 * ({ name: async (config) => adapter }). There is no implicit default: a
 * composition root that cannot say which backend it talks to fails closed
 * here instead of silently binding the mock.
 */
export async function createTransportDriver({ driver, config = {}, drivers = {} } = {}) {
  if (typeof driver !== 'string' || !driver.trim()) {
    fail('GUI_TRANSPORT_CONFIG', 'A transport driver name is required');
  }
  if (!drivers || typeof drivers !== 'object' || Array.isArray(drivers)) {
    fail('GUI_TRANSPORT_CONFIG', 'Injected transport drivers must be a factory map');
  }
  const factory = Object.hasOwn(drivers, driver) ? drivers[driver] : builtins[driver];
  if (!factory) {
    const available = [...BUILTIN_TRANSPORT_DRIVERS, ...Object.keys(drivers)].sort();
    fail('GUI_TRANSPORT_UNAVAILABLE', `Unknown transport driver '${driver}'. Available: ${available.join(', ') || '(none)'}`);
  }
  if (typeof factory !== 'function') fail('GUI_TRANSPORT_CONFIG', `Transport driver '${driver}' is not a factory`);
  const adapter = await factory(config);
  if (!adapter || typeof adapter.sessions?.list !== 'function' || typeof adapter.capabilities?.get !== 'function') {
    fail('GUI_TRANSPORT_CONFIG', `Transport driver '${driver}' did not return a session port`);
  }
  return adapter;
}
