/**
 * Contract table for the Contract Mock: projection, digest and case selection.
 *
 * Decision record (1e-2, 2026-11-15)
 * - `contract/daemon-request/openapi.json` stays the only source of method
 *   facts. `projectContract` is the single code path that turns it, together
 *   with `http-envelope.schema.json`, into the table the mock serves: params
 *   schema, examples, gap reasons, selectors, mutation flag and the status map.
 *   The Node form applies it at start; the generator applies it once and the
 *   browser form imports the generated table. Nothing here is hand-listed.
 * - The scenario vocabulary is success, empty, unconfigured and error. The
 *   error scenario serves a declared domain error or a schema-valid semantic
 *   parameter error; the example kind says which boundary produced it.
 * - Selection prefers the first case whose declared params agree with the
 *   request (omitted keys are not conflicts). Failing that, a case that only
 *   disagrees on fields no published result speaks about may still answer, so
 *   caller-typed content keeps the fixed example. A field the result also
 *   speaks about is a selector: a request that contradicts it is refused as
 *   missing, never answered with another object. Caller-minted identity keys
 *   are never selectors.
 * - Decision update (2026-11-21): instead of keeping fixed published identity
 *   keys in success replies, methods declare `x-hyprial-mock-echo`
 *   (`paramKey -> resultKey`) on MethodSpec, projected into `MethodEntry.echo`.
 *   Keys listed in `echo` are never selectors. `respond.ts` copies caller-supplied
 *   values into a copy of success-kind results when both keys exist.
 * - The digest is the sha256 of the canonical projected table (sorted keys,
 *   compact, UTF-8). JavaScript cannot round-trip numbers such as `10.0`, so it
 *   differs from the digest the previous Python mock computed over the whole
 *   OpenAPI document; no consumer interprets its value.
 * - Load-time checks are structural (names, kinds, shapes) and keep a
 *   hand-edited artifact from becoming a plausible success. Schema validation
 *   of every example against its result and error schemas stays with the
 *   Python exporter and `check_contract`.
 * - Only methods with an HTTP path are projected (2026-11-16). The production
 *   host answers a declared IPC-only method with 404 `METHOD_NOT_FOUND`, so a
 *   mock that served its examples would report a success production refuses.
 */

import { assertSupportedSchema, deepEqual } from './schema.ts';

export const SCENARIOS = ['success', 'empty', 'unconfigured', 'error'] as const;
export type Scenario = (typeof SCENARIOS)[number];

export const SCENARIO_KINDS: Record<Scenario, readonly string[]> = {
  success: ['success'],
  empty: ['empty'],
  unconfigured: ['unconfigured'],
  error: ['domain-error', 'semantic-param-error'],
};

export const EXAMPLE_KINDS: readonly string[] = [
  'success',
  'empty',
  'unconfigured',
  'invalid-params',
  'semantic-param-error',
  'domain-error',
];

export const IDENTITY_PARAMS: readonly string[] = ['idempotencyKey', 'operationKey'];

const GAP_KEYS: Partial<Record<Scenario, string>> = {
  success: 'success',
  empty: 'empty',
  error: 'domainErrors',
};

export type Json = unknown;

export interface ExampleError {
  code: string;
  message: string;
  data?: Record<string, unknown>;
}

export interface ExampleCase {
  name: string;
  kind: string;
  description: string;
  params: Json;
  result?: Record<string, unknown>;
  error?: ExampleError;
  reason?: string;
}

export interface MethodEntry {
  path: string;
  paramsSchema: Record<string, unknown>;
  selectors: string[];
  echo: Record<string, string>;
  mutates: boolean;
  gaps: Record<string, string>;
  examples: ExampleCase[];
}

export interface ContractProjection {
  version: 1;
  openapiVersion: string;
  contractVersion: string;
  envelope: {
    statusByCode: Record<string, number>;
    statusDefault: number;
    source: Record<string, unknown>;
  };
  methods: Record<string, MethodEntry>;
  skipped: Record<string, string>;
}

export interface ContractTable extends ContractProjection {
  digest: string;
}

