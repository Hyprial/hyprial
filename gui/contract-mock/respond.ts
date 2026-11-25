/**
 * Contract Mock request handling: selection, presets, guards and envelopes.
 *
 * Decision record (1e-2, 2026-11-15)
 * - `handle(method, params)` is the whole mock behaviour and knows nothing of
 *   HTTP or `fetch`; the Node server and the in-browser form only frame it.
 *   Order: unknown method (404), non-object params (400), input guard, params
 *   schema (400, message names the first failing path), selection, error case
 *   (dispatched), success.
 * - A method without a case for the selected scenario answers 409
 *   `MOCK_SCENARIO_MISSING` with the recorded gap reason; a method with cases
 *   but none describing the request answers 409 `MOCK_EXAMPLE_MISSING`. Neither
 *   falls back to another payload.
 * - Behaviour that is policy rather than contract is data in `presets.json`
 *   and holds references only: named presets (a scenario plus per-method
 *   scenarios and cases), transitions (after a 200 reply to a method while a
 *   preset is current, switch to another preset) and input guards (a method
 *   accepts only parameters that equal documented synthetic values). This code
 *   names no daemon method, so a method rename cannot hide inside it.
 * - Explicit selection overrides sit on top of the current preset and survive
 *   a transition. Every reference resolves at construction or the mock throws.
 * - An example of kind `invalid-params` is the declared reply to a request the
 *   daemon refuses before dispatch. Forcing one by name (`cases`, `--case`)
 *   answers its declared error as 400 `not-dispatched` whatever the request
 *   params are, after the input guard and before the schema; it is never turned
 *   into a success (fixed after review: it used to become `ok:true` without a
 *   `result`). `handle` is exhaustive over example kinds: an unknown kind, or a
 *   success-like example without a `result` object, answers a 500
 *   `MOCK_INTERNAL_ERROR` (a host-side mock fault), never an `ok:true`
 *   envelope, and `createContractMock` rejects such a table at construction.
 * - Error messages lose CR/LF and are cut to 512 code points, the envelope
 *   bound. Replies are validated against the envelope schema by the tests,
 *   not at runtime.
 * - Decision update (2026-11-21): when serving a success-kind example
 *   (`success`, `empty`, `unconfigured`), `handle` copies each parameter named
 *   in `entry.echo` (`paramKey -> resultKey`) from `params` into a shallow copy
 *   of `example.result` when both `params` has `paramKey` and `example.result`
 *   has `resultKey`. Error examples and forced failure cases stay unchanged.
 */

import {
  ContractError,
  checkExamples,
  SCENARIOS,
  availableScenarios,
  casesFor,
  gapFor,
  selectCase,
} from './contract.ts';
import type { ContractTable, ExampleCase, MethodEntry, Scenario } from './contract.ts';
import { assertSupportedSchema, deepEqual, firstFailingPath } from './schema.ts';

export interface Selection {
  preset?: string;
  scenario?: Scenario;
  methodScenarios?: Record<string, Scenario>;
  cases?: Record<string, string>;
}

export interface PresetDefinition {
  scenario: Scenario;
  methodScenarios?: Record<string, Scenario>;
  cases?: Record<string, string>;
}

export interface GuardRequirement {
  path: readonly string[];
  equals: unknown;
}

export interface InputGuard {
  method: string;
  requireOneOf: GuardRequirement[];
  refusal: { code: string; message: string };
}

export interface PresetTransition {
  preset: string;
  method: string;
  next: string;
}

export interface PresetDocument {
  version: 1;
  presets: Record<string, PresetDefinition>;
  transitions: PresetTransition[];
  inputGuards: InputGuard[];
}

export interface MockOptions {
  presets?: PresetDocument;
  requestId?: () => string;
}

export interface MockResponse {
  status: number;
  payload: Record<string, unknown>;
}

export interface ContractMock {
  readonly table: ContractTable;
  handle(method: string, params: unknown): MockResponse;
  describe(): Record<string, unknown>;
  examples(method: string): readonly ExampleCase[];
  selectedCase(method: string, params?: Record<string, unknown>): ExampleCase | null;
  unsupportedMethods(): string[];
  scenarioFor(method: string): Scenario;
  statusFor(code: string): number;
  errorResponse(
    code: string,
    message: string,
    options?: { status?: number; dispatch?: string; method?: string; data?: Record<string, unknown> },
  ): MockResponse;
}

