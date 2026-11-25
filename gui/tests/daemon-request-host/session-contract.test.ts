// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import {
  CORPUS_PATH,
  README_PATH,
  assertSchemaSupported,
  loadSessionContract,
  validateSchema,
  type Json,
  type JsonObject,
} from './session-corpus';

const DISPATCH = ['not-dispatched', 'dispatched', 'unknown'];

function section(corpus: JsonObject, name: string): JsonObject {
  return corpus[name] as JsonObject;
}

describe('GUI session contract v1 corpus', () => {
  it('is published next to its README at contract/gui-session/', () => {
    expect(existsSync(CORPUS_PATH)).toBe(true);
    expect(existsSync(README_PATH)).toBe(true);
    const corpus = loadSessionContract();
    expect(corpus.contract).toBe('hyprial.gui-session');
    expect(corpus.sessionContractVersion).toBe(1);
  });

  it('keeps the README within its line budget and names every route, header, cookie and error code', () => {
    const corpus = loadSessionContract();
    const readme = readFileSync(README_PATH, 'utf8');
    expect(readme.split('\n').length).toBeLessThanOrEqual(1000);
    expect(readFileSync(CORPUS_PATH, 'utf8').split('\n').length).toBeLessThanOrEqual(1000);
    const names: string[] = [];
    for (const route of corpus.routes as JsonObject[]) names.push(route.path as string);
    const headers = section(corpus, 'headers');
    for (const list of [headers.request, headers.response] as JsonObject[][]) {
      for (const entry of list) names.push(entry.name as string);
    }
    const cookies = section(corpus, 'cookies');
    names.push((cookies.control as JsonObject).name as string, (cookies.session as JsonObject).name as string);
    for (const error of corpus.errors as JsonObject[]) names.push(error.code as string);
    names.push(section(section(corpus, 'static'), 'healthz').path as string);
    const missing = names.filter((name) => !readme.includes(name));
    expect(missing).toEqual([]);
  });

  it('has a closed, internally consistent error table', () => {
    const corpus = loadSessionContract();
    const errors = corpus.errors as JsonObject[];
    const codes = errors.map((error) => error.code as string);
    expect(new Set(codes).size).toBe(codes.length);
    for (const error of errors) {
      expect(error.code).toMatch(/^[A-Z][A-Z0-9_]+$/);
      for (const status of error.status as number[]) {
        expect(status).toBeGreaterThanOrEqual(400);
        expect(status).toBeLessThan(600);
      }
      for (const dispatch of error.dispatch as string[]) expect(DISPATCH).toContain(dispatch);
    }
    for (const route of corpus.routes as JsonObject[]) {
      for (const code of route.emits as string[]) expect(codes, `${route.id} emits ${code}`).toContain(code);
    }
    const refusal = section(section(corpus, 'versioning'), 'refusal');
    expect(codes).toContain(refusal.code);
  });

  it('validates its own examples against its own schemas and rejects broken ones', () => {
    const corpus = loadSessionContract();
    const schemas = section(corpus, 'schemas');
    const examples = section(corpus, 'examples');
    const pairs: Array<[string, string]> = [
      ['sessionResponse', 'sessionResponseReady'],
      ['sessionResponse', 'sessionResponseBootstrap'],
      ['sessionStatusResponse', 'sessionStatusResponse'],
      ['logoutResponse', 'logoutResponse'],
      ['attachResponse', 'attachResponse'],
      ['hostError', 'hostError'],
    ];
    for (const [schema, example] of pairs) {
      expect(validateSchema(schemas[schema] as JsonObject, examples[example] as Json), `${example}`).toEqual([]);
    }
    const ready = JSON.parse(JSON.stringify(examples.sessionResponseReady)) as JsonObject;
    const catalog = ready.catalog as JsonObject;
    expect(new Set((catalog.methods as JsonObject[]).map((method) => method.name)).size).toBe(catalog.methodCount);
    delete ready.csrf;
    expect(validateSchema(schemas.sessionResponse as JsonObject, ready)).toContain('$: missing csrf');
    const bootstrap = JSON.parse(JSON.stringify(examples.sessionResponseBootstrap)) as JsonObject;
    bootstrap.principal = { kind: 'serving', owner: 'a', node: 'b', actor: 'user:a' };
    expect(validateSchema(schemas.sessionResponse as JsonObject, bootstrap)).not.toEqual([]);
  });
});

describe('the schema checker', () => {
  it('refuses an unsupported keyword anywhere in a schema, including unvisited properties and unused $defs', () => {
    const unvisited: JsonObject = { type: 'object', properties: { a: { type: 'string', maxLength: 3 } } };
    expect(() => assertSchemaSupported(unvisited)).toThrow(/maxLength/);
    expect(() => validateSchema(unvisited, {})).toThrow(/maxLength/);
    const unused: JsonObject = { type: 'object', $defs: { unused: { additionalProperties: false } } };
    expect(() => assertSchemaSupported(unused)).toThrow(/additionalProperties/);
    const nested: JsonObject = { allOf: [{ if: { type: 'string' }, then: { items: { else: {} } } }] };
    expect(() => assertSchemaSupported(nested)).toThrow(/else/);
    expect(() => assertSchemaSupported({ $ref: 'http://example.invalid/x' })).toThrow(/\$ref/);
    for (const schema of Object.values(loadSessionContract().schemas as JsonObject)) {
      expect(() => assertSchemaSupported(schema as JsonObject)).not.toThrow();
    }
  });

  it('applies keywords that sit next to a $ref and looks at own properties only', () => {
    const sibling: JsonObject = { $ref: '#/$defs/text', minLength: 3, $defs: { text: { type: 'string' } } };
    expect(validateSchema(sibling, 'abc')).toEqual([]);
    expect(validateSchema(sibling, 'ab')).toEqual(['$: shorter than 3']);
    expect(validateSchema(sibling, 5)).not.toEqual([]);
    const inherited: JsonObject = { type: 'object', required: ['constructor', 'toString'], properties: { constructor: { type: 'string' } } };
    expect(validateSchema(inherited, {})).toEqual(['$: missing constructor', '$: missing toString']);
    expect(validateSchema(inherited, { constructor: 'x', toString: 'y' })).toEqual([]);
    expect(validateSchema(inherited, { constructor: 1, toString: 'y' })).toEqual(['$.constructor: expected string, got number']);
  });

  it('pins the serving principal shape: actor is the user URI, not the owner', () => {
    const corpus = loadSessionContract();
    const schema = section(corpus, 'schemas').sessionResponse as JsonObject;
    const ready = JSON.parse(JSON.stringify(section(corpus, 'examples').sessionResponseReady)) as JsonObject;
    expect(validateSchema(schema, ready)).toEqual([]);
    (ready.principal as JsonObject).actor = 'owner';
    expect(validateSchema(schema, ready)).not.toEqual([]);
  });
});
