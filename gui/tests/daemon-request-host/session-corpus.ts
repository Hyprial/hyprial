/**
 * Decision record (GUI session contract v1, 2026-11-15)
- This helper is the only reader of contract/gui-session/gui-session.v1.json
  for the vitest side. The host conformance suite and the contract tests share
  it so the corpus is parsed in one place.
- The schema checker implements only the keywords the corpus uses and throws on
  any other keyword. loadSessionContract() walks every subschema of every
  corpus schema once (properties, $defs, items, oneOf, allOf, if, then), so an
  unsupported keyword is refused even where no example reaches it. Unknown
  response properties are ignored by design: the contract is additive.
- Keywords next to a $ref apply as well, as in draft 2020-12, and required and
  properties look at own properties only.
- It deliberately has no dependency on any host implementation: it serves the
  Python host's HTTP suite, the only host since the Node host was deleted.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export const REPOSITORY_ROOT = resolve(fileURLToPath(new URL('../../..', import.meta.url)));
export const CORPUS_PATH = resolve(REPOSITORY_ROOT, 'contract/gui-session/gui-session.v1.json');
export const README_PATH = resolve(REPOSITORY_ROOT, 'contract/gui-session/README.md');

export function loadSessionContract(): JsonObject {
  const corpus = JSON.parse(readFileSync(CORPUS_PATH, 'utf8')) as JsonObject;
  for (const [name, schema] of Object.entries(corpus.schemas as JsonObject)) {
    assertSchemaSupported(schema as JsonObject, name);
  }
  return corpus;
}

const ANNOTATIONS = new Set(['$schema', '$id', '$defs', 'title', 'description', 'examples', '$comment']);
const KEYWORDS = new Set([
  '$ref', 'type', 'const', 'enum', 'required', 'properties', 'items', 'minItems',
  'minLength', 'minimum', 'pattern', 'oneOf', 'allOf', 'if', 'then',
]);

function isSchema(value: Json | undefined): value is JsonObject {
  return value !== undefined && value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function assertSchemaSupported(schema: JsonObject, path = '$'): void {
  for (const key of Object.keys(schema)) {
    if (!ANNOTATIONS.has(key) && !KEYWORDS.has(key)) throw new Error(`unsupported schema keyword ${key} at ${path}`);
  }
  if (schema.$ref !== undefined && (typeof schema.$ref !== 'string' || !schema.$ref.startsWith('#/$defs/'))) {
    throw new Error(`unsupported $ref at ${path}`);
  }
  for (const group of ['properties', '$defs'] as const) {
    const children = schema[group];
    if (children === undefined) continue;
    if (!isSchema(children)) throw new Error(`${group} must be an object at ${path}`);
    for (const [name, child] of Object.entries(children)) {
      if (!isSchema(child)) throw new Error(`${group}.${name} must be a schema at ${path}`);
      assertSchemaSupported(child, `${path}.${group}.${name}`);
    }
  }
  for (const key of ['items', 'if', 'then'] as const) {
    const child = schema[key];
    if (child === undefined) continue;
    if (!isSchema(child)) throw new Error(`${key} must be a schema at ${path}`);
    assertSchemaSupported(child, `${path}.${key}`);
  }
  for (const key of ['oneOf', 'allOf'] as const) {
    const children = schema[key];
    if (children === undefined) continue;
    if (!Array.isArray(children)) throw new Error(`${key} must be an array at ${path}`);
    children.forEach((child, index) => {
      if (!isSchema(child)) throw new Error(`${key}[${index}] must be a schema at ${path}`);
      assertSchemaSupported(child, `${path}.${key}[${index}]`);
    });
  }
}

function typeOf(value: Json): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function matches(type: string, value: Json): boolean {
  if (type === 'integer') return typeof value === 'number' && Number.isInteger(value);
  if (type === 'number') return typeof value === 'number';
  return typeOf(value) === type;
}

function resolveRef(root: JsonObject, ref: string): JsonObject {
  const prefix = '#/$defs/';
  if (!ref.startsWith(prefix)) throw new Error(`unsupported $ref ${ref}`);
  const target = (root.$defs as JsonObject | undefined)?.[ref.slice(prefix.length)];
  if (target === undefined || typeof target !== 'object' || target === null || Array.isArray(target)) {
    throw new Error(`unresolved $ref ${ref}`);
  }
  return target;
}

export function validateSchema(schema: JsonObject, value: Json, root: JsonObject = schema, path = '$'): string[] {
  const problems: string[] = [];
  assertSchemaSupported(schema, path);
  if (typeof schema.$ref === 'string') {
    problems.push(...validateSchema(resolveRef(root, schema.$ref), value, root, path));
  }
  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type as string[] : [schema.type as string];
    if (!types.some((type) => matches(type, value))) {
      problems.push(`${path}: expected ${types.join('|')}, got ${typeOf(value)}`);
      return problems;
    }
  }
  if ('const' in schema && JSON.stringify(schema.const) !== JSON.stringify(value)) {
    problems.push(`${path}: expected const ${JSON.stringify(schema.const)}`);
  }
  if (Array.isArray(schema.enum) && !schema.enum.some((item) => JSON.stringify(item) === JSON.stringify(value))) {
    problems.push(`${path}: not one of ${JSON.stringify(schema.enum)}`);
  }
  if (typeof value === 'string') {
    if (typeof schema.minLength === 'number' && value.length < schema.minLength) problems.push(`${path}: shorter than ${schema.minLength}`);
    if (typeof schema.pattern === 'string' && !new RegExp(schema.pattern).test(value)) problems.push(`${path}: does not match ${schema.pattern}`);
  }
  if (typeof value === 'number' && typeof schema.minimum === 'number' && value < schema.minimum) {
    problems.push(`${path}: below ${schema.minimum}`);
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === 'number' && value.length < schema.minItems) problems.push(`${path}: fewer than ${schema.minItems} items`);
    if (schema.items !== undefined) {
      value.forEach((item, index) => {
        problems.push(...validateSchema(schema.items as JsonObject, item, root, `${path}[${index}]`));
      });
    }
  }
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    for (const name of (schema.required as string[] | undefined) ?? []) {
      if (!Object.prototype.hasOwnProperty.call(value, name)) problems.push(`${path}: missing ${name}`);
    }
    for (const [name, child] of Object.entries((schema.properties as JsonObject | undefined) ?? {})) {
      if (Object.prototype.hasOwnProperty.call(value, name)) problems.push(...validateSchema(child as JsonObject, value[name], root, `${path}.${name}`));
    }
  }
  if (Array.isArray(schema.oneOf)) {
    const passing = (schema.oneOf as JsonObject[]).filter((branch) => validateSchema(branch, value, root, path).length === 0);
    if (passing.length !== 1) problems.push(`${path}: matched ${passing.length} of ${schema.oneOf.length} alternatives`);
  }
  if (Array.isArray(schema.allOf)) {
    for (const branch of schema.allOf as JsonObject[]) problems.push(...validateSchema(branch, value, root, path));
  }
  if (schema.if !== undefined && schema.then !== undefined
      && validateSchema(schema.if as JsonObject, value, root, path).length === 0) {
    problems.push(...validateSchema(schema.then as JsonObject, value, root, path));
  }
  return problems;
}
