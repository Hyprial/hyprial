/**
 * JSON-Schema subset validator for the Contract Mock.
 *
 * Decision record (1e-2, 2026-11-15)
 * - The Contract Mock validates request params against the declared
 *   `paramsSchema` of each method. The published params schemas use a small
 *   keyword set; this module implements exactly that set and nothing else so
 *   the mock stays one dependency-free module that runs in the browser and
 *   under plain Node.
 * - A keyword outside the supported set or the annotation set throws at load
 *   (`assertSupportedSchema`). A daemon change that introduces a new keyword
 *   or `format` therefore fails the gate until this file learns it, instead of
 *   the mock silently accepting what it cannot check.
 * - Errors carry instance paths. The mock reports the first failing path in
 *   element-wise sorted order (a shorter prefix sorts first), which is the
 *   order the previous Python mock used.
 * - `anyOf`, `oneOf`, `not` and `if` evaluate their subschemas into a scratch
 *   list and report at the current path; `allOf`, `then` and `else` descend
 *   with the same path.
 * - Lengths count code points, patterns search (not anchor), and equality is
 *   deep and independent of key order.
 * - Patterns answer like the daemon, which validates with Python `jsonschema`
 *   and `re`, not like ECMAScript: `pattern.ts` translates each pattern at
 *   load to Python semantics (`$` also matches before a trailing newline, Python's
 *   whitespace set for `\s`, `.` excluding only \n) and throws on a construct
 *   it cannot translate faithfully. The first comparison with the Python mock
 *   probed no such input; see docs/evidence/gui-contract-mock-js-parity-2026-11-15.md
 *   ("after review").
 * - `type` must name a known JSON type at load, so a typo is a load error and
 *   not a request-time 500.
 */

import { compilePattern } from './pattern.ts';

export type JsonPath = readonly (string | number)[];
type Json = unknown;
type SchemaObject = Record<string, unknown>;

const TYPE_NAMES: readonly string[] = ['string', 'number', 'integer', 'boolean', 'null', 'array', 'object'];
const ANNOTATIONS = new Set(['description', 'examples', 'default', 'writeOnly', 'title']);
const SUBSCHEMA_KEYWORDS = new Set(['additionalProperties', 'items', 'not', 'if', 'then', 'else']);
const SUBSCHEMA_LIST_KEYWORDS = new Set(['allOf', 'anyOf', 'oneOf']);
const VALUE_KEYWORDS = new Set([
  'type',
  'required',
  'enum',
  'const',
  'minItems',
  'maxItems',
  'uniqueItems',
  'minLength',
  'maxLength',
  'pattern',
  'minimum',
  'maximum',
  'exclusiveMinimum',
]);

function isObject(value: unknown): value is SchemaObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function deepEqual(left: Json, right: Json): boolean {
  if (left === right) return true;
  if (typeof left !== typeof right || left === null || right === null) return false;
  if (Array.isArray(left)) {
    return (
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((item, index) => deepEqual(item, right[index]))
    );
  }
  if (typeof left !== 'object' || Array.isArray(right)) return false;
  const leftKeys = Object.keys(left as SchemaObject);
  const rightKeys = Object.keys(right as SchemaObject);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) =>
        Object.hasOwn(right as SchemaObject, key) &&
        deepEqual((left as SchemaObject)[key], (right as SchemaObject)[key]),
    )
  );
}

export function assertSupportedSchema(schema: unknown, where = '<root>'): void {
  if (typeof schema === 'boolean') return;
  if (!isObject(schema)) throw new Error(`${where}: a schema must be an object`);
  for (const [keyword, value] of Object.entries(schema)) {
    const here = `${where}/${keyword}`;
    if (keyword === 'type') {
      for (const type of Array.isArray(value) ? value : [value]) {
        if (typeof type !== 'string' || !TYPE_NAMES.includes(type)) throw new Error(`${here}: unsupported JSON Schema type "${String(type)}"`);
      }
      continue;
    }
    if (keyword === 'pattern') {
      if (typeof value !== 'string') throw new Error(`${here}: pattern must be a string`);
      compilePattern(value, here);
      continue;
    }
    if (ANNOTATIONS.has(keyword) || VALUE_KEYWORDS.has(keyword)) continue;
    if (keyword === 'properties') {
      if (!isObject(value)) throw new Error(`${here}: properties must be an object`);
      for (const [name, child] of Object.entries(value)) assertSupportedSchema(child, `${here}/${name}`);
    } else if (SUBSCHEMA_KEYWORDS.has(keyword)) {
      assertSupportedSchema(value, here);
    } else if (SUBSCHEMA_LIST_KEYWORDS.has(keyword)) {
      if (!Array.isArray(value)) throw new Error(`${here}: must be an array`);
      value.forEach((child, index) => assertSupportedSchema(child, `${here}/${index}`));
    } else {
      throw new Error(`${where}: unsupported JSON Schema keyword "${keyword}"`);
    }
  }
}

