// @vitest-environment node
/**
 * Contract Mock conformance against the real contract.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Contract to JavaScript: every declared case of every method is served by
 *   the Node form, pinned with `cases`, exactly as declared: result or error,
 *   the status from the envelope status map, the dispatch state. Every reply,
 *   and every refusal for a missing case, validates against the pinned HTTP
 *   envelope `$defs`. A scenario without a case answers 409 with the recorded
 *   gap reason, never with another payload.
 * - Two forms agree: the Node form behind the real same-origin Python host (a child
 *   process, a real session, real cookies) and the in-browser form give equal status, envelope
 *   and catalog-digest header for the same probes, for four scenarios and three
 *   override sets, and equal session and catalog documents.
 * - Red proofs: the comparator must report an altered result, an altered
 *   status and a missing header; an oracle that states the selection rule in
 *   the plainest terms must accept the real selection and reject a deliberately
 *   wrong rule (last match wins) on the real table.
 * - The probes are the ones the one-time Python parity run used: empty
 *   params, every case's params, one probe per selector, a type probe and a
 *   removal probe for the first keys of a valid base, an unknown key and an
 *   unknown method.
 */

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import {
  MOCK_ORIGIN,
  SCENARIOS,
  casesFor,
  createContractMock,
  createContractMockFetch,
  deepEqual,
  defaultPresets,
  EMPTY_PRESETS,
  projectContract,
  schemaErrors,
  selectCase,
  withDigest,
} from '../../contract-mock/index.ts';
import type { ContractTable, ExampleCase, MethodEntry, Scenario, Selection } from '../../contract-mock/index.ts';
import { startContractMockServer } from '../../contract-mock/serve.ts';
import { HOST_TEST_TIMEOUT_MS, startPythonHost } from '../support/python-host';
import { SESSION_VOLATILE, differences, observe } from './compare.ts';
import type { Observed } from './compare.ts';
import { realEnvelope, realOpenapi, syntheticTable } from './synthetic.ts';

vi.setConfig({ testTimeout: HOST_TEST_TIMEOUT_MS });

const envelope = realEnvelope();
const openapi = realOpenapi();
const table: ContractTable = withDigest(projectContract(openapi, envelope));
const methods = Object.keys(table.methods);
const statusByCode: Record<string, number> = envelope['x-hyprial-status-by-code'];
const statusDefault: number = envelope['x-hyprial-status-default'];
const TOKEN = 'conformance-upstream-token';
const GRANT = 'conformance-launch-grant-'.padEnd(64, 'g');
const PRINCIPAL = { owner: 'owner', node: 'node', actor: 'user:owner' } as const;
const GAP_KEYS: Record<string, string> = { success: 'success', empty: 'empty', error: 'domainErrors' };

type Obj = Record<string, any>;

function operationOf(method: string): Obj {
  const item = openapi.paths[`/daemon/request/${method}`];
  if (item) return item.post;
  return openapi['x-hyprial-methods'].find((entry: Obj) => entry.name === method);
}

function cleaned(message: string): string {
  return [...message.replace(/[\r\n]/g, ' ')].slice(0, 512).join('');
}

async function post(base: string, method: string, params: unknown, token = TOKEN): Promise<{ status: number; body: any }> {
  const response = await fetch(`${base}/daemon/request/${method}`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify(params),
  });
  return { status: response.status, body: await response.json() };
}

function holds(value: unknown, path: readonly string[], equals: unknown): boolean {
  if (path.length === 0) return deepEqual(value, equals);
  const [head, ...rest] = path;
  if (head === '*') return Array.isArray(value) && value.length > 0 && value.every((item) => holds(item, rest, equals));
  return typeof value === 'object' && value !== null && Object.hasOwn(value, head) && holds((value as Obj)[head], rest, equals);
}

function refusedByGuard(method: string, params: unknown): { code: string; message: string } | null {
  if (typeof params !== 'object' || params === null || Array.isArray(params)) return null;
  for (const guard of defaultPresets.inputGuards) {
    if (guard.method !== method) continue;
    if (!guard.requireOneOf.some((rule) => holds(params, rule.path, rule.equals))) return guard.refusal;
  }
  return null;
}