const SUCCESS_KINDS: readonly string[] = ['success', 'empty', 'unconfigured'];

export const MOCK_SCENARIO_MISSING = 'MOCK_SCENARIO_MISSING';
export const MOCK_EXAMPLE_MISSING = 'MOCK_EXAMPLE_MISSING';

export const EMPTY_PRESETS: PresetDocument = { version: 1, presets: {}, transitions: [], inputGuards: [] };

const PRESET_KEYS = ['version', 'presets', 'transitions', 'inputGuards'];
const PRESET_ENTRY_KEYS = ['scenario', 'methodScenarios', 'cases'];
const TRANSITION_KEYS = ['preset', 'method', 'next'];
const GUARD_KEYS = ['method', 'requireOneOf', 'refusal'];
const REQUIREMENT_KEYS = ['path', 'equals'];
const REFUSAL_KEYS = ['code', 'message'];

type Obj = Record<string, unknown>;

function isObject(value: unknown): value is Obj {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactKeys(value: unknown, allowed: readonly string[], where: string): Obj {
  if (!isObject(value)) throw new ContractError(`${where}: must be an object`);
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new ContractError(`${where}: unexpected key "${key}"`);
  }
  return value;
}

function isScenario(value: unknown): value is Scenario {
  return typeof value === 'string' && (SCENARIOS as readonly string[]).includes(value);
}

