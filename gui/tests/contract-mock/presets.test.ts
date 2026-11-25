// @vitest-environment node
/**
 * Contract Mock presets: guard, transitions and reference resolution.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Ported from tests/daemon/contract_mock/safety/test_login_safety.py. The
 *   login guard is data (`presets.json` `inputGuards`); the previous mock also
 *   ran a whole-params credential-prefix scan before the sentinel check. That
 *   scan is dropped (decision D7): examples are still scanned in Python, and
 *   the guard now asks only for sentinel equality. The row that mixes the
 *   sentinel token with foreign file content documents the new behaviour: it
 *   passes the guard and is answered by a fixed example, which never echoes
 *   the caller's content.
 * - The interpreter is exercised with synthetic tables because the shipped
 *   presets carry no transition yet.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  ContractError,
  createContractMock,
  defaultPresets,
  projectContract,
  withDigest,
} from '../../contract-mock/index.ts';
import type { PresetDocument } from '../../contract-mock/index.ts';
import { SENTINEL, SENTINEL_B64, realEnvelope, realOpenapi, syntheticTable } from './synthetic.ts';

const real = withDigest(projectContract(realOpenapi(), realEnvelope()));

function login(params: Record<string, unknown>) {
  return createContractMock(real, { scenario: 'success' }).handle('agent.provider.login', {
    actor: 'example-agent',
    provider: 'claude',
    ...params,
  });
}

describe('login guard', () => {
  it.each([
    { setupToken: 'ordinary-secret' },
    { setupToken: null },
    { files: [{ filename: 'auth.json', contentBase64: 'e30=' }] },
    { files: [] },
    {},
  ])('refuses non-synthetic material without echoing it: %j', (credential) => {
    const reply = login(credential);
    expect(reply.status).toBe(400);
    expect(reply.payload.dispatch).toBe('not-dispatched');
    expect(reply.payload.message).toBe('mock login accepts only the documented synthetic sentinel');
    expect(JSON.stringify(reply.payload)).not.toContain('ordinary-secret');
  });

  it('refuses a file list when any element is not the sentinel', () => {
    const reply = login({
      files: [
        { filename: 'a.json', contentBase64: SENTINEL_B64 },
        { filename: 'b.json', contentBase64: 'e30=' },
      ],
    });
    expect(reply.status).toBe(400);
  });

  it.each(['setupToken', 'files'])('accepts the documented sentinel in the %s form', (form) => {
    const served = createContractMock(real, { scenario: 'success' });
    const published = { ...(served.selectedCase('agent.provider.login')!.params as Record<string, any>) };
    if (form === 'setupToken') {
      delete published.files;
      published.setupToken = SENTINEL;
    } else {
      published.files = published.files.map((item: object) => ({ ...item, contentBase64: SENTINEL_B64 }));
    }
    expect(served.handle('agent.provider.login', published).status).toBe(200);
  });

  it('lets the sentinel token through the guard even beside foreign file content', () => {
    const reply = login({ setupToken: SENTINEL, files: [{ filename: 'auth.json', contentBase64: 'e30=' }] });
    expect(JSON.stringify(reply.payload)).not.toContain('e30=');
    expect(reply.payload.code).not.toBe('MOCK_SCENARIO_MISSING');
  });

  it('is data: the engine source names no daemon method', () => {
    const directory = fileURLToPath(new URL('../../contract-mock/', import.meta.url));
    const methods = Object.keys(real.methods).concat(Object.keys(real.skipped));
    for (const name of readdirSync(directory).filter((file) => file.endsWith('.ts'))) {
      const text = readFileSync(`${directory}${name}`, 'utf8');
      for (const method of methods) {
        expect(text.includes(`'${method}'`) || text.includes(`"${method}"`) || text.includes(`\`${method}\``), `${name}: ${method}`).toBe(false);
      }
    }
  });

  it('ships presets that resolve against the real table', () => {
    for (const name of Object.keys(defaultPresets.presets)) {
      expect(() => createContractMock(real, { preset: name })).not.toThrow();
    }
  });
});

function synthetic(document: Partial<PresetDocument>): PresetDocument {
  return { version: 1, presets: { a: { scenario: 'success' }, b: { scenario: 'error' } }, transitions: [], inputGuards: [], ...document };
}

describe('transitions', () => {
  const table = syntheticTable();
  const rows = [{ preset: 'a', method: 'demo.write', next: 'b' }];

  it('switches the preset after a 200 reply to the named method only', () => {
    const served = createContractMock(table, { preset: 'a' }, { presets: synthetic({ transitions: rows }) });
    expect(served.handle('demo.list', { filter: null }).status).toBe(200);
    expect((served.describe() as any).preset).toBe('a');
    expect(served.handle('demo.write', { actor: '', setupToken: SENTINEL }).status).toBe(400);
    expect((served.describe() as any).preset).toBe('a');
    expect(served.handle('demo.write', { actor: 'agent-a', setupToken: SENTINEL }).status).toBe(200);
    expect((served.describe() as any).preset).toBe('b');
    expect(served.handle('demo.list', { filter: null }).payload.code).toBe('DAEMON_NOT_READY');
  });

  it('does not switch when the reply is an error case', () => {
    const served = createContractMock(table, { preset: 'a', cases: { 'demo.write': 'foreign-target' } }, { presets: synthetic({ transitions: rows }) });
    expect(served.handle('demo.write', { actor: 'agent-b', setupToken: SENTINEL }).status).toBe(403);
    expect((served.describe() as any).preset).toBe('a');
  });

  it('keeps explicit overrides on top of the new preset', () => {
    const served = createContractMock(table, { preset: 'a', methodScenarios: { 'demo.list': 'empty' } }, { presets: synthetic({ transitions: rows }) });
    served.handle('demo.write', { actor: 'agent-a', setupToken: SENTINEL });
    expect(served.scenarioFor('demo.list')).toBe('empty');
    expect(served.scenarioFor('demo.get')).toBe('error');
  });

  it('does nothing without a preset', () => {
    const served = createContractMock(table, { scenario: 'success' }, { presets: synthetic({ transitions: rows }) });
    expect(served.handle('demo.write', { actor: 'agent-a', setupToken: SENTINEL }).status).toBe(200);
    expect((served.describe() as any).preset).toBeNull();
  });
});

describe('guards', () => {
  const table = syntheticTable();
  const guard = {
    method: 'demo.write',
    requireOneOf: [
      { path: ['setupToken'], equals: SENTINEL },
      { path: ['list', '*', 'tag'], equals: 'x' },
    ],
    refusal: { code: 'INVALID_ARGUMENT', message: 'refused' },
  };

  it('requires one alternative to hold and applies only to its method', () => {
    const served = createContractMock(table, { scenario: 'success' }, { presets: synthetic({ inputGuards: [guard] }) });
    expect(served.handle('demo.write', { actor: 'a', setupToken: 'other' }).payload.message).toBe('refused');
    expect(served.handle('demo.write', { actor: 'agent-a', setupToken: SENTINEL }).status).toBe(200);
    expect(served.handle('demo.list', { filter: null }).status).toBe(200);
  });

  it('treats a wildcard as a non-empty array of matching elements', () => {
    const served = createContractMock(table, { scenario: 'success' }, { presets: synthetic({ inputGuards: [{ ...guard, requireOneOf: [guard.requireOneOf[1]] }] }) });
    const call = (list: unknown) => served.handle('demo.write', { actor: 'a', setupToken: SENTINEL, list } as any).payload.message === 'refused';
    expect(call([])).toBe(true);
    expect(call('x')).toBe(true);
    expect(call([{ tag: 'x' }, { tag: 'y' }])).toBe(true);
    expect(call([{ tag: 'x' }, { tag: 'x' }])).toBe(false);
  });
});

describe('reference resolution', () => {
  const table = syntheticTable();
  const build = (document: PresetDocument) => () => createContractMock(table, { preset: 'a' }, { presets: document });

  it.each([
    ['an unknown method in a preset', synthetic({ presets: { a: { scenario: 'success', methodScenarios: { 'no.such': 'success' } } } }), /unknown method/],
    ['an unknown case in a preset', synthetic({ presets: { a: { scenario: 'success', cases: { 'demo.list': 'nope' } } } }), /no example case named/],
    ['a scenario a method cannot serve', synthetic({ presets: { a: { scenario: 'success', methodScenarios: { 'demo.write': 'empty' } } } }), /no empty example/],
    ['an unknown preset in a transition', synthetic({ transitions: [{ preset: 'a', method: 'demo.list', next: 'zzz' }] }), /unknown preset/],
    ['an unknown method in a transition', synthetic({ transitions: [{ preset: 'a', method: 'no.such', next: 'b' }] }), /unknown method/],
    ['an unknown method in a guard', synthetic({ inputGuards: [{ method: 'no.such', requireOneOf: [{ path: ['x'], equals: 1 }], refusal: { code: 'C', message: 'm' } }] }), /unknown method/],
    ['a payload key in a preset', synthetic({ presets: { a: { scenario: 'success', result: {} } as never } }), /unexpected key "result"/],
    ['a payload key in a guard refusal', synthetic({ inputGuards: [{ method: 'demo.list', requireOneOf: [{ path: ['x'], equals: 1 }], refusal: { code: 'C', message: 'm', error: {} } as never }] }), /unexpected key "error"/],
    ['a top-level payload key', { ...synthetic({}), examples: [] } as never, /unexpected key "examples"/],
    ['an empty alternative list', synthetic({ inputGuards: [{ method: 'demo.list', requireOneOf: [], refusal: { code: 'C', message: 'm' } }] }), /non-empty list/],
  ])('rejects %s', (_name, document, pattern) => {
    expect(build(document)).toThrow(ContractError);
    expect(build(document)).toThrow(pattern);
  });
});
