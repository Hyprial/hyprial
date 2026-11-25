import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../../..', import.meta.url)));
const script = await import('../../../scripts/ci/check_gui_request_architecture.mjs');
const baseline = join(root, 'scripts/ci/gui-request-architecture-baseline.json');

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), 'gui-request-architecture-'));
  mkdirSync(join(directory, 'contract/daemon-request'), { recursive: true });
  writeFileSync(join(directory, 'contract/daemon-request/openapi.json'), JSON.stringify({
    'x-hyprial-methods': [
      { name: 'agent.list', mutates: false },
      { name: 'agent.create', mutates: true },
      { name: 'message.send.key_lookup', mutates: false },
    ],
  }));
  return directory;
}

function write(root, relative, text) {
  const path = join(root, relative);
  mkdirSync(join(path, '..'), { recursive: true });
  writeFileSync(path, text);
  return path;
}

function kinds(report) {
  return report.violations.map(row => row.kind);
}

test('registered generated calls and key lookup do not raise method findings', () => {
  const repo = fixture();
  write(repo, 'gui/src/features/demo.ts', `
export async function action(client) {
  await client.agent.list({});
  await client.message.send.keyLookup({ text: 'x', idempotencyKey: 'k' });
}
`);
  const report = script.scan(repo);
  assert.equal(kinds(report).includes('undeclared-method'), false);
  assert.equal(kinds(report).includes('dynamic-method'), false);
});

test('fanout loops unknown methods direct network and storage are findings', () => {
  const repo = fixture();
  write(repo, 'gui/src/features/demo.ts', `
export async function action(client, method, targets) {
  for (const name of targets) await client.agent.create({ name });
  await client.agent.list({});
  await transport.invoke(method, {});
  await fetch('/private-business');
  new WebSocket('ws://private-business');
  new XMLHttpRequest();
  new EventSource('/private-events');
  globalThis.localStorage.setItem('business', 'state');
}
`);
  const report = script.scan(repo);
  for (const kind of ['request-fanout', 'request-in-loop', 'dynamic-method', 'transport-bypass', 'direct-network', 'browser-storage']) {
    assert.ok(kinds(report).includes(kind), kind);
  }
  assert.equal(report.summary['direct-network'], 4);
});

test('unknown literal wrapper methods and undeclared server business modules are findings', () => {
  const repo = fixture();
  write(repo, 'gui/src/features/demo.ts', `
import { callAgentMethod } from './client';
export async function action(client) {
  return callAgentMethod('agent.destroyEverything', client?.agent?.destroyEverything, {});
}
`);
  write(repo, 'gui/server/setup/service.mjs', 'export const prepare = async () => true;\n');
  const report = script.scan(repo);
  assert.ok(kinds(report).includes('undeclared-method'));
  assert.ok(kinds(report).includes('server-business-module'));
});

test('generic transport and sibling methods are not false method or fanout findings', () => {
  const repo = fixture();
  write(repo, 'gui/src/client/transport.ts', `
export async function invoke(method, payload) {
  return transport.invoke(method, payload);
}
`);
  write(repo, 'gui/src/features/demo.ts', `
export function createApi(client) {
  return {
    list() { return client.agent.list({}); },
    create(name) { return client.agent.create({ name }); },
  };
}
`);
  const report = script.scan(repo);

  assert.equal(kinds(report).includes('dynamic-method'), false);
  assert.equal(kinds(report).includes('transport-bypass'), false);
  assert.equal(kinds(report).includes('request-fanout'), false);
});

test('server tests and declaration files are outside production source findings', () => {
  const repo = fixture();
  write(repo, 'gui/server/live/service.test.mjs', 'export const testOnly = true;\n');
  write(repo, 'gui/server/live/index.d.mts', 'export declare const value: string;\n');
  const report = script.scan(repo);

  assert.equal(kinds(report).includes('server-business-module'), false);
});

test('baseline growth is rejected and shrink requires a ledger update', () => {
  const repo = fixture();
  const source = write(repo, 'gui/src/features/demo.ts', 'client.agent.list({});\n');
  const path = join(repo, script.BASELINE_RELATIVE_PATH);
  assert.equal(script.writeBaseline(repo, path).status, 'PASS');
  assert.equal(script.check(repo, path).status, 'PASS');
  writeFileSync(source, 'client.agent.list({});\nclient.agent.create({});\n');
  const report = script.check(repo, path);
  assert.equal(report.status, 'FAIL');
  assert.ok(kinds(report).includes('request-fanout'));
  assert.ok(kinds(report).includes('new-architecture-finding'));
  assert.equal(script.writeBaseline(repo, path).status, 'FAIL');
  writeFileSync(source, 'client.agent.list({});\n');
  assert.equal(script.writeBaseline(repo, path).status, 'PASS');
  assert.equal(script.check(repo, path).status, 'PASS');
});