function randomId(): string {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(8));
  return `mock-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

function cleanMessage(value: string): string {
  return [...value.replace(/[\r\n]/g, ' ')].slice(0, 512).join('');
}

function echoResult(
  echo: Readonly<Record<string, string>> | undefined,
  params: Obj,
  result: Obj,
): Obj {
  if (echo === undefined) return result;
  const entries = Object.entries(echo);
  if (entries.length === 0) return result;
  let copied: Obj | null = null;
  for (const [paramKey, resultKey] of entries) {
    if (!Object.hasOwn(params, paramKey) || !Object.hasOwn(result, resultKey)) continue;
    if (copied === null) copied = { ...result };
    copied[resultKey] = structuredClone(params[paramKey]);
  }
  return copied ?? result;
}

function pathMatches(value: unknown, path: readonly string[], equals: unknown): boolean {
  if (path.length === 0) return deepEqual(value, equals);
  const [head, ...rest] = path;
  if (head === '*') {
    return Array.isArray(value) && value.length > 0 && value.every((item) => pathMatches(item, rest, equals));
  }
  return isObject(value) && Object.hasOwn(value, head) && pathMatches(value[head], rest, equals);
}

function resolveMethod(table: ContractTable, method: string, where: string): MethodEntry {
  const entry = table.methods[method];
  if (entry === undefined) throw new ContractError(`${where}: unknown method "${method}"`);
  return entry;
}

function checkSelection(table: ContractTable, value: PresetDefinition | Selection, where: string): void {
  if (value.scenario !== undefined && !isScenario(value.scenario)) {
    throw new ContractError(`${where}: unknown scenario "${String(value.scenario)}"`);
  }
  for (const [method, scenario] of Object.entries(value.methodScenarios ?? {})) {
    const entry = resolveMethod(table, method, where);
    if (!isScenario(scenario)) throw new ContractError(`${where}: unknown scenario "${String(scenario)}"`);
    if (casesFor(entry, scenario).length === 0) {
      const gap = gapFor(entry, scenario);
      throw new ContractError(
        `${where}: ${method} has no ${scenario} example for the explicit override${gap ? `: ${gap}` : ''}`,
      );
    }
  }
  for (const [method, name] of Object.entries(value.cases ?? {})) {
    const entry = resolveMethod(table, method, where);
    if (!entry.examples.some((example) => example.name === name)) {
      throw new ContractError(`${where}: ${method} has no example case named "${name}"`);
    }
  }
}

function validatePresets(document: PresetDocument, table: ContractTable): void {
  exactKeys(document, PRESET_KEYS, 'presets');
  if (document.version !== 1) throw new ContractError('presets: unsupported version');
  for (const [name, preset] of Object.entries(document.presets)) {
    exactKeys(preset, PRESET_ENTRY_KEYS, `preset ${name}`);
    if (!isScenario(preset.scenario)) throw new ContractError(`preset ${name}: scenario is required`);
    checkSelection(table, preset, `preset ${name}`);
  }
  for (const row of document.transitions) {
    exactKeys(row, TRANSITION_KEYS, 'transition');
    if (!(row.preset in document.presets)) throw new ContractError(`transition: unknown preset "${row.preset}"`);
    if (!(row.next in document.presets)) throw new ContractError(`transition: unknown preset "${row.next}"`);
    resolveMethod(table, row.method, 'transition');
  }
  for (const guard of document.inputGuards) {
    exactKeys(guard, GUARD_KEYS, 'input guard');
    resolveMethod(table, guard.method, 'input guard');
    exactKeys(guard.refusal, REFUSAL_KEYS, 'input guard refusal');
    if (!Array.isArray(guard.requireOneOf) || guard.requireOneOf.length === 0) {
      throw new ContractError('input guard: requireOneOf must be a non-empty list');
    }
    for (const requirement of guard.requireOneOf) {
      exactKeys(requirement, REQUIREMENT_KEYS, 'input guard requirement');
      if (!Array.isArray(requirement.path) || requirement.path.length === 0) {
        throw new ContractError('input guard: path must be a non-empty list');
      }
    }
  }
}

export function createContractMock(table: ContractTable, selection: Selection, options: MockOptions = {}): ContractMock {
  const presets = options.presets ?? EMPTY_PRESETS;
  const nextId = options.requestId ?? randomId;
  validatePresets(presets, table);
  for (const [name, entry] of Object.entries(table.methods)) {
    assertSupportedSchema(entry.paramsSchema, `${name}.params`);
    checkExamples(name, entry.examples);
  }
  if (selection.preset !== undefined && !(selection.preset in presets.presets)) {
    throw new ContractError(`unknown preset "${selection.preset}"`);
  }
  if (selection.scenario === undefined && selection.preset === undefined) {
    throw new ContractError('a scenario or a preset is required');
  }
  checkSelection(table, selection, 'selection');
  let current = selection.preset;

  const effective = (): PresetDefinition => {
    const preset = current === undefined ? undefined : presets.presets[current];
    return {
      scenario: selection.scenario ?? preset?.scenario ?? 'success',
      methodScenarios: { ...preset?.methodScenarios, ...selection.methodScenarios },
      cases: { ...preset?.cases, ...selection.cases },
    };
  };
  const scenarioFor = (method: string): Scenario => {
    const state = effective();
    return state.methodScenarios?.[method] ?? state.scenario;
  };
  const caseNameFor = (method: string): string | undefined => effective().cases?.[method];
  const selectedCase = (method: string, params?: Record<string, unknown>): ExampleCase | null =>
    selectCase(table.methods[method], scenarioFor(method), caseNameFor(method), params);
  const statusFor = (code: string): number => table.envelope.statusByCode[code] ?? table.envelope.statusDefault;

  const errorResponse: ContractMock['errorResponse'] = (code, message, extra = {}) => {
    const payload: Record<string, unknown> = {
      schemaVersion: 1,
      ok: false,
      requestId: nextId(),
      code,
      message: cleanMessage(message),
      dispatch: extra.dispatch ?? 'not-dispatched',
    };
    if (extra.method !== undefined) payload.method = extra.method;
    if (extra.data !== undefined) payload.data = extra.data;
    return { status: extra.status ?? statusFor(code), payload };
  };

  const missingCase = (method: string): MockResponse => {
    const entry = table.methods[method];
    const scenario = scenarioFor(method);
    if (casesFor(entry, scenario).length === 0) {
      const gap = gapFor(entry, scenario);
      const available = availableScenarios(entry).join(', ') || 'none';
      return errorResponse(
        MOCK_SCENARIO_MISSING,
        `${method} has no ${scenario} example (available: ${available}).${gap ? ` Recorded reason: ${gap}.` : ''}`,
        { status: 409, method },
      );
    }
    const selectors = [...entry.selectors].sort().join(', ') || 'none';
    return errorResponse(
      MOCK_EXAMPLE_MISSING,
      `${method} has no ${scenario} example that describes this request (selector fields: ${selectors}); the Mock does not substitute another payload.`,
      { status: 409, method },
    );
  };

  const hostFault = (method: string, example: ExampleCase): MockResponse =>
    errorResponse(
      'MOCK_INTERNAL_ERROR',
      `${method}/${example.name}: the mock cannot answer an example of kind "${String(example.kind)}" as declared`,
      { status: 500, method },
    );

  const declaredError = (method: string, example: ExampleCase, dispatch: string): MockResponse => {
    const error = example.error;
    if (error === undefined || typeof error.code !== 'string') return hostFault(method, example);
    const status = dispatch === 'not-dispatched' && example.kind === 'invalid-params' ? 400 : undefined;
    return errorResponse(error.code, error.message, { status, dispatch, method, data: error.data });
  };

  const handle = (method: string, params: unknown): MockResponse => {
    const entry = table.methods[method];
    if (entry === undefined) {
      return errorResponse('METHOD_NOT_FOUND', 'unknown daemon method', { status: 404, method });
    }
    const forced = caseNameFor(method);
    const forcedCase = forced === undefined ? undefined : entry.examples.find((item) => item.name === forced);
    const forcedInvalid = forcedCase !== undefined && forcedCase.kind === 'invalid-params';
    if (!isObject(params)) {
      if (forcedInvalid) return declaredError(method, forcedCase, 'not-dispatched');
      return errorResponse('INVALID_ARGUMENT', 'params must be a JSON object', { status: 400, method });
    }
    for (const guard of presets.inputGuards) {
      if (guard.method !== method) continue;
      if (!guard.requireOneOf.some((rule) => pathMatches(params, rule.path, rule.equals))) {
        return errorResponse(guard.refusal.code, guard.refusal.message, { method });
      }
    }
    if (forcedInvalid) return declaredError(method, forcedCase, 'not-dispatched');
    const failing = firstFailingPath(entry.paramsSchema, params);
    if (failing !== null) {
      return errorResponse(
        'INVALID_ARGUMENT',
        `${method} params do not match the declared schema at ${failing.join('/') || '<root>'}`,
        { status: 400, method },
      );
    }
    const example = selectedCase(method, params);
    if (example === null) return missingCase(method);
    if (example.kind === 'domain-error' || example.kind === 'semantic-param-error') {
      return declaredError(method, example, 'dispatched');
    }
    if (example.kind === 'invalid-params') return declaredError(method, example, 'not-dispatched');
    if (!SUCCESS_KINDS.includes(example.kind) || !isObject(example.result)) {
      return hostFault(method, example);
    }
    const response: MockResponse = {
      status: 200,
      payload: {
        schemaVersion: 1,
        ok: true,
        method,
        requestId: nextId(),
        dispatch: 'dispatched',
        result: echoResult(entry.echo, params, example.result),
      },
    };
    if (current !== undefined) {
      const row = presets.transitions.find((item) => item.preset === current && item.method === method);
      if (row !== undefined) current = row.next;
    }
    return response;
  };

  const unsupportedMethods = (): string[] =>
    Object.keys(table.methods)
      .sort()
      .filter((method) => selectedCase(method) === null);

  const describe = (): Record<string, unknown> => {
    const methods: Record<string, unknown> = {};
    for (const [name, entry] of Object.entries(table.methods)) {
      methods[name] = { path: entry.path, mutates: entry.mutates, scenarios: availableScenarios(entry), gaps: { ...entry.gaps } };
    }
    const state = effective();
    const sortedEntries = (value: Record<string, string> | undefined) =>
      Object.fromEntries(Object.entries(value ?? {}).sort(([left], [right]) => (left < right ? -1 : 1)));
    return {
      mock: true,
      contractVersion: table.contractVersion,
      contractDigest: table.digest,
      openapiVersion: table.openapiVersion,
      scenario: state.scenario,
      preset: current ?? null,
      methodScenarios: sortedEntries(state.methodScenarios),
      methodCases: sortedEntries(state.cases),
      unsupportedForScenario: unsupportedMethods(),
      envelopeSource: table.envelope.source,
      methods,
      skipped: { ...table.skipped },
    };
  };

  return {
    table,
    handle,
    describe,
    examples: (method) => table.methods[method]?.examples ?? [],
    selectedCase,
    unsupportedMethods,
    scenarioFor,
    statusFor,
    errorResponse,
  };
}