type Obj = Record<string, unknown>;

export class ContractError extends Error {}

function isObject(value: unknown): value is Obj {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function ensure(condition: boolean, message: string): asserts condition {
  if (!condition) throw new ContractError(message);
}

export function canonicalJson(value: Json): string {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new ContractError('canonical JSON cannot carry a non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (isObject(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  throw new ContractError('canonical JSON cannot carry this value');
}

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98,
  0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8,
  0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
  0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
  0xc67178f2,
]);

export function sha256Hex(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const padded = new Uint8Array((((bytes.length + 9 + 63) >> 6) << 6));
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor((bytes.length * 8) / 0x100000000));
  view.setUint32(padded.length - 4, (bytes.length * 8) >>> 0);
  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const w = new Uint32Array(64);
  const rotr = (value: number, bits: number) => (value >>> bits) | (value << (32 - bits));
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) w[index] = view.getUint32(offset + index * 4);
    for (let index = 16; index < 64; index += 1) {
      const s0 = rotr(w[index - 15], 7) ^ rotr(w[index - 15], 18) ^ (w[index - 15] >>> 3);
      const s1 = rotr(w[index - 2], 17) ^ rotr(w[index - 2], 19) ^ (w[index - 2] >>> 10);
      w[index] = (w[index - 16] + s0 + w[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const t1 = (h + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[index] + w[index]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + t1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) >>> 0;
    }
    const next = [a, b, c, d, e, f, g, h];
    for (let index = 0; index < 8; index += 1) state[index] = (state[index] + next[index]) >>> 0;
  }
  return Array.from(state, (word) => word.toString(16).padStart(8, '0')).join('');
}

export function tableDigest(projection: ContractProjection): string {
  return `sha256:${sha256Hex(canonicalJson(projection))}`;
}

export function withDigest(projection: ContractProjection): ContractTable {
  return { ...projection, digest: tableDigest(projection) };
}

function propertyNames(schema: unknown, names: Set<string>): Set<string> {
  if (Array.isArray(schema)) {
    for (const value of schema) propertyNames(value, names);
  } else if (isObject(schema)) {
    if (isObject(schema.properties)) for (const name of Object.keys(schema.properties)) names.add(name);
    for (const value of Object.values(schema)) propertyNames(value, names);
  }
  return names;
}

function selectorParams(paramsSchema: Obj, resultSchema: unknown, echo: Readonly<Record<string, string>>): string[] {
  if (!isObject(paramsSchema.properties)) return [];
  const claimed = propertyNames(resultSchema, new Set());
  return Object.keys(paramsSchema.properties).filter(
    (name) => claimed.has(name) && !IDENTITY_PARAMS.includes(name) && !Object.hasOwn(echo, name),
  );
}

export function checkExamples(name: string, examples: unknown): ExampleCase[] {
  ensure(Array.isArray(examples) && examples.length > 0, `${name}: x-hyprial-examples is missing`);
  const seen = new Set<string>();
  for (const entry of examples) {
    ensure(isObject(entry), `${name}: example is not an object`);
    const label = String(entry.name);
    ensure(typeof entry.name === 'string' && entry.name !== '', `${name}: example name`);
    ensure(!seen.has(entry.name), `${name}: duplicate example ${label}`);
    seen.add(entry.name);
    ensure(EXAMPLE_KINDS.includes(entry.kind as string), `${label}: unknown example kind ${String(entry.kind)}`);
    ensure(typeof entry.description === 'string' && entry.description !== '', `${label}: description is required`);
    ensure('params' in entry, `${label}: params is required`);
    if (entry.kind !== 'invalid-params') ensure(isObject(entry.params), `${label}: params must be an object`);
    if (['invalid-params', 'domain-error', 'semantic-param-error'].includes(entry.kind as string)) {
      ensure(isObject(entry.error) && typeof entry.error.code === 'string', `${label}: ${String(entry.kind)} needs an error`);
      ensure(!('result' in entry), `${label}: ${String(entry.kind)} cannot carry a result`);
    } else {
      ensure(isObject(entry.result), `${label}: ${String(entry.kind)} needs a result object`);
      ensure(!('error' in entry), `${label}: ${String(entry.kind)} cannot carry an error`);
    }
  }
  return examples as ExampleCase[];
}