describe('contract to JavaScript: every declared case', () => {
  it.each(methods)('%s serves each declared case as declared', async (method) => {
    for (const example of table.methods[method].examples) {
      const server = await startContractMockServer({
        scenario: 'success',
        cases: { [method]: example.name },
        table,
        token: TOKEN,
        port: 0,
      });
      try {
        const reply = await post(server.endpoint, method, example.params);
        const label = `${method}/${example.name}`;
        const guard = refusedByGuard(method, example.params);
        if (guard !== null) {
          expect(reply.status, label).toBe(statusByCode[guard.code] ?? statusDefault);
          expect(reply.body.code, label).toBe(guard.code);
          expect(reply.body.message, label).toBe(guard.message);
          expect(reply.body.dispatch, label).toBe('not-dispatched');
          expect(schemaErrors(envelope.$defs.error, reply.body), label).toEqual([]);
        } else if (example.kind === 'invalid-params') {
          expect(reply.status, label).toBe(400);
          expect(reply.body.ok, label).toBe(false);
          expect(reply.body.dispatch, label).toBe('not-dispatched');
          expect(reply.body.code, label).toBe(example.error!.code);
          expect(reply.body.message, label).toBe(cleaned(example.error!.message));
          expect(schemaErrors(envelope.$defs.error, reply.body), label).toEqual([]);
        } else if (example.error !== undefined) {
          expect(reply.status, label).toBe(statusByCode[example.error.code] ?? statusDefault);
          expect(reply.body.code, label).toBe(example.error.code);
          expect(reply.body.message, label).toBe(cleaned(example.error.message));
          expect(reply.body.dispatch, label).toBe('dispatched');
          expect(reply.body.method, label).toBe(method);
          expect(reply.body.data, label).toEqual(example.error.data);
          expect(schemaErrors(envelope.$defs.error, reply.body), label).toEqual([]);
        } else {
          expect(reply.status, label).toBe(200);
          expect(reply.body.ok, label).toBe(true);
          expect(reply.body.method, label).toBe(method);
          expect(reply.body.dispatch, label).toBe('dispatched');
          expect(reply.body.result, label).toEqual(example.result);
          expect(schemaErrors(envelope.$defs.success, reply.body), label).toEqual([]);
        }
      } finally {
        await server.close();
      }
    }
  }, 60_000);
});

const invalidExamples = methods.flatMap((method) =>
  table.methods[method].examples.filter((example) => example.kind === 'invalid-params').map((example) => [method, example] as const),
);

describe('contract to JavaScript: a forced invalid-params case', () => {
  it('has examples to force', () => {
    expect(invalidExamples.length).toBeGreaterThan(100);
  });

  it('answers its declared error as 400 not-dispatched for schema-valid params too, never ok:true', async () => {
    const failures: string[] = [];
    for (const [method, example] of invalidExamples) {
      const entry = table.methods[method];
      const valid = entry.examples.find(
        (other) => other.kind !== 'invalid-params' && schemaErrors(entry.paramsSchema, other.params).length === 0,
      );
      const paramsSets: unknown[] = [{}];
      if (valid !== undefined) paramsSets.push(valid.params);
      const server = await startContractMockServer({ scenario: 'success', cases: { [method]: example.name }, table, token: TOKEN, port: 0 });
      try {
        for (const params of paramsSets) {
          const label = `${method}/${example.name} ${JSON.stringify(params).slice(0, 60)}`;
          const reply = await post(server.endpoint, method, params);
          const declared = refusedByGuard(method, params) ?? example.error!;
          const problems: string[] = [];
          if (reply.status !== 400) problems.push(`status ${reply.status}`);
          if (reply.body.ok !== false) problems.push('ok is not false');
          if ('result' in reply.body) problems.push('has result');
          if (reply.body.dispatch !== 'not-dispatched') problems.push('dispatched');
          if (reply.body.code !== declared.code) problems.push(`code ${reply.body.code}`);
          if (reply.body.message !== cleaned(declared.message)) problems.push(`message ${reply.body.message}`);
          if (schemaErrors(envelope.$defs.error, reply.body).length > 0) problems.push('not a $defs.error envelope');
          if (problems.length > 0) failures.push(`${label}: ${problems.join(', ')}`);
        }
      } finally {
        await server.close();
      }
    }
    expect(failures).toEqual([]);
  }, 120_000);
});

function schemaValidParams(entry: MethodEntry): unknown {
  const usable = entry.examples.find((example) => example.kind !== 'invalid-params');
  if (usable === undefined) throw new Error('no usable example');
  return usable.params;
}

const GAPPED_SCENARIOS = SCENARIOS.filter((scenario) =>
  Object.values(table.methods).some((entry) => casesFor(entry, scenario).length === 0),
);

