/**
 * Development entry to the Contract Mock: front-end work without a backend.
 *
 * Decision record (1e-2, 2026-11-15)
 * - This is the only importer of the generated contract table. The table is the
 *   projection `gui/scripts/generate-reference-client.mjs` writes with the same
 *   `projectContract` the Node form applies to `openapi.json`; `client:reference:check`
 *   fails on any byte difference, so these examples cannot drift from the
 *   contract and no hand-written second copy exists.
 * - `connectContractMock` runs the product's own session bootstrap, transport
 *   and generated client against the in-browser form. Only `fetch` is replaced,
 *   so a page sees the typed results and refusals it would see from the host.
 *   The session proof lives in an in-memory map, never in browser storage.
 * - Nothing here names a daemon method. Callers that need one example ask by
 *   method name through `contractExamples`.
 */

import tableData from './contract.generated.json';
import { connectBrowserBackend } from '../../app/backend';
import type { BrowserBackendBootstrap } from '../../app/backend';
import { MOCK_ORIGIN, createContractMock, createContractMockFetch, withDigest } from '../../../contract-mock/index.ts';
import type { ContractProjection, ContractTable, ExampleCase, Selection } from '../../../contract-mock/index.ts';

export type { ExampleCase, Selection };

const LAUNCH_GRANT = 'contract-mock-launch';

let loaded: ContractTable | null = null;

function table(): ContractTable {
  loaded ??= withDigest(tableData as unknown as ContractProjection);
  return loaded;
}

export function contractExamples(method: string): readonly ExampleCase[] {
  return table().methods[method]?.examples ?? [];
}

export async function connectContractMock(selection: Selection): Promise<BrowserBackendBootstrap> {
  const mock = createContractMock(table(), selection);
  const memory = new Map<string, string>();
  return connectBrowserBackend({
    fetch: createContractMockFetch(mock),
    storage: {
      getItem: (key) => memory.get(key) ?? null,
      setItem: (key, value) => { memory.set(key, value); },
      removeItem: (key) => { memory.delete(key); },
    },
    location: { origin: MOCK_ORIGIN, hash: `#grant=${LAUNCH_GRANT}`, pathname: '/', search: '' },
    history: { replaceState() {} },
  });
}
