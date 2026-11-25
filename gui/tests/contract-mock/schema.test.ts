// @vitest-environment node
/**
 * Contract Mock JSON-Schema subset: keyword behaviour, paths and load-time refusal.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Every supported keyword has a passing and a failing row; every keyword the
 *   published params schemas do not use must be refused at load, so a daemon
 *   change cannot slip past the mock unchecked.
 */

import { describe, expect, it } from 'vitest';
import {
  assertSupportedSchema,
  comparePaths,
  deepEqual,
  firstFailingPath,
  schemaErrors,
} from '../../contract-mock/index.ts';
import { realOpenapi } from './synthetic.ts';

const rows: [string, unknown, unknown, unknown][] = [
  ['type string', { type: 'string' }, 'x', 1],
  ['type integer', { type: 'integer' }, 3, 3.5],
  ['type union', { type: ['string', 'null'] }, null, 4],
  ['type object rejects array', { type: 'object' }, {}, []],
  ['type number rejects string', { type: 'number' }, 1.5, '1'],
  ['type boolean', { type: 'boolean' }, true, 0],
  ['enum', { enum: ['a', 2] }, 2, 'b'],
  ['const', { const: { a: [1] } }, { a: [1] }, { a: [2] }],
  ['required', { required: ['a'] }, { a: 1 }, {}],
  ['properties', { properties: { a: { type: 'string' } } }, { a: 'x' }, { a: 1 }],
  ['additionalProperties false', { properties: { a: {} }, additionalProperties: false }, { a: 1 }, { a: 1, b: 2 }],
  ['additionalProperties schema', { additionalProperties: { type: 'integer' } }, { a: 1 }, { a: 'x' }],
  ['items', { items: { type: 'integer' } }, [1, 2], [1, 'x']],
  ['minItems', { minItems: 2 }, [1, 2], [1]],
  ['maxItems', { maxItems: 1 }, [1], [1, 2]],
  ['uniqueItems', { uniqueItems: true }, [{ a: 1, b: 2 }, { a: 1 }], [{ a: 1, b: 2 }, { b: 2, a: 1 }]],
  ['minLength counts code points', { minLength: 2 }, '😀😀', '😀'],
  ['maxLength counts code points', { maxLength: 1 }, '😀', '😀😀'],
  ['pattern searches', { pattern: '^a' }, 'abc', 'bca'],
  ['minimum', { minimum: 0 }, 0, -1],
  ['maximum', { maximum: 5 }, 5, 6],
  ['exclusiveMinimum', { exclusiveMinimum: 0 }, 1, 0],
  ['allOf', { allOf: [{ type: 'string' }, { minLength: 2 }] }, 'ab', 'a'],
  ['anyOf', { anyOf: [{ type: 'string' }, { type: 'integer' }] }, 4, null],
  ['oneOf', { oneOf: [{ type: 'integer' }, { minimum: 3 }] }, 1, 5],
  ['not', { not: { type: 'string' } }, 1, 'x'],
  ['if/then', { if: { properties: { a: { const: 1 } }, required: ['a'] }, then: { required: ['b'] } }, { a: 2 }, { a: 1 }],
  ['if/else', { if: { type: 'string' }, else: { type: 'integer' } }, 3, true],
];

describe('supported keywords', () => {
  it.each(rows)('%s', (_name, schema, passing, failing) => {
    expect(schemaErrors(schema, passing)).toEqual([]);
    expect(schemaErrors(schema, failing).length).toBeGreaterThan(0);
  });

  it('ignores annotations', () => {
    const schema = { description: 'x', examples: [1], default: 2, writeOnly: true, title: 't', type: 'string' };
    expect(() => assertSupportedSchema(schema)).not.toThrow();
    expect(schemaErrors(schema, 'ok')).toEqual([]);
  });
});

describe('load-time refusal', () => {
  it.each([
    ['format', { type: 'string', format: 'uuid' }],
    ['$ref', { $ref: '#/$defs/x' }],
    ['patternProperties', { patternProperties: { '^a': {} } }],
    ['dependentRequired', { dependentRequired: { a: ['b'] } }],
    ['nested unknown keyword', { properties: { a: { anyOf: [{ contains: {} }] } } }],
    ['exclusiveMaximum', { exclusiveMaximum: 3 }],
  ])('%s is refused', (_name, schema) => {
    expect(() => assertSupportedSchema(schema)).toThrow(/unsupported JSON Schema keyword/);
  });

  it('does not mistake a property named like a keyword for a keyword', () => {
    expect(() => assertSupportedSchema({ properties: { format: { type: 'string' }, $ref: {} } })).not.toThrow();
  });

  it('accepts every published params schema', () => {
    const document = realOpenapi();
    for (const [path, item] of Object.entries<any>(document.paths)) {
      const schema = item.post.requestBody.content['application/json'].schema;
      expect(() => assertSupportedSchema(schema, path)).not.toThrow();
    }
  });
});

describe('failing paths', () => {
  const schema = {
    type: 'object',
    properties: {
      a: { type: 'object', properties: { b: { type: 'integer' } } },
      list: { type: 'array', items: { type: 'string' } },
      z: {},
    },
    required: ['z'],
    additionalProperties: false,
  };

  it('reports the instance path of an item', () => {
    expect(firstFailingPath(schema, { z: 1, list: ['x', 2] })).toEqual(['list', 1]);
  });

  it('sorts element-wise and puts a shorter prefix first', () => {
    expect(firstFailingPath(schema, { a: { b: 'x' }, list: [1], q: 1 })).toEqual([]);
    expect(firstFailingPath(schema, { z: 1, a: { b: 'x' }, list: [1] })).toEqual(['a', 'b']);
    expect(comparePaths(['a'], ['a', 'b'])).toBeLessThan(0);
    expect(comparePaths([1], [0, 5])).toBeGreaterThan(0);
    expect(comparePaths([2], ['a'])).toBeLessThan(0);
  });

  it('returns null for a valid value', () => {
    expect(firstFailingPath(schema, { z: 1 })).toBeNull();
  });

  it('treats deep equality as independent of key order', () => {
    expect(deepEqual({ a: 1, b: [1, { c: 2, d: 3 }] }, { b: [1, { d: 3, c: 2 }], a: 1 })).toBe(true);
    expect(deepEqual({ a: 1 }, { a: 1, b: undefined })).toBe(false);
    expect(deepEqual([1], { 0: 1 })).toBe(false);
    expect(deepEqual(1, true)).toBe(false);
  });
});
