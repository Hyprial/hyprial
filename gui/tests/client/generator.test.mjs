import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  GeneratorError,
  checkDirectory,
  collectMethods,
  generateToDirectory,
  renderClient,
  unwrapOpenApi,
} from '../../scripts/generate-client.mjs';

const fixturePath = resolve('tests/client/fixtures/http-openapi.generic.json');
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
const scratchDirectories = [];

async function scratch() {
  const directory = await mkdtemp(join(tmpdir(), 'h02-generator-'));
  scratchDirectories.push(directory);
  return directory;
}

function reverseKeys(value) {
  if (Array.isArray(value)) return value.map(reverseKeys);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .reverse()
        .map(([key, child]) => [key, reverseKeys(child)]),
    );
  }
  return value;
}

function addMethod(contract, name) {
  contract.paths[`/daemon/request/${name}`] = {
    post: {
      'x-hyprial-method': name,
      'x-hyprial-state': 'callable',
      'x-hyprial-retry': 'never',
      'x-hyprial-mutates': false,
      requestBody: {
        required: true,
        content: { 'application/json': { schema: { type: 'object', properties: {} } } },
      },
      responses: {
        200: {
          description: 'ok',
          content: {
            'application/json': {
              schema: {
                type: 'object',
                properties: {
                  result: {
                    type: 'object',
                    properties: { value: { type: 'string' } },
                    required: ['value'],
                  },
                },
                required: ['result'],
              },
            },
          },
        },
      },
    },
  };
}