describe('contract to JavaScript: a scenario without a case', () => {
  it('has a success case for every method', () => {
    expect(GAPPED_SCENARIOS).not.toContain('success');
  });

  it.each(GAPPED_SCENARIOS)('%s answers 409 with the recorded reason for every method that lacks one', async (scenario) => {
    const server = await startContractMockServer({ scenario, table, token: TOKEN, port: 0 });
    try {
      let checked = 0;
      for (const method of methods) {
        const entry = table.methods[method];
        if (casesFor(entry, scenario).length > 0) continue;
        const reply = await post(server.endpoint, method, schemaValidParams(entry));
        const available = SCENARIOS.filter((item) => casesFor(entry, item).length > 0).join(', ') || 'none';
        const gapKey = GAP_KEYS[scenario];
        const recorded = gapKey === undefined ? undefined : operationOf(method)['x-hyprial-example-gaps']?.[gapKey];
        const suffix = typeof recorded === 'string' && recorded !== '' ? ` Recorded reason: ${recorded}.` : '';
        expect(reply.status, method).toBe(409);
        expect(reply.body.code, method).toBe('MOCK_SCENARIO_MISSING');
        expect(reply.body.dispatch, method).toBe('not-dispatched');
        expect(reply.body.message, method).toBe(
          cleaned(`${method} has no ${scenario} example (available: ${available}).${suffix}`),
        );
        expect(schemaErrors(envelope.$defs.error, reply.body), method).toEqual([]);
        checked += 1;
      }
      expect(checked).toBeGreaterThan(0);
    } finally {
      await server.close();
    }
  }, 60_000);
});

interface Side {
  call(path: string, init?: { method?: string; body?: string; headers?: Record<string, string> }): Promise<Response>;
  headers(): Record<string, string>;
  session: Obj;
  close(): Promise<void>;
}

function startHost(upstream: string): Promise<{ origin: string; stop: () => Promise<void> }> {
  return startPythonHost({
    driver: 'contract-mock',
    endpoint: upstream,
    upstreamToken: TOKEN,
    launchGrant: GRANT,
    principal: PRINCIPAL,
  }).then((host) => ({ origin: host.origin, stop: host.close }));
}

async function nodeSide(selection: Selection, served: ContractTable = table): Promise<Side> {
  const upstream = await startContractMockServer({ ...selection, table: served, token: TOKEN, port: 0, presets: served === table ? undefined : EMPTY_PRESETS });
  const host = await startHost(upstream.endpoint);
  const origin = host.origin;
  let cookie = '';
  const call: Side['call'] = async (path, init = {}) => {
    const headers: Record<string, string> = { origin, ...init.headers };
    if (cookie !== '') headers.cookie = cookie;
    return fetch(`${origin}${path}`, { method: init.method ?? 'GET', headers, body: init.body });
  };
  const opened = await call('/daemon/session', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ grant: GRANT }),
  });
  cookie = opened.headers.getSetCookie().map((line) => line.split(';', 1)[0]).join('; ');
  const session = await opened.json();
  return {
    call,
    session,
    headers: () => ({ 'x-hyprial-view': session.view, 'x-hyprial-generation': session.generation, 'x-hyprial-csrf': session.csrf }),
    close: async () => {
      await host.stop();
      await upstream.close();
    },
  };
}

async function browserSide(selection: Selection, served: ContractTable = table): Promise<Side> {
  const answer = createContractMockFetch(createContractMock(served, selection, served === table ? undefined : { presets: EMPTY_PRESETS }), { principal: PRINCIPAL });
  const call: Side['call'] = (path, init = {}) =>
    answer(`${MOCK_ORIGIN}${path}`, { method: init.method ?? 'GET', headers: init.headers, body: init.body });
  const opened = await call('/daemon/session', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ grant: GRANT }),
  });
  const session = await opened.json();
  return {
    call,
    session,
    headers: () => ({ 'x-hyprial-view': session.view, 'x-hyprial-generation': session.generation, 'x-hyprial-csrf': session.csrf }),
    close: async () => {},
  };
}

function probesFor(method: string, scenario: Scenario): unknown[] {
  const entry = table.methods[method];
  const probes: unknown[] = [{}, ...entry.examples.map((example) => example.params)];
  const first = casesFor(entry, scenario).find((example) => typeof example.params === 'object' && !Array.isArray(example.params));
  const base = (first?.params ?? {}) as Obj;
  for (const selector of [...entry.selectors].sort()) probes.push({ ...base, [selector]: '__probe__' });
  for (const key of Object.keys(base).slice(0, 3)) {
    const { [key]: _removed, ...rest } = base;
    probes.push({ ...base, [key]: 7 }, rest);
  }
  probes.push({ ...base, __unknown__: 1 });
  return probes;
}

