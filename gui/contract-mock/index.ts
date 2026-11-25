/**
 * Public surface of the Contract Mock.
 *
 * Decision record (1e-2, 2026-11-15)
 * - One module serves every caller: the Node process form (`serve.ts`), the
 *   in-browser form and tests all call `createContractMock` over the same
 *   projected table. This file only binds the committed `presets.json` as the
 *   default preset document so callers do not each import it.
 * - The module is erasable TypeScript with `.ts` relative imports, so plain
 *   Node 24 and Vite load it without a build step.
 */

import presetData from './presets.json' with { type: 'json' };
import { createContractMock as createCoreMock } from './respond.ts';
import type { ContractMock, MockOptions, PresetDocument, Selection } from './respond.ts';
import type { ContractTable } from './contract.ts';

export * from './browser.ts';
export * from './contract.ts';
export * from './pattern.ts';
export * from './respond.ts';
export * from './schema.ts';

export const defaultPresets = presetData as unknown as PresetDocument;

export function createContractMock(
  table: ContractTable,
  selection: Selection,
  options: MockOptions = {},
): ContractMock {
  return createCoreMock(table, selection, { ...options, presets: options.presets ?? defaultPresets });
}