function checkGaps(name: string, gaps: unknown): Record<string, string> {
  ensure(isObject(gaps), `${name}: x-hyprial-example-gaps is not an object`);
  return gaps as Record<string, string>;
}

function checkEcho(name: string, echo: unknown, paramsSchema: Obj, resultSchema: Obj): Record<string, string> {
  if (echo === undefined) return {};
  ensure(isObject(echo), `${name}: x-hyprial-mock-echo is not an object`);
  ensure(Object.keys(echo).length > 0, `${name}: x-hyprial-mock-echo must not be empty`);
  const paramProps = isObject(paramsSchema.properties) ? paramsSchema.properties : {};
  const resultProps = isObject(resultSchema.properties) ? resultSchema.properties : {};
  const checked: Record<string, string> = {};
  for (const paramKey of Object.keys(echo).sort()) {
    const resultKey = echo[paramKey];
    ensure(paramKey !== '' && Object.hasOwn(paramProps, paramKey), `${name}: x-hyprial-mock-echo param "${paramKey}" is not declared`);
    ensure(
      typeof resultKey === 'string' && resultKey !== '' && Object.hasOwn(resultProps, resultKey),
      `${name}: x-hyprial-mock-echo result "${String(resultKey)}" is not declared`,
    );
    checked[paramKey] = resultKey;
  }
  return checked;
}

function entryFor(
  name: string,
  path: string,
  paramsSchema: unknown,
  resultSchema: unknown,
  examples: unknown,
  gaps: unknown,
  mutates: unknown,
  echo: unknown,
): MethodEntry {
  ensure(isObject(paramsSchema), `${name}: params schema is missing`);
  ensure(isObject(resultSchema), `${name}: result schema is missing`);
  try {
    assertSupportedSchema(paramsSchema, `${name}.params`);
  } catch (error) {
    throw new ContractError(error instanceof Error ? error.message : String(error));
  }
  const checkedEcho = checkEcho(name, echo, paramsSchema, resultSchema);
  return {
    path,
    paramsSchema,
    selectors: selectorParams(paramsSchema, resultSchema, checkedEcho),
    echo: checkedEcho,
    mutates: mutates === true,
    gaps: checkGaps(name, gaps),
    examples: checkExamples(name, examples),
  };
}

function readSchema(content: unknown): unknown {
  return isObject(content) && isObject(content['application/json']) ? content['application/json'].schema : undefined;
}