test('base comparison rejects growth even when the head ledger is edited', () => {
  const repo = fixture();
  const source = write(repo, 'gui/src/features/demo.ts', 'client.agent.list({});\n');
  const path = join(repo, script.BASELINE_RELATIVE_PATH);
  script.writeBaseline(repo, path);
  const git = (...args) => execFileSync('git', ['-C', repo, ...args], {
    env: {
      ...process.env,
      GIT_AUTHOR_NAME: 'gui-request-architecture',
      GIT_AUTHOR_EMAIL: 'gui-request-architecture@example.invalid',
      GIT_COMMITTER_NAME: 'gui-request-architecture',
      GIT_COMMITTER_EMAIL: 'gui-request-architecture@example.invalid',
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  git('init', '-q');
  git('add', '.');
  git('commit', '-qm', 'base');
  writeFileSync(source, 'client.agent.list({});\nglobalThis.localStorage.setItem("x", "y");\n');
  writeFileSync(path, JSON.stringify(script._baselineDocument(script.scan(repo).findings), null, 2) + '\n');
  const report = script.check(repo, path, { baseRef: 'HEAD' });
  assert.equal(report.status, 'FAIL');
  assert.ok(kinds(report).includes('base-architecture-growth'));
  assert.ok(kinds(report).includes('ledger-growth'));
});

test('the real repository has retired server business modules without growing the baseline', () => {
  const report = script.check(root, baseline, { baseRef: 'HEAD' });
  assert.equal(report.status, 'PASS', JSON.stringify(report.violations));
  assert.ok(report.summary['request-fanout'] > 0);
  assert.equal(report.summary['server-business-module'] ?? 0, 0);
  const entries = JSON.parse(readFileSync(baseline, 'utf8')).findings;
  assert.ok(entries.every(entry => !/^gui\/server\/(auth|setup|live)\//u.test(entry.path)));
});

test('a reviewed baseline can first-seed only on a protected base', () => {
  const repo = fixture();
  write(repo, 'gui/src/features/demo.ts', 'client.agent.list({});\n');
  const git = (...args) => execFileSync('git', ['-C', repo, ...args], {
    env: {
      ...process.env,
      GIT_AUTHOR_NAME: 'gui-request-architecture',
      GIT_AUTHOR_EMAIL: 'gui-request-architecture@example.invalid',
      GIT_COMMITTER_NAME: 'gui-request-architecture',
      GIT_COMMITTER_EMAIL: 'gui-request-architecture@example.invalid',
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  git('init', '-q');
  git('add', '.');
  git('commit', '-qm', 'base without architecture ledger');
  const path = join(repo, script.BASELINE_RELATIVE_PATH);
  script.writeBaseline(repo, path);
  const reviewedDigest = script._digest(path);

  const missing = script.check(repo, path, { baseRef: 'HEAD', baseBranch: 'dev', reviewedDigest });
  assert.equal(missing.status, 'FAIL');
  assert.ok(kinds(missing).includes('missing-base-ledger'));

  const seeded = script.check(repo, path, { baseRef: 'HEAD', baseBranch: 'dev', allowFirstSeed: true, reviewedDigest });
  assert.equal(seeded.status, 'PASS', JSON.stringify(seeded.violations));
  assert.equal(seeded.firstSeedUsed, true);

  const wrongBranch = script.check(repo, path, { baseRef: 'HEAD', baseBranch: 'feature', allowFirstSeed: true, reviewedDigest });
  assert.equal(wrongBranch.status, 'FAIL');
  assert.ok(kinds(wrongBranch).includes('missing-base-ledger'));

  const invalidBase = script.check(repo, path, { baseRef: 'does-not-exist', baseBranch: 'dev', allowFirstSeed: true, reviewedDigest });
  assert.equal(invalidBase.status, 'FAIL');
  assert.ok(kinds(invalidBase).includes('invalid-base-ref'));
});