function typeMatches(type: string, value: Json): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string';
    case 'number':
      return typeof value === 'number';
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value);
    case 'boolean':
      return typeof value === 'boolean';
    case 'null':
      return value === null;
    case 'array':
      return Array.isArray(value);
    case 'object':
      return isObject(value);
    default:
      throw new Error(`unsupported JSON Schema type "${type}"`);
  }
}

function codePoints(value: string): number {
  let count = 0;
  for (const _ of value) count += 1;
  return count;
}

function accepts(schema: unknown, value: Json): boolean {
  const scratch: (string | number)[][] = [];
  collect(schema, value, [], scratch);
  return scratch.length === 0;
}

function collect(schema: unknown, value: Json, path: (string | number)[], out: (string | number)[][]): void {
  if (schema === true) return;
  if (schema === false) {
    out.push(path);
    return;
  }
  if (!isObject(schema)) throw new Error('a schema must be an object');
  for (const [keyword, rule] of Object.entries(schema)) {
    switch (keyword) {
      case 'type': {
        const types = Array.isArray(rule) ? rule : [rule];
        if (!types.some((type) => typeMatches(String(type), value))) out.push(path);
        break;
      }
      case 'enum':
        if (!(rule as Json[]).some((item) => deepEqual(item, value))) out.push(path);
        break;
      case 'const':
        if (!deepEqual(rule, value)) out.push(path);
        break;
      case 'required':
        if (isObject(value)) {
          for (const name of rule as string[]) if (!Object.hasOwn(value, name)) out.push(path);
        }
        break;
      case 'properties':
        if (isObject(value)) {
          for (const [name, child] of Object.entries(rule as SchemaObject)) {
            if (Object.hasOwn(value, name)) collect(child, value[name], [...path, name], out);
          }
        }
        break;
      case 'additionalProperties':
        if (isObject(value)) {
          const declared = isObject(schema.properties) ? schema.properties : {};
          const extras = Object.keys(value).filter((name) => !Object.hasOwn(declared, name));
          if (rule === false) {
            if (extras.length > 0) out.push(path);
          } else if (rule !== true) {
            for (const name of extras) collect(rule, value[name], [...path, name], out);
          }
        }
        break;
      case 'items':
        if (Array.isArray(value)) {
          value.forEach((item, index) => collect(rule, item, [...path, index], out));
        }
        break;
      case 'minItems':
        if (Array.isArray(value) && value.length < (rule as number)) out.push(path);
        break;
      case 'maxItems':
        if (Array.isArray(value) && value.length > (rule as number)) out.push(path);
        break;
      case 'uniqueItems':
        if (rule === true && Array.isArray(value)) {
          const repeated = value.some((item, index) => value.findIndex((other) => deepEqual(item, other)) !== index);
          if (repeated) out.push(path);
        }
        break;
      case 'minLength':
        if (typeof value === 'string' && codePoints(value) < (rule as number)) out.push(path);
        break;
      case 'maxLength':
        if (typeof value === 'string' && codePoints(value) > (rule as number)) out.push(path);
        break;
      case 'pattern':
        if (typeof value === 'string' && !compilePattern(rule as string).test(value)) out.push(path);
        break;
      case 'minimum':
        if (typeof value === 'number' && value < (rule as number)) out.push(path);
        break;
      case 'maximum':
        if (typeof value === 'number' && value > (rule as number)) out.push(path);
        break;
      case 'exclusiveMinimum':
        if (typeof value === 'number' && value <= (rule as number)) out.push(path);
        break;
      case 'allOf':
        for (const child of rule as unknown[]) collect(child, value, path, out);
        break;
      case 'anyOf':
        if (!(rule as unknown[]).some((child) => accepts(child, value))) out.push(path);
        break;
      case 'oneOf':
        if ((rule as unknown[]).filter((child) => accepts(child, value)).length !== 1) out.push(path);
        break;
      case 'not':
        if (accepts(rule, value)) out.push(path);
        break;
      case 'if':
        if (accepts(rule, value)) {
          if ('then' in schema) collect(schema.then, value, path, out);
        } else if ('else' in schema) {
          collect(schema.else, value, path, out);
        }
        break;
      case 'then':
      case 'else':
        break;
      default:
        if (!ANNOTATIONS.has(keyword)) throw new Error(`unsupported JSON Schema keyword "${keyword}"`);
    }
  }
}

export function schemaErrors(schema: unknown, value: Json): JsonPath[] {
  const out: (string | number)[][] = [];
  collect(schema, value, [], out);
  return out;
}

function comparePart(left: string | number, right: string | number): number {
  if (typeof left === typeof right) return left < right ? -1 : left > right ? 1 : 0;
  return typeof left === 'number' ? -1 : 1;
}

export function comparePaths(left: JsonPath, right: JsonPath): number {
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const order = comparePart(left[index], right[index]);
    if (order !== 0) return order;
  }
  return left.length - right.length;
}

export function firstFailingPath(schema: unknown, value: Json): JsonPath | null {
  const errors = schemaErrors(schema, value);
  if (errors.length === 0) return null;
  return errors.sort(comparePaths)[0];
}