export function projectContract(openapi: unknown, envelope: unknown): ContractProjection {
  ensure(isObject(openapi), 'business OpenAPI must be an object');
  ensure(openapi.openapi === '3.1.0', 'business OpenAPI must be 3.1.0');
  ensure(isObject(openapi.info), 'business OpenAPI has no info');
  ensure(isObject(openapi.paths), 'business OpenAPI has no paths');
  ensure(isObject(envelope), 'HTTP envelope schema must be an object');
  ensure(isObject(envelope.$defs), 'HTTP envelope schema has no $defs');
  const methods: Record<string, MethodEntry> = {};
  const skipped: Record<string, string> = {};
  for (const [path, item] of Object.entries(openapi.paths)) {
    ensure(isObject(item), `${path}: path item is not an object`);
    const operation = item.post;
    ensure(isObject(operation), `${path}: post operation is missing`);
    const name = operation.operationId;
    ensure(typeof name === 'string' && name !== '', `${path}: operationId is missing`);
    ensure(path === `/daemon/request/${name}`, `${path}: operationId/path mismatch`);
    const examples = operation['x-hyprial-examples'];
    if (!Array.isArray(examples) || examples.length === 0) {
      skipped[name] = 'no x-hyprial-examples';
      continue;
    }
    const responses = isObject(operation.responses) ? operation.responses : {};
    const ok = isObject(responses['200']) ? responses['200'] : {};
    methods[name] = entryFor(
      name,
      path,
      readSchema(isObject(operation.requestBody) ? operation.requestBody.content : undefined),
      readSchema(ok.content),
      examples,
      operation['x-hyprial-example-gaps'] ?? {},
      operation['x-hyprial-mutates'],
      operation['x-hyprial-mock-echo'],
    );
  }
  const declarations = openapi['x-hyprial-methods'];
  if (Array.isArray(declarations)) {
    for (const entry of declarations) {
      if (!isObject(entry)) continue;
      const name = entry.name;
      const examples = entry['x-hyprial-examples'];
      if (
        typeof name !== 'string' ||
        Object.hasOwn(methods, name) ||
        entry.httpExposed ||
        !Array.isArray(examples) ||
        examples.length === 0
      ) continue;
      const paramsSchema = entry.paramsSchema;
      const resultSchema = entry.resultSchema;
      ensure(isObject(paramsSchema), `${name}: params schema is missing`);
      ensure(isObject(resultSchema), `${name}: result schema is missing`);
      checkEcho(name, entry['x-hyprial-mock-echo'], paramsSchema, resultSchema);
    }
  }
  ensure(Object.keys(methods).length > 0, 'business OpenAPI has no mockable methods; no path has x-hyprial-examples');
  const sorted: Record<string, MethodEntry> = {};
  for (const name of Object.keys(methods).sort()) sorted[name] = methods[name];
  const sortedSkipped: Record<string, string> = {};
  for (const name of Object.keys(skipped).sort()) sortedSkipped[name] = skipped[name];
  const statuses = isObject(envelope['x-hyprial-status-by-code']) ? envelope['x-hyprial-status-by-code'] : {};
  const statusByCode: Record<string, number> = {};
  for (const [code, value] of Object.entries(statuses)) if (Number.isInteger(value)) statusByCode[code] = value as number;
  const fallback = envelope['x-hyprial-status-default'];
  return {
    version: 1,
    openapiVersion: openapi.openapi,
    contractVersion: String(openapi.info.version ?? ''),
    envelope: {
      statusByCode,
      statusDefault: Number.isInteger(fallback) ? (fallback as number) : 500,
      source: isObject(envelope['x-hyprial-source']) ? envelope['x-hyprial-source'] : {},
    },
    methods: sorted,
    skipped: sortedSkipped,
  };
}

export function casesFor(method: MethodEntry, scenario: Scenario): ExampleCase[] {
  const kinds = SCENARIO_KINDS[scenario];
  return method.examples.filter((example) => kinds.includes(example.kind));
}

export function gapFor(method: MethodEntry, scenario: Scenario): string | null {
  const key = GAP_KEYS[scenario];
  if (key === undefined) return null;
  const value = method.gaps[key];
  return typeof value === 'string' && value !== '' ? value : null;
}

export function availableScenarios(method: MethodEntry): Scenario[] {
  return SCENARIOS.filter((scenario) => casesFor(method, scenario).length > 0);
}

function agrees(example: ExampleCase, params: Obj, selectors: readonly string[] | null): boolean {
  if (!isObject(example.params)) return false;
  return Object.entries(example.params).every(
    ([key, value]) =>
      !Object.hasOwn(params, key) ||
      deepEqual(params[key], value) ||
      (selectors !== null && !selectors.includes(key)),
  );
}

export function selectCase(
  method: MethodEntry,
  scenario: Scenario,
  caseName?: string,
  params?: Obj,
): ExampleCase | null {
  if (caseName !== undefined) {
    const named = method.examples.find((example) => example.name === caseName);
    if (named === undefined) throw new ContractError(`no example case named "${caseName}"`);
    return named;
  }
  const candidates = casesFor(method, scenario);
  if (candidates.length === 0) return null;
  if (params === undefined) return candidates[0];
  return (
    candidates.find((example) => agrees(example, params, null)) ??
    candidates.find((example) => agrees(example, params, method.selectors)) ??
    null
  );
}
