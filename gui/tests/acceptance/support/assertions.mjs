/**
 * Decision record (M1/PR 4b-C/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - Shared assertions for built dev-tools asset enumeration and Settings
 *   read-only catalog verification.
 */

import assert from 'node:assert/strict';
import { readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { GUI_ROOT } from './home.mjs';

export async function builtToolPaths() {
  const directory = join(GUI_ROOT, 'dist', 'dev-tools');
  const names = (await readdir(directory, { withFileTypes: true }))
    .filter((entry) => entry.isFile())
    .map((entry) => `/dev-tools/${entry.name}`);
  assert.ok(names.some((name) => name.endsWith('.js')), 'the build must emit the tool layer chunk');
  return [...names, '/dev-tools/docs/swagger-ui-bundle.js', '/dev-tools/docs/swagger-ui.css'];
}

export function assertSettingsReadOnly(result) {
  const catalog = new Map(result.catalogMethods.map((entry) => [entry.name, entry]));
  for (const request of result.requests) {
    const declared = catalog.get(request.method);
    assert.ok(declared, `Settings dispatched an undeclared method ${request.method}`);
    if (declared.mutates !== true) continue;
    const fixedProviderDefaultRead = request.method === 'agent.provider.default'
      && request.params !== null
      && typeof request.params === 'object'
      && !Array.isArray(request.params)
      && Reflect.ownKeys(request.params).length === 0;
    assert.equal(fixedProviderDefaultRead, true,
      `Settings dispatched a write method ${request.method}`);
  }
}