async function viaMethod(side: Side, method: string, body: string, extra: Record<string, string> = {}): Promise<Observed> {
  return observe(
    await side.call(`/daemon/request/${method}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...side.headers(), ...extra },
      body,
    }),
  );
}

async function compareSides(left: Side, right: Side, rows: Array<[string, string]>): Promise<string[]> {
  const found: string[] = [];
  for (const [method, body] of rows) {
    found.push(...differences(`${method} ${body.slice(0, 80)}`, await viaMethod(left, method, body), await viaMethod(right, method, body)));
  }
  return found;
}

function rowsFor(scenario: Scenario, only?: readonly string[]): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  for (const method of only ?? methods) for (const probe of probesFor(method, scenario)) rows.push([method, JSON.stringify(probe)]);
  rows.push(['no.such', '{}']);
  return rows;
}

function firstMethod(predicate: (entry: MethodEntry) => boolean): string {
  const found = methods.find((method) => predicate(table.methods[method]));
  if (found === undefined) throw new Error('the real contract has no method for this override probe');
  return found;
}

const overrideSets = (): Array<[string, Selection, string[]]> => {
  const mixed = firstMethod((entry) => casesFor(entry, 'success').length > 0 && casesFor(entry, 'empty').length > 0);
  const many = firstMethod((entry) => casesFor(entry, 'success').length > 1);
  const failing = firstMethod((entry) => casesFor(entry, 'success').length > 0 && casesFor(entry, 'error').length > 0);
  const second = casesFor(table.methods[many], 'success')[1].name;
  return [
    ['method scenario', { scenario: 'success', methodScenarios: { [mixed]: 'empty' } }, [mixed, many]],
    ['pinned case', { scenario: 'success', cases: { [many]: second } }, [many, mixed]],
    ['error beside success', { scenario: 'empty', methodScenarios: { [failing]: 'error' }, cases: { [many]: second } }, [failing, many, mixed]],
  ];
};

describe('the two forms agree', () => {
  const live: Side[] = [];
  afterAll(async () => {
    for (const side of live) await side.close();
  });

  async function pair(selection: Selection): Promise<[Side, Side]> {
    const left = await nodeSide(selection);
    const right = await browserSide(selection);
    live.push(left, right);
    return [left, right];
  }

  it.each(SCENARIOS)('answer every probe alike in the %s scenario', async (scenario) => {
    const [left, right] = await pair({ scenario });
    const found = await compareSides(left, right, rowsFor(scenario));
    expect(found.slice(0, 10)).toEqual([]);
  }, 180_000);

  it.each([0, 1, 2])('answer every probe alike under override set %i', async (index) => {
    const [, selection, touched] = overrideSets()[index];
    const [left, right] = await pair(selection);
    const found = await compareSides(left, right, rowsFor(selection.scenario ?? 'success', touched));
    expect(found.slice(0, 10)).toEqual([]);
  }, 180_000);

  it('give the same session, catalog, readiness and status documents', async () => {
    const [left, right] = await pair({ scenario: 'success' });
    expect(left.session.catalog.digest).toBe(table.digest);
    expect(differences('session', { status: 200, envelope: left.session, digest: null }, { status: 200, envelope: right.session, digest: null }, SESSION_VOLATILE)).toEqual([]);
    for (const path of ['/daemon/request', '/daemon/readyz']) {
      const a = await observe(await left.call(path, { headers: left.headers() }));
      const b = await observe(await right.call(path, { headers: right.headers() }));
      expect(differences(path, a, b), path).toEqual([]);
      expect(a.status).toBe(200);
    }
    const status = async (side: Side) =>
      observe(await side.call('/daemon/session/status', { method: 'POST', headers: { 'content-type': 'application/json', ...side.headers() }, body: '{}' }));
    expect(differences('status', await status(left), await status(right))).toEqual([]);
  }, 60_000);


  it('agree on a read-only method the GUI build does not declare: the session lists it false and a call needs no CSRF', async () => {
    const synthetic = syntheticTable();
    const left = await nodeSide({ scenario: 'success' }, synthetic);
    const right = await browserSide({ scenario: 'success' }, synthetic);
    live.push(left, right);
    const mutatesOf = (side: Side) =>
      Object.fromEntries(side.session.catalog.methods.map((method: Obj) => [method.name, method.mutates]));
    expect(mutatesOf(right)).toEqual({ 'demo.get': false, 'demo.list': false, 'demo.write': true });
    expect(mutatesOf(left)).toEqual(mutatesOf(right));
    expect(differences('session', { status: 200, envelope: left.session, digest: null }, { status: 200, envelope: right.session, digest: null }, SESSION_VOLATILE)).toEqual([]);

    const withoutCsrf = async (side: Side, method: string, body: string) => {
      const { 'x-hyprial-csrf': _dropped, ...headers } = side.headers();
      return observe(await side.call(`/daemon/request/${method}`, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body }));
    };
    const rows: Array<[string, string, number]> = [
      ['demo.list', '{"filter":null}', 200],
      ['demo.get', '{"id":"one"}', 200],
      ['demo.write', JSON.stringify({ actor: 'agent-a', setupToken: 'example-not-a-credential' }), 403],
    ];
    for (const [method, body, status] of rows) {
      const a = await withoutCsrf(left, method, body);
      const b = await withoutCsrf(right, method, body);
      expect(a.status, `node ${method}`).toBe(status);
      expect(b.status, `browser ${method}`).toBe(status);
      expect(differences(method, a, b), method).toEqual([]);
    }
  }, 60_000);


  it('refuse a broken session or frame alike', async () => {
    const [left, right] = await pair({ scenario: 'success' });
    const writer = firstMethod((entry) => entry.mutates && casesFor(entry, 'success').length > 0);
    const reader = firstMethod((entry) => !entry.mutates && casesFor(entry, 'success').length > 0);
    const params = JSON.stringify(schemaValidParams(table.methods[reader]));
    const rows: Array<[string, string, Record<string, string>]> = [
      [reader, params, { 'x-hyprial-generation': 'stale' }],
      [reader, params, { 'x-hyprial-view': 'unknown' }],
      [writer, JSON.stringify(schemaValidParams(table.methods[writer])), { 'x-hyprial-csrf': 'wrong' }],
      [reader, params, { 'x-hyprial-catalog-digest': 'sha256:other' }],
      [reader, '[]', {}],
      [reader, 'not json', {}],
      [reader, params, { 'content-type': 'text/plain' }],
    ];
    for (const [method, body, extra] of rows) {
      const found = differences(`${method} ${JSON.stringify(extra)}`, await viaMethod(left, method, body, extra), await viaMethod(right, method, body, extra));
      expect(found).toEqual([]);
    }
  }, 60_000);
});

describe('the comparator and the selection rule can fail', () => {
  const good: Observed = { status: 200, envelope: { ok: true, result: { a: 1 }, requestId: 'x' }, digest: 'sha256:d' };

  it('ignores only the request id', () => {
    expect(differences('same', good, { ...good, envelope: { ok: true, result: { a: 1 }, requestId: 'y' } })).toEqual([]);
  });

  it('reports an altered result', () => {
    expect(differences('result', good, { ...good, envelope: { ok: true, result: { a: 2 }, requestId: 'x' } })).toHaveLength(1);
  });

  it('reports an altered status', () => {
    expect(differences('status', good, { ...good, status: 409 })).toHaveLength(1);
  });

  it('reports a missing header', () => {
    expect(differences('header', good, { ...good, digest: null })).toHaveLength(1);
  });

  function agrees(example: ExampleCase, params: Obj): boolean {
    return Object.entries(example.params as Obj).every(([key, value]) => !Object.hasOwn(params, key) || deepEqual(params[key], value));
  }

  function oracle(entry: MethodEntry, scenario: Scenario, params: Obj): string | null {
    return casesFor(entry, scenario).find((example) => agrees(example, params))?.name ?? null;
  }

  function lastWins(entry: MethodEntry, scenario: Scenario, params: Obj): string | null {
    return [...casesFor(entry, scenario)].reverse().find((example) => agrees(example, params))?.name ?? null;
  }

  function replays(): Array<[MethodEntry, Scenario, Obj]> {
    const rows: Array<[MethodEntry, Scenario, Obj]> = [];
    for (const method of methods) {
      const entry = table.methods[method];
      for (const scenario of SCENARIOS) {
        for (const example of casesFor(entry, scenario)) {
          if (typeof example.params === 'object' && example.params !== null && !Array.isArray(example.params)) {
            rows.push([entry, scenario, example.params as Obj]);
          }
        }
      }
    }
    return rows;
  }

  it('selects like the plain statement of the rule on every replayed case', () => {
    const rows = replays();
    expect(rows.length).toBeGreaterThan(300);
    const off = rows.filter(([entry, scenario, params]) => {
      const exact = oracle(entry, scenario, params);
      const chosen = selectCase(entry, scenario, undefined, params)?.name ?? null;
      return exact !== null && exact !== chosen;
    });
    expect(off).toEqual([]);
  });

  it('is caught when the rule is last match wins', () => {
    const off = replays().filter(([entry, scenario, params]) => oracle(entry, scenario, params) !== lastWins(entry, scenario, params));
    expect(off.length).toBeGreaterThan(0);
  });
});

beforeAll(() => {
  expect(methods.length).toBeGreaterThan(100);
});
