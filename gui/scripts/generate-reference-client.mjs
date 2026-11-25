/**
 * Generate all HTTP-exposed business types from MethodSpec.
 * IPC-only declarations (ACL, daemon.catalog, message.send.lookup) are left out
 * of the GUI reference client so no browser surface can even name them.
 * A generated method is still not HTTP admission: H-01 separately rejects
 * hidden writes.
 *
 * It also writes the Contract Mock table the browser form serves
 * (`src/dev/contract-mock/contract.generated.json`): the same `projectContract`
 * the Node form applies to `openapi.json` at start, so the two forms cannot
 * read different facts. `--check` fails on any byte difference.
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkDirectory, generateToDirectory } from './generate-client.mjs';

const source = new URL('../../contract/daemon-request/openapi.json', import.meta.url);
const document = JSON.parse(await readFile(source, 'utf8'));
const projection = { openapi: document.openapi, info: document.info, paths: {} };
const httpMethods = document['x-hyprial-methods'].filter(entry => entry.httpExposed === true);
for (const entry of httpMethods) {
  const path = document.paths?.[`/daemon/request/${entry.name}`]?.post;
  const httpParams = path?.requestBody?.content?.['application/json']?.schema;
  projection.paths[`/daemon/request/${entry.name}`] = { post: {
    operationId: entry.name,
    'x-hyprial-mutates': entry.mutates,
    'x-hyprial-retry': entry.retry,
    'x-hyprial-http-binding': entry.httpBinding,
    'x-hyprial-errors': entry.errors,
    // HTTP-exposed methods use the listener projection (host-injected fields
    // removed); IPC-only methods keep their full declaration.
    requestBody: { content: { 'application/json': { schema: httpParams ?? entry.paramsSchema } } },
    responses: { 200: { content: { 'application/json': { schema: {
      type: 'object', properties: { result: entry.resultSchema }, required: ['result'],
    } } } } },
  } };
}
const output = fileURLToPath(new URL('../src/client/generated', import.meta.url));
const options = { source: 'contract/daemon-request/openapi.json (HTTP-exposed declarations)' };
if (process.argv.includes('--check')) {
  const drift = await checkDirectory(projection, output, options);
  if (drift.length) throw new Error(`Generated reference client drift: ${drift.join(', ')}`);
} else {
  await generateToDirectory(projection, output, options);
}
const tableFile = new URL('../src/dev/contract-mock/contract.generated.json', import.meta.url);
const { projectContract } = await import('../contract-mock/contract.ts');
const envelope = JSON.parse(await readFile(new URL('../../contract/daemon-request/http-envelope.schema.json', import.meta.url), 'utf8'));
const text = `${JSON.stringify(projectContract(document, envelope), null, 2)}\n`;
if (process.argv.includes('--check')) {
  const current = await readFile(tableFile, 'utf8').catch(() => null);
  if (current !== text) throw new Error('Contract table drift');
} else {
  await mkdir(dirname(fileURLToPath(tableFile)), { recursive: true });
  await writeFile(tableFile, text);
}
console.log('Reference client and contract table match MethodSpec');
