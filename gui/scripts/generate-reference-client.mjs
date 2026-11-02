/**
 * Generate all declared business types from MethodSpec, including IPC-only writes.
 * A generated method is not HTTP admission: H-01 still rejects hidden writes.
 * Development examples are emitted separately and cannot enter the live bundle.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { checkDirectory, generateToDirectory } from './generate-client.mjs';

const source = new URL('../../contract/daemon-request/openapi.json', import.meta.url);
const document = JSON.parse(await readFile(source, 'utf8'));
const projection = { openapi: document.openapi, info: document.info, paths: {} };
for (const entry of document['x-hyprial-methods']) {
  projection.paths[`/daemon/request/${entry.name}`] = { post: {
    operationId: entry.name,
    'x-hyprial-mutates': entry.mutates,
    'x-hyprial-retry': entry.retry,
    'x-hyprial-http-binding': entry.httpBinding,
    'x-hyprial-errors': entry.errors,
    requestBody: { content: { 'application/json': { schema: entry.paramsSchema } } },
    responses: { 200: { content: { 'application/json': { schema: {
      type: 'object', properties: { result: entry.resultSchema }, required: ['result'],
    } } } } },
  } };
}
const output = fileURLToPath(new URL('../src/client/generated', import.meta.url));
const options = { source: 'contract/daemon-request/openapi.json (all declarations; not HTTP admission)' };
if (process.argv.includes('--check')) {
  const drift = await checkDirectory(projection, output, options);
  if (drift.length) throw new Error(`Generated reference client drift: ${drift.join(', ')}`);
} else {
  await generateToDirectory(projection, output, options);
}
const exampleFile = new URL('../src/features/agent-provider/api/examples.generated.json', import.meta.url);
const examples = Object.fromEntries(document['x-hyprial-methods']
  .filter(entry => entry['x-hyprial-examples'])
  .map(entry => [entry.name, entry['x-hyprial-examples']]));
const text = `${JSON.stringify(examples, null, 2)}\n`;
if (process.argv.includes('--check')) {
  if (await readFile(exampleFile, 'utf8') !== text) throw new Error('Reference example drift');
} else {
  await writeFile(exampleFile, text);
}
console.log('Reference client and examples match MethodSpec');