afterEach(async () => {
  await Promise.all(scratchDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe('HTTP OpenAPI client generator', () => {
  it('preserves null in a nullable object with properties', () => {
    const contract = structuredClone(fixture);
    addMethod(contract, 'demo.nullable');
    contract.paths['/daemon/request/demo.nullable'].post.responses[200].content['application/json'].schema.properties.result.properties.value = {
      type: ['object', 'null'], properties: { name: { type: 'string' } }, required: ['name'], additionalProperties: false,
    };
    expect(renderClient(contract).files['types.ts']).toContain('value: null | { name: string }');
  });
  it('discovers every declared method from the served projection instead of a whitelist', () => {
    const methods = collectMethods(fixture);

    expect(methods.map((method) => method.name)).toEqual([
      'agent.list',
      'agent.provider.default',
      'agent.provider.login',
      'agent.provider.logout',
      'agent.secret.list',
      'demo.ping',
    ]);
    expect(methods.find((method) => method.name === 'agent.provider.logout')).toMatchObject({
      path: '/daemon/request/agent.provider.logout',
      mutates: true,
      retry: 'never',
      state: 'callable',
    });
  });

  it('renders deterministic types and nested namespace entries', () => {
    const first = renderClient(fixture);
    const second = renderClient(fixture);
    const reordered = renderClient(reverseKeys(fixture));

    expect(first.files).toEqual(second.files);
    expect(first.files).toEqual(reordered.files);
    expect(first.files['index.ts']).toContain('agent: {');
    expect(first.files['index.ts']).toContain('provider: {');
    expect(first.files['index.ts']).toContain("transport.invoke<AgentProviderLogoutResult>('agent.provider.logout'");
    expect(first.files['index.ts']).toContain("transport.invoke<AgentSecretListResult>('agent.secret.list'");
    expect(first.files['index.ts']).toContain('retry: \'never\'');
    expect(first.files['types.ts']).toContain('export interface AgentProviderLoginParams');
    expect(first.files['types.ts']).toContain('contentBase64: string;');
    expect(first.files['types.ts']).toContain('environmentNames: Array<string>;');
    expect(first.files['types.ts']).toContain("'agent.provider.logout': {");
  });

  it('adds a new nested namespace when the contract grows, without editing the generator', () => {
    const grown = structuredClone(fixture);
    grown.paths['/daemon/request/internal.diagnostics'] = {
      post: {
        'x-hyprial-method': 'internal.diagnostics',
        'x-hyprial-state': 'callable',
        'x-hyprial-retry': 'safe',
        'x-hyprial-mutates': false,
        requestBody: {
          required: true,
          content: { 'application/json': { schema: { type: 'object', properties: {} } } },
        },
        responses: {
          200: {
            description: 'ok',
            content: {
              'application/json': {
                schema: {
                  type: 'object',
                  properties: {
                    result: {
                      type: 'object',
                      properties: { uptimeSeconds: { type: 'integer' } },
                      required: ['uptimeSeconds'],
                    },
                  },
                  required: ['result'],
                },
              },
            },
          },
        },
      },
    };

    const rendered = renderClient(grown);

    expect(rendered.methods.map((method) => method.name)).toContain('internal.diagnostics');
    expect(rendered.files['index.ts']).toContain('internal: {');
    expect(rendered.files['index.ts']).toContain('diagnostics(');
    expect(rendered.files['types.ts']).toContain('export interface InternalDiagnosticsResult');
  });

  it('unwraps the transport envelope around /openapi.json before reading the contract', () => {
    const wrapped = {
      schemaVersion: 1,
      ok: true,
      requestId: 'req-openapi',
      dispatch: 'dispatched',
      result: fixture,
    };

    const unwrapped = unwrapOpenApi(wrapped);

    expect(unwrapped.openapi).toBe('3.1.0');
    expect(renderClient(wrapped).files).toEqual(renderClient(fixture).files);
  });

  it('writes files and --check reports no drift on a fresh generation', async () => {
    const directory = await scratch();

    const written = await generateToDirectory(fixture, directory);
    const drift = await checkDirectory(fixture, directory);

    expect(written.sort()).toEqual(['index.ts', 'types.ts']);
    expect(drift).toEqual([]);
    const index = await readFile(join(directory, 'index.ts'), 'utf8');
    expect(index).toContain('DO NOT EDIT');
    expect(index).toContain("retry: 'safe'");
  });

  it('--check reports the exact drifted file and does not rewrite it', async () => {
    const directory = await scratch();
    await generateToDirectory(fixture, directory);
    const mutated = '// local edit that must be reported\n';
    await writeFile(join(directory, 'types.ts'), mutated, 'utf8');

    const drift = await checkDirectory(fixture, directory);

    expect(drift).toEqual(['types.ts']);
    expect(await readFile(join(directory, 'types.ts'), 'utf8')).toBe(mutated);
  });

  it('rejects a method mounted outside /daemon/request/<canonical-method>', () => {
    const drifted = structuredClone(fixture);
    drifted.paths['/agents/list'] = {
      post: {
        'x-hyprial-method': 'agent.list',
        responses: { 200: { description: 'ok', content: { 'application/json': { schema: { type: 'object' } } } } },
      },
    };

    expect(() => collectMethods(drifted)).toThrowError(GeneratorError);
    expect(() => collectMethods(drifted)).toThrowError(/NON_CANONICAL_PATH/);
  });

  it('rejects a path whose method does not match x-hyprial-method', () => {
    const drifted = structuredClone(fixture);
    drifted.paths['/daemon/request/agent.list'].post['x-hyprial-method'] = 'agent.secret.list';

    expect(() => collectMethods(drifted)).toThrowError(/METHOD_PATH_MISMATCH/);
  });

  it('renders a callable method that also owns a namespace', () => {
    const drifted = structuredClone(fixture);
    drifted.paths['/daemon/request/agent'] = {
      post: {
        'x-hyprial-method': 'agent',
        'x-hyprial-retry': 'never',
        'x-hyprial-mutates': false,
        requestBody: { required: true, content: { 'application/json': { schema: { type: 'object', properties: {} } } } },
        responses: {
          200: {
            description: 'ok',
            content: {
              'application/json': {
                schema: { type: 'object', properties: { result: { type: 'object', properties: {} } }, required: ['result'] },
              },
            },
          },
        },
      },
    };

    expect(collectMethods(drifted).map((method) => method.name)).toContain('agent');
    const index = renderClient(drifted).files['index.ts'];
    expect(index).toContain('agent: Object.assign(');
    expect(index).toContain("transport.invoke<AgentResult>('agent'");
    expect(index).toContain("transport.invoke<AgentListResult>('agent.list'");
  });

  it('rejects colliding generated identifiers across namespaces', () => {
    const colliding = structuredClone(fixture);
    addMethod(colliding, 'foo.bar-baz');
    addMethod(colliding, 'foo.bar_baz');

    expect(() => collectMethods(colliding)).toThrowError(/IDENTIFIER_COLLISION/);
  });

  it('renders a deeper callable namespace without losing its children', () => {
    const drifted = structuredClone(fixture);
    addMethod(drifted, 'agent.provider');

    const index = renderClient(drifted).files['index.ts'];
    expect(index).toContain('provider: Object.assign(');
    expect(index).toContain("transport.invoke<AgentProviderResult>('agent.provider'");
    expect(index).toContain("transport.invoke<AgentProviderLogoutResult>('agent.provider.logout'");
  });

  it('reads identity from x-hyprial-http-binding when the flat extension is absent', () => {
    const contract = structuredClone(fixture);
    const post = contract.paths['/daemon/request/agent.provider.logout'].post;
    delete post['x-hyprial-identity'];
    post['x-hyprial-http-binding'] = { identity: { mode: 'local-owner-user-selector', param: 'actor' } };

    const method = collectMethods(contract).find((entry) => entry.name === 'agent.provider.logout');

    expect(method.identity).toEqual({ mode: 'local-owner-user-selector', param: 'actor' });
  });

  it('rejects dangerous generated property names', () => {
    const drifted = structuredClone(fixture);
    addMethod(drifted, 'agent.constructor');

    expect(() => collectMethods(drifted)).toThrowError(/UNSAFE_PROPERTY_NAME/);
  });

  it('rejects a method segment that is not a TypeScript identifier', () => {
    const drifted = structuredClone(fixture);
    addMethod(drifted, 'agent.1provider');

    expect(() => collectMethods(drifted)).toThrowError(/INVALID_IDENTIFIER/);
  });

  it('refuses to generate a mutating method with the safe read retry policy', () => {
    const drifted = structuredClone(fixture);
    drifted.paths['/daemon/request/agent.provider.logout'].post['x-hyprial-retry'] = 'safe';

    expect(() => collectMethods(drifted)).toThrowError(/UNSAFE_RETRY/);
  });

  it('refuses an empty contract instead of generating a silent empty client', () => {
    expect(() => collectMethods({ openapi: '3.1.0', paths: {} })).toThrowError(/NO_METHODS/);
  });

  it('accepts a success envelope composed with allOf', () => {
    const composed = structuredClone(fixture);
    const operation = composed.paths['/daemon/request/demo.ping'].post;
    const original = operation.responses['200'].content['application/json'].schema;
    operation.responses['200'].content['application/json'].schema = {
      allOf: [
        {
          type: 'object',
          properties: {
            schemaVersion: { type: 'integer' },
            ok: { type: 'boolean' },
            method: { type: 'string' },
            requestId: { type: 'string' },
            dispatch: { type: 'string' },
          },
          required: ['schemaVersion', 'ok', 'method', 'requestId', 'dispatch'],
        },
        { type: 'object', properties: { result: original.properties.result }, required: ['result'] },
      ],
    };

    const rendered = renderClient(composed);

    expect(rendered.files['types.ts']).toContain('export interface DemoPingResult');
  });

  it('preserves default-open, true, typed and false additionalProperties in a real tsc consumer', async () => {
    const contract = structuredClone(fixture);
    for (const name of ['demo.accept', 'demo.defaultOpen', 'demo.trueOpen', 'demo.closed']) {
      addMethod(contract, name);
    }
    const jsonValue = {
      type: ['string', 'number', 'boolean', 'null', 'object', 'array'],
    };
    const params = (schema) => ({
      required: true,
      content: { 'application/json': { schema } },
    });
    contract.paths['/daemon/request/demo.accept'].post.requestBody = params({
      type: 'object',
      properties: {
        known: { type: 'string' },
        nested: {
          type: 'object',
          properties: { a: { type: 'string' } },
          additionalProperties: { type: 'number' },
        },
      },
      required: ['known'],
      additionalProperties: jsonValue,
    });
    contract.paths['/daemon/request/demo.defaultOpen'].post.requestBody = params({
      type: 'object',
      properties: { known: { type: 'string' } },
      required: ['known'],
    });
    contract.paths['/daemon/request/demo.trueOpen'].post.requestBody = params({
      type: 'object',
      properties: { known: { type: 'string' } },
      required: ['known'],
      additionalProperties: true,
    });
    contract.paths['/daemon/request/demo.closed'].post.requestBody = params({
      type: 'object',
      properties: { known: { type: 'string' } },
      required: ['known'],
      additionalProperties: false,
    });

    const rendered = renderClient(contract);
    const directory = await scratch();
    const typesPath = join(directory, 'types.ts');
    const consumerPath = join(directory, 'consumer.ts');
    await writeFile(typesPath, rendered.files['types.ts'], 'utf8');
    await writeFile(
      consumerPath,
      [
        "import type { DemoAcceptParams, DemoClosedParams, DemoDefaultOpenParams, DemoTrueOpenParams } from './types';",
        "const accept: DemoAcceptParams = { known: 'ok', legacyIgnoredKey: 123, nested: { a: 'x', extraNested: 42 } };",
        "const defaultOpen: DemoDefaultOpenParams = { known: 'ok', legacyIgnoredKey: 'x' };",
        "const trueOpen: DemoTrueOpenParams = { known: 'ok', anything: { deep: true } };",
        '// @ts-expect-error additionalProperties:false must reject an unknown key',
        "const closed: DemoClosedParams = { known: 'ok', extra: 1 };",
        'void accept; void defaultOpen; void trueOpen; void closed;',
        '',
      ].join('\n'),
      'utf8',
    );
    const tsc = resolve('node_modules/typescript/bin/tsc');
    const result = spawnSync(
      process.execPath,
      [
        tsc,
        '--noEmit',
        '--strict',
        '--skipLibCheck',
        '--target',
        'ES2022',
        '--module',
        'ESNext',
        '--moduleResolution',
        'Bundler',
        typesPath,
        consumerPath,
      ],
      { encoding: 'utf8' },
    );

    expect(result.status, `${result.stdout}\n${result.stderr}`).toBe(0);
  });

  it('refuses a success projection without a business result schema', () => {
    const broken = structuredClone(fixture);
    delete broken.paths['/daemon/request/agent.list'].post.responses['200'];

    expect(() => collectMethods(broken)).toThrowError(/MISSING_RESULT_SCHEMA/);
  });
});
