// @vitest-environment node
/**
 * Contract Mock core: projection, selection, envelopes and failure refusals.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Ported from tests/daemon/contract_mock/test_mock_contract.py. HTTP framing,
 *   the bearer and `--check` live in serve.test.ts. Sensitive-material scanning
 *   of examples stays in Python (`check_contract`).
 * - Replies are validated against the real pinned envelope `$defs` with the
 *   mock's own schema subset, so the subset is exercised on a schema it did not
 *   get to choose.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  ContractError,
  availableScenarios,
  canonicalJson,
  EMPTY_PRESETS,
  createContractMock,
  projectContract,
  schemaErrors,
  sha256Hex,
  tableDigest,
  withDigest,
} from '../../contract-mock/index.ts';
import type { Selection } from '../../contract-mock/index.ts';
import { clone, realEnvelope, realOpenapi, syntheticOpenapi, syntheticTable } from './synthetic.ts';

const envelope = realEnvelope();

function mock(selection: Selection = { scenario: 'success' }, document = syntheticOpenapi()) {
  let counter = 0;
  return createContractMock(syntheticTable(document), selection, {
    presets: EMPTY_PRESETS,
    requestId: () => `R${(counter += 1)}`,
  });
}

function expectEnvelope(payload: unknown, kind: 'success' | 'error') {
  expect(schemaErrors(envelope.$defs[kind], payload)).toEqual([]);
}

describe('projection', () => {
  it('loads the business projection', () => {
    const table = syntheticTable();
    expect(Object.keys(table.methods)).toEqual(['demo.get', 'demo.list', 'demo.write']);
    expect(table.digest).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(table.contractVersion).toBe('1');
    expect(availableScenarios(table.methods['demo.list'])).toEqual(['success', 'empty', 'error']);
    expect(table.methods['demo.write'].gaps.empty).toBeTruthy();
    expect(table.methods['demo.write'].mutates).toBe(true);
    expect(table.envelope.statusByCode.DAEMON_NOT_READY).toBe(503);
  });

  it('computes selectors from the result schema and never from identity keys', () => {
    const table = syntheticTable();
    expect(table.methods['demo.get'].selectors).toEqual(['id']);
    expect(table.methods['demo.list'].selectors).toEqual([]);
  });

  it('projects x-hyprial-mock-echo, drops echo keys from selectors, and rejects invalid echo rules', () => {
    const document = syntheticOpenapi();
    const operation = document.paths['/daemon/request/demo.write'].post;
    operation.requestBody.content['application/json'].schema.properties.idempotencyKey = { type: 'string' };
    operation.requestBody.content['application/json'].schema.properties.sharedTag = { type: 'string' };
    operation.responses['200'].content['application/json'].schema.properties.commandId = { type: 'string' };
    operation.responses['200'].content['application/json'].schema.properties.sharedTag = { type: 'string' };
    operation['x-hyprial-mock-echo'] = { idempotencyKey: 'commandId', sharedTag: 'sharedTag' };
    const table = syntheticTable(document);
    expect(table.methods['demo.write'].echo).toEqual({ idempotencyKey: 'commandId', sharedTag: 'sharedTag' });
    expect(table.methods['demo.write'].selectors).toEqual(['actor']);
    expect(table.methods['demo.get'].echo).toEqual({});

    const badTarget = clone(document);
    badTarget.paths['/daemon/request/demo.write'].post['x-hyprial-mock-echo'] = { idempotencyKey: 'missingField' };
    expect(() => syntheticTable(badTarget)).toThrow(/x-hyprial-mock-echo/);

    const badParam = clone(document);
    badParam.paths['/daemon/request/demo.write'].post['x-hyprial-mock-echo'] = { missingParam: 'commandId' };
    expect(() => syntheticTable(badParam)).toThrow(/x-hyprial-mock-echo/);

    for (const echo of [null, {}]) {
      const badEcho = clone(document);
      badEcho.paths['/daemon/request/demo.write'].post['x-hyprial-mock-echo'] = echo;
      expect(() => syntheticTable(badEcho)).toThrow(/x-hyprial-mock-echo/);
    }
  });

  it('rejects explicit null and empty declaration echo mappings while accepting a missing echo', () => {
    const source = syntheticOpenapi();
    const operation = source.paths['/daemon/request/demo.write'].post;
    const declaration = {
      name: 'demo.ipc',
      httpExposed: false,
      paramsSchema: clone(operation.requestBody.content['application/json'].schema),
      resultSchema: clone(operation.responses['200'].content['application/json'].schema),
      'x-hyprial-examples': clone(operation['x-hyprial-examples']),
    };
    for (const echo of [null, {}]) {
      const badDeclaration = clone(source);
      badDeclaration['x-hyprial-methods'] = [{ ...declaration, 'x-hyprial-mock-echo': echo }];
      expect(() => syntheticTable(badDeclaration)).toThrow(/x-hyprial-mock-echo/);
    }
    const missingDeclaration = clone(source);
    missingDeclaration['x-hyprial-methods'] = [declaration];
    expect(() => syntheticTable(missingDeclaration)).not.toThrow();
  });

  it('skips a method without examples instead of faking it', () => {
    const document = syntheticOpenapi();
    delete document.paths['/daemon/request/demo.list'].post['x-hyprial-examples'];
    const table = syntheticTable(document);
    expect(Object.keys(table.methods)).toEqual(['demo.get', 'demo.write']);
    expect(table.skipped).toEqual({ 'demo.list': 'no x-hyprial-examples' });
    const reply = mock({ scenario: 'success' }, document).handle('demo.list', {});
    expect(reply.status).toBe(404);
    expect(reply.payload.code).toBe('METHOD_NOT_FOUND');
  });

  it('blocks a document without any mockable method', () => {
    const document = syntheticOpenapi();
    for (const item of Object.values<any>(document.paths)) delete item.post['x-hyprial-examples'];
    expect(() => projectContract(document, envelope)).toThrow(/no mockable methods/);
  });

  it('refuses an operation whose id and path disagree', () => {
    const document = syntheticOpenapi();
    document.paths['/daemon/request/demo.list'].post.operationId = 'demo.other';
    expect(() => projectContract(document, envelope)).toThrow(ContractError);
  });

  it('refuses a params schema that uses an unsupported keyword', () => {
    const document = syntheticOpenapi();
    const schema = document.paths['/daemon/request/demo.list'].post.requestBody.content['application/json'].schema;
    schema.properties.filter.format = 'uuid';
    expect(() => projectContract(document, envelope)).toThrow(/unsupported JSON Schema keyword "format"/);
  });

  it('digests the canonical table, not the byte layout', () => {
    const projection = projectContract(syntheticOpenapi(), envelope);
    const reverse = (value: unknown): unknown =>
      Array.isArray(value)
        ? value.map(reverse)
        : value !== null && typeof value === 'object'
          ? Object.fromEntries(Object.entries(value).reverse().map(([key, item]) => [key, reverse(item)]))
          : value;
    expect(tableDigest(reverse(projection) as typeof projection)).toBe(tableDigest(projection));
    expect(withDigest(projection).digest).toBe(tableDigest(projection));
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
    expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(sha256Hex('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')).toBe(
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    );
    expect(canonicalJson({ b: [1, { d: 1, c: 'é' }], a: null })).toBe('{"a":null,"b":[1,{"c":"é","d":1}]}');
  });
});

describe('responses', () => {
  it('serves success in the pinned envelope', () => {
    const reply = mock().handle('demo.list', { filter: null });
    expect(reply.status).toBe(200);
    expect(reply.payload).toEqual({
      schemaVersion: 1,
      ok: true,
      method: 'demo.list',
      requestId: 'R1',
      dispatch: 'dispatched',
      result: { ok: true, items: ['alpha'] },
    });
    expectEnvelope(reply.payload, 'success');
  });

  it('serves empty as a legal success and unconfigured as an explicit refusal', () => {
    const empty = mock({ scenario: 'empty' });
    const reply = empty.handle('demo.list', { filter: 'missing' });
    expect(reply.status).toBe(200);
    expect((reply.payload.result as any).items).toEqual([]);
    const missing = empty.handle('demo.write', { actor: 'a', setupToken: 'example-not-a-credential' });
    expect(missing.status).toBe(409);
    expect(missing.payload.code).toBe('MOCK_SCENARIO_MISSING');
    expect(missing.payload.message).toBe(
      'demo.write has no empty example (available: success, error). Recorded reason: the write either returns its removal receipt or fails; it has no legal empty result.',
    );
    expectEnvelope(missing.payload, 'error');
  });

  it('dispatches a domain error with the transport status', () => {
    const reply = mock({ scenario: 'error' }).handle('demo.list', { filter: null });
    expect(reply.status).toBe(503);
    expect(reply.payload).toMatchObject({
      ok: false,
      code: 'DAEMON_NOT_READY',
      dispatch: 'dispatched',
      method: 'demo.list',
      data: { reason: 'restore' },
    });
    expectEnvelope(reply.payload, 'error');
  });

  it.each([
    ['absent', undefined],
    ['empty', {}],
    ['present', { reason: 'restore' }],
  ])('preserves %s optional error data exactly as the example declares it', (_label, data) => {
    const document = syntheticOpenapi();
    const examples = document.paths['/daemon/request/demo.list'].post['x-hyprial-examples'];
    const failing = examples.find((example: any) => example.name === 'unknown-filter');
    if (data === undefined) delete failing.error.data;
    else failing.error.data = data;
    const reply = mock({ scenario: 'error', cases: { 'demo.list': 'unknown-filter' } }, document).handle('demo.list', { filter: 'unknown' });
    expect(reply.payload.dispatch).toBe('dispatched');
    expect('data' in reply.payload).toBe(data !== undefined);
    if (data !== undefined) expect(reply.payload.data).toEqual(data);
    expectEnvelope(reply.payload, 'error');
  });

  it('refuses invalid params before dispatch and names the failing path', () => {
    const reply = mock().handle('demo.list', { filter: 5 });
    expect(reply.status).toBe(400);
    expect(reply.payload).toMatchObject({
      code: 'INVALID_ARGUMENT',
      dispatch: 'not-dispatched',
      method: 'demo.list',
      message: 'demo.list params do not match the declared schema at filter',
    });
    expectEnvelope(reply.payload, 'error');
    expect(mock().handle('demo.list', {}).payload.message).toBe(
      'demo.list params do not match the declared schema at <root>',
    );
  });

  it('answers a forced invalid-params case with its declared error whatever the params are', () => {
    for (const params of [{ filter: null }, { filter: 'x' }, { filter: 5 }, {}]) {
      const reply = mock({ scenario: 'success', cases: { 'demo.list': 'wrong-filter-type' } }).handle('demo.list', params);
      expect(reply.status).toBe(400);
      expect(reply.payload).toMatchObject({
        ok: false,
        code: 'INVALID_ARGUMENT',
        dispatch: 'not-dispatched',
        method: 'demo.list',
        message: 'demo.list params do not match the schema',
      });
      expect(reply.payload).not.toHaveProperty('result');
      expectEnvelope(reply.payload, 'error');
    }
  });

  it('refuses at load a table whose example could become a success without a result', () => {
    const noResult = syntheticTable();
    delete noResult.methods['demo.list'].examples.find((item) => item.kind === 'success')!.result;
    expect(() => createContractMock(noResult, { scenario: 'success' }, { presets: EMPTY_PRESETS })).toThrow(/needs a result object/);
    const unknown = syntheticTable();
    unknown.methods['demo.list'].examples[0].kind = 'mystery';
    expect(() => createContractMock(unknown, { scenario: 'success' }, { presets: EMPTY_PRESETS })).toThrow(/unknown example kind/);
    const badType = syntheticOpenapi();
    badType.paths['/daemon/request/demo.list'].post.requestBody.content['application/json'].schema.properties.filter.type = 'strng';
    expect(() => projectContract(badType, envelope)).toThrow(/unsupported JSON Schema type "strng"/);
  });

  it('fails closed with a host-side 500 when a table is altered after load', () => {
    const table = syntheticTable();
    const live = createContractMock(table, { scenario: 'success', cases: { 'demo.list': 'all' } }, { presets: EMPTY_PRESETS });
    const entry = table.methods['demo.list'].examples.find((item) => item.name === 'all')!;
    delete entry.result;
    const missing = live.handle('demo.list', { filter: null });
    expect(missing.status).toBe(500);
    expect(missing.payload).toMatchObject({ ok: false, code: 'MOCK_INTERNAL_ERROR', dispatch: 'not-dispatched' });
    entry.kind = 'mystery';
    expect(live.handle('demo.list', { filter: null }).payload.ok).toBe(false);
  });

  it('rejects a missing or blank HTTP key and accepts the same contract with a valid one', () => {
    const document = syntheticOpenapi();
    document.paths['/daemon/request/probe.submit'] = {
      post: {
        operationId: 'probe.submit',
        requestBody: {
          content: {
            'application/json': {
              schema: {
                type: 'object',
                properties: { key: { type: 'string', minLength: 1, pattern: '\\S' } },
                required: ['key'],
                additionalProperties: false,
              },
            },
          },
          required: true,
        },
        responses: {
          '200': { content: { 'application/json': { schema: { type: 'object', properties: { ok: { const: true } }, required: ['ok'] } } }, description: 'ok' },
        },
        'x-hyprial-examples': [
          { name: 'accepted', kind: 'success', description: 'synthetic accepted write', params: { key: 'mock-key' }, result: { ok: true } },
          { name: 'bad-body', kind: 'invalid-params', description: 'object required', params: [], error: { code: 'INVALID_ARGUMENT', message: 'object required' } },
        ],
        'x-hyprial-example-gaps': { empty: 'a write returns one result', domainErrors: 'synthetic write has no domain condition' },
        'x-hyprial-mutates': true,
      },
    };
    const served = mock({ scenario: 'success' }, document);
    for (const params of [{}, { key: null }, { key: ' ' }, { key: '\x1c' }]) {
      const reply = served.handle('probe.submit', params);
      expect(reply.status).toBe(400);
      expect(reply.payload.dispatch).toBe('not-dispatched');
      expectEnvelope(reply.payload, 'error');
    }
    const accepted = served.handle('probe.submit', { key: 'mock-key' });
    expect(accepted.status).toBe(200);
    expect(accepted.payload.result).toEqual({ ok: true });
  });

  it('answers an unknown method with 404 and no dispatch', () => {
    const reply = mock().handle('demo.missing', {});
    expect(reply.status).toBe(404);
    expect(reply.payload).toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched', message: 'unknown daemon method' });
  });

  it('refuses a non-object body before the schema', () => {
    for (const body of [[], null, 'x', 3]) {
      const reply = mock().handle('demo.list', body);
      expect(reply.status).toBe(400);
      expect(reply.payload).toMatchObject({ code: 'INVALID_ARGUMENT', dispatch: 'not-dispatched', message: 'params must be a JSON object' });
    }
  });

  it('serves a schema-valid semantic error as dispatched', () => {
    const reply = mock({ scenario: 'error', cases: { 'demo.list': 'unknown-filter' } }).handle('demo.list', { filter: 'unknown' });
    expect(reply.status).toBe(400);
    expect(reply.payload).toMatchObject({ code: 'INVALID_ARGUMENT', dispatch: 'dispatched' });
    expectEnvelope(reply.payload, 'error');
  });

  it('lets an explicit case win over the global scenario', () => {
    const reply = mock({ scenario: 'success', cases: { 'demo.list': 'daemon-restoring' } }).handle('demo.list', { filter: null });
    expect(reply.status).toBe(503);
    expect(reply.payload.code).toBe('DAEMON_NOT_READY');
  });

  it('cuts error messages to 512 code points without line breaks', () => {
    const document = syntheticOpenapi();
    const cases = document.paths['/daemon/request/demo.list'].post['x-hyprial-examples'];
    cases.find((item: any) => item.name === 'unknown-filter').error.message = `a\r\nb${'😀'.repeat(600)}`;
    const reply = mock({ scenario: 'error', cases: { 'demo.list': 'unknown-filter' } }, document).handle('demo.list', { filter: 'unknown' });
    const message = reply.payload.message as string;
    expect([...message]).toHaveLength(512);
    expect(message.startsWith('a  b')).toBe(true);
  });

  it('uses the default status for an unmapped code', () => {
    const document = syntheticOpenapi();
    const cases = document.paths['/daemon/request/demo.list'].post['x-hyprial-examples'];
    cases.find((item: any) => item.name === 'daemon-restoring').error.code = 'SOMETHING_NEW';
    const reply = mock({ scenario: 'error' }, document).handle('demo.list', { filter: null });
    expect(reply.status).toBe(500);
  });
});

describe('selection', () => {
  it('prefers the first case whose declared params agree', () => {
    const reply = mock().handle('demo.get', { id: 'two' });
    expect((reply.payload.result as any).id).toBe('two');
  });

  it('keeps the fixed example when only a non-selector disagrees', () => {
    const reply = mock().handle('demo.get', { id: 'one', note: 'typed by the caller', idempotencyKey: 'fresh-key' });
    expect(reply.status).toBe(200);
    expect(reply.payload.result).toEqual({ ok: true, id: 'one', idempotencyKey: 'key-1', entries: [{ text: 'fixed' }] });
  });

  it('echoes x-hyprial-mock-echo keys on success replies only and never mutates the example or errors', () => {
    const document = syntheticOpenapi();
    const operation = document.paths['/daemon/request/demo.write'].post;
    operation.requestBody.content['application/json'].schema = {
      type: 'object',
      properties: { idempotencyKey: { type: 'string' }, payload: { type: 'string' } },
      required: ['idempotencyKey'],
      additionalProperties: false,
    };
    operation.responses['200'].content['application/json'].schema = {
      type: 'object',
      properties: { ok: { const: true }, commandId: { type: 'string' } },
      required: ['ok'],
      additionalProperties: false,
    };
    operation['x-hyprial-mock-echo'] = { idempotencyKey: 'commandId' };
    operation['x-hyprial-examples'] = [
      {
        name: 'accepted',
        kind: 'success',
        description: 'accepted write with commandId',
        params: { idempotencyKey: 'k1', payload: 'hello' },
        result: { ok: true, commandId: 'k1' },
      },
      {
        name: 'without-command-id',
        kind: 'success',
        description: 'result variant without commandId',
        params: { idempotencyKey: 'k2', payload: 'skip' },
        result: { ok: true },
      },
      {
        name: 'write-refused',
        kind: 'domain-error',
        description: 'error reply stays fixed',
        params: { idempotencyKey: 'k1', payload: 'bad' },
        error: { code: 'INVALID_ARGUMENT', message: 'bad payload' },
      },
    ];
    const served = mock({ scenario: 'success' }, document);
    const echoed = served.handle('demo.write', { idempotencyKey: 'caller-key', payload: 'other' });
    expect(echoed.status).toBe(200);
    expect(echoed.payload.result).toEqual({ ok: true, commandId: 'caller-key' });
    expect(served.table.methods['demo.write'].examples[0].result).toEqual({ ok: true, commandId: 'k1' });

    const noTarget = createContractMock(
      served.table,
      { scenario: 'success', cases: { 'demo.write': 'without-command-id' } },
      { presets: EMPTY_PRESETS },
    );
    const skipped = noTarget.handle('demo.write', { idempotencyKey: 'caller-key', payload: 'skip' });
    expect(skipped.status).toBe(200);
    expect(skipped.payload.result).toEqual({ ok: true });

    const errored = mock({ scenario: 'error', cases: { 'demo.write': 'write-refused' } }, document).handle('demo.write', {
      idempotencyKey: 'caller-key',
      payload: 'bad',
    });
    expect(errored.status).toBe(400);
    expect(errored.payload).toMatchObject({ ok: false, code: 'INVALID_ARGUMENT' });
  });

  it('refuses a selector the examples do not describe', () => {
    const reply = mock().handle('demo.get', { id: 'three' });
    expect(reply.status).toBe(409);
    expect(reply.payload.code).toBe('MOCK_EXAMPLE_MISSING');
    expect(reply.payload.message).toBe(
      'demo.get has no success example that describes this request (selector fields: id); the Mock does not substitute another payload.',
    );
  });

  it('answers without params by the first case', () => {
    expect(mock().selectedCase('demo.get')?.name).toBe('first');
    expect(mock({ scenario: 'empty' }).selectedCase('demo.get')).toBeNull();
  });

  it('reports the gap reason for a scenario without cases', () => {
    const reply = mock({ scenario: 'error' }).handle('demo.get', { id: 'one' });
    expect(reply.payload.message).toBe('demo.get has no error example (available: success). Recorded reason: no declared domain error.');
  });
});

describe('selection overrides', () => {
  it('fails at construction when an explicit scenario has no case', () => {
    expect(() => mock({ scenario: 'success', methodScenarios: { 'demo.write': 'empty' } })).toThrow(/no empty example for the explicit override: the write/);
  });

  it('fails at construction on unknown methods, cases, scenarios and presets', () => {
    expect(() => mock({ scenario: 'success', methodScenarios: { 'no.such': 'success' } })).toThrow(/unknown method/);
    expect(() => mock({ scenario: 'success', cases: { 'demo.list': 'no-such-case' } })).toThrow(/no example case named/);
    expect(() => mock({ scenario: 'bogus' as never })).toThrow(/unknown scenario/);
    expect(() => mock({ preset: 'nope' })).toThrow(/unknown preset/);
    expect(() => mock({} as Selection)).toThrow(/scenario or a preset is required/);
  });

  it('applies a per-method scenario over the global one', () => {
    const served = mock({ scenario: 'success', methodScenarios: { 'demo.list': 'empty' } });
    expect((served.handle('demo.list', { filter: 'missing' }).payload.result as any).items).toEqual([]);
    expect(served.scenarioFor('demo.write')).toBe('success');
  });
});

describe('metadata', () => {
  it('reports scenarios, gaps and unsupported methods', () => {
    const served = mock({ scenario: 'empty' });
    const document = served.describe() as any;
    expect(document.mock).toBe(true);
    expect(document.scenario).toBe('empty');
    expect(document.methods['demo.list'].scenarios).toEqual(['success', 'empty', 'error']);
    expect(document.methods['demo.write'].gaps.empty).toBeTruthy();
    expect(document.unsupportedForScenario).toEqual(['demo.get', 'demo.write']);
    expect(document.skipped).toEqual({});
    expect(document.contractDigest).toBe(served.table.digest);
  });

  it('publishes the declared mutates of every method', () => {
    const document = mock({ scenario: 'success' }).describe() as any;
    expect(document.methods['demo.list'].mutates).toBe(false);
    expect(document.methods['demo.get'].mutates).toBe(false);
    expect(document.methods['demo.write'].mutates).toBe(true);
  });

  it('publishes mutates equal to x-hyprial-mutates for every method of the real contract', () => {
    const openapi = realOpenapi();
    const served = createContractMock(withDigest(projectContract(openapi, envelope)), { scenario: 'success' });
    const document = served.describe() as any;
    const declared: Record<string, boolean> = {};
    for (const [path, item] of Object.entries<any>(openapi.paths)) {
      const operation = item.post;
      const examples = operation?.['x-hyprial-examples'];
      if (Array.isArray(examples) && examples.length > 0) declared[path.slice('/daemon/request/'.length)] = operation['x-hyprial-mutates'] === true;
    }
    const ipcOnlyWithExamples = openapi['x-hyprial-methods']
      .filter((entry: any) => !entry.httpExposed && Array.isArray(entry['x-hyprial-examples']) && entry['x-hyprial-examples'].length > 0)
      .map((entry: any) => entry.name);
    expect(ipcOnlyWithExamples.length).toBeGreaterThan(0);
    for (const name of ipcOnlyWithExamples) expect(document.methods).not.toHaveProperty([name]);
    const published = Object.fromEntries(Object.entries<any>(document.methods).map(([name, entry]) => [name, entry.mutates]));
    expect(Object.keys(published).length).toBeGreaterThan(100);
    expect(published).toEqual(declared);
    expect(Object.values(published).every((value) => typeof value === 'boolean')).toBe(true);
    expect(new Set(Object.values(published))).toEqual(new Set([true, false]));
  });
});

describe('real projection', () => {
  const table = withDigest(projectContract(realOpenapi(), envelope));
  const served = createContractMock(table, { scenario: 'success' }, { requestId: () => 'R' });

  it('loads the real projection with no skipped methods', () => {
    expect(Object.keys(table.methods).length).toBeGreaterThan(100);
    expect(table.skipped).toEqual({});
    expect(table.methods['agent.provider.login'].gaps.empty).toBeTruthy();
    expect(availableScenarios(table.methods['agent.provider.default'])).toContain('unconfigured');
    expect(availableScenarios(table.methods['org.list'])).toContain('empty');
    expect(availableScenarios(table.methods['org.fetch'])).toContain('empty');
  });

  it.each([
    'agent.provider.allow', 'agent.provider.revoke', 'agent.provider.status',
    'user.provider.list', 'user.provider.login', 'user.provider.logout', 'user.provider.rotate',
  ])('serves the local-operator denial for %s', (method) => {
    const denied = createContractMock(table, { scenario: 'error', cases: { [method]: 'denied' } });
    const reply = denied.handle(method, denied.selectedCase(method)!.params);
    expect(reply.status).toBe(403);
    expect(reply.payload).toMatchObject({ ok: false, code: 'CALLER_NOT_AUTHORIZED' });
  });

  it('serves the first success case of every method with its own params', () => {
    for (const name of Object.keys(table.methods).sort()) {
      const example = served.selectedCase(name);
      expect(example, name).not.toBeNull();
      const reply = served.handle(name, example!.params);
      expect(reply.status, name).toBe(200);
      expect(reply.payload.ok, name).toBe(true);
      expectEnvelope(reply.payload, 'success');
    }
  });

  it('is deterministic for a clone of the document', () => {
    const again = withDigest(projectContract(clone(realOpenapi()), envelope));
    expect(again.digest).toBe(table.digest);
  });
});

describe('module hygiene', () => {
  const directory = fileURLToPath(new URL('../../contract-mock/', import.meta.url));
  const sources = readdirSync(directory).filter((name) => name.endsWith('.ts'));

  it('imports only sibling modules and Node built-ins', () => {
    for (const name of sources) {
      const text = readFileSync(`${directory}${name}`, 'utf8');
      for (const match of text.matchAll(/from\s+'([^']+)'/g)) {
        expect(match[1], `${name} imports ${match[1]}`).toMatch(/^(\.\/[a-z-]+\.(ts|json)|node:[a-z:/]+)$/);
      }
    }
  });

  it('writes every relative import with the .ts extension', () => {
    for (const name of sources) {
      const text = readFileSync(`${directory}${name}`, 'utf8');
      for (const match of text.matchAll(/from\s+'(\.[^']+)'/g)) expect(match[1]).toMatch(/\.(ts|json)$/);
    }
  });
});
