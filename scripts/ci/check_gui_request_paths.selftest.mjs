/**
 * Decision record (2026-11-14, GUI launch-path convergence, PR 1e):
 * - Each rule of the request-path gate is proven by planting one violation in
 *   a synthetic tree and seeing the gate fail; a clean tree and the transport
 *   module itself must pass.
 * - The ledger cannot grow in the pull request that adds a violation: every
 *   growth case (new finding with a new pending row, new allowlist row, larger
 *   count) is planted against a git base that already has a ledger and must
 *   fail; so must an empty or unresolvable base ref and an unreadable base.
 * - The scanner configuration is pinned against the base blob: removing or
 *   changing a pattern, root or extension, adding a skip, changing the ledger
 *   path fails; additions and removed skips pass; any other change passes only
 *   with the matching reviewed digest. The first-seed digest is never
 *   consulted once the base carries a ledger.
 * - The widened scanner is proven attack by attack in a synthetic tree: bracket,
 *   destructured and Reflect access to request globals, HTTP client libraries,
 *   banned prefixes mid-string, dist/node_modules inside gui/src, inline html
 *   scripts, node:http/net in every import form, server libraries, vite
 *   createServer with plugins, middlewares.use through an alias and bracket
 *   fulfill; benign look-alikes must still pass.
 * - Synthetic trees run without a reviewed scanner digest unless a test passes
 *   one, so the pin of the real configuration cannot clear a synthetic violation.
 * - The real repository is checked last: the committed ledger must describe it
 *   exactly and every allowlist row must carry a reason.
 */
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  CONFIG_RELATIVE_PATH,
  FIRST_SEED_LEDGER_SHA256,
  LEDGER_RELATIVE_PATH,
  REVIEWED_SCANNER_SHA256,
  _ledgerDocument,
  check as checkWithPin,
  compareScannerConfigs,
  parseLedger,
  scan,
  scannerDigest,
} from './check_gui_request_paths.mjs';

const check = (root, options = {}) => checkWithPin(root, { reviewedScannerSha256: null, ...options });

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const checker = join(repoRoot, 'scripts/ci/check_gui_request_paths.mjs');
const REASON = 'documented upstream requirement';
const CONFIG_TEXT = readFileSync(join(repoRoot, CONFIG_RELATIVE_PATH), 'utf8');
const BASE_CONFIG = JSON.parse(CONFIG_TEXT);

function configText(change = {}) {
  return `${JSON.stringify({ ...BASE_CONFIG, ...change }, null, 2)}\n`;
}

function ledgerText(pending = [], allowlist = []) {
  return `${JSON.stringify(_ledgerDocument(allowlist, pending), null, 2)}\n`;
}

function tree(files, pending = [], allowlist = []) {
  const root = mkdtempSync(join(tmpdir(), 'gui-request-paths-'));
  const all = { [LEDGER_RELATIVE_PATH]: ledgerText(pending, allowlist), [CONFIG_RELATIVE_PATH]: CONFIG_TEXT, ...files };
  for (const [name, text] of Object.entries(all)) {
    mkdirSync(dirname(join(root, name)), { recursive: true });
    writeFileSync(join(root, name), text);
  }
  return root;
}

function kinds(root, options = {}) {
  try {
    return check(root, options).violations.map(row => `${row.kind}:${row.finding ?? ''}:${row.path ?? ''}:${row.subject ?? ''}`);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function gitIn(root, ...args) {
  return execFileSync('git', ['-C', root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', ...args], { stdio: ['ignore', 'pipe', 'pipe'] });
}

function withBase({ files = {}, pending = [], allowlist = [], baseLedger, baseConfig }, run) {
  const root = tree(files, pending, allowlist);
  try {
    gitIn(root, 'init', '-q');
    if (baseLedger === null) rmSync(join(root, LEDGER_RELATIVE_PATH));
    else if (baseLedger !== undefined) writeFileSync(join(root, LEDGER_RELATIVE_PATH), baseLedger);
    if (baseConfig === null) rmSync(join(root, CONFIG_RELATIVE_PATH));
    else if (baseConfig !== undefined) writeFileSync(join(root, CONFIG_RELATIVE_PATH), baseConfig);
    gitIn(root, 'add', '-A');
    gitIn(root, 'commit', '-q', '--allow-empty', '-m', 'base');
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText(pending, allowlist));
    writeFileSync(join(root, CONFIG_RELATIVE_PATH), CONFIG_TEXT);
    return run(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function violationKinds(result) {
  return result.violations.map(item => item.kind);
}

test('the transport module and ordinary feature code pass', () => {
  assert.deepEqual(kinds(tree({
    'gui/src/client/transport.ts': 'export const send = () => fetch(`${base}/daemon/request/${method}`);\n',
    'gui/src/app/backend/browser-session.ts': "const path = '/daemon/session';\nexport const t = new HttpTransport({ fetch: globalThis.fetch });\n",
    'gui/src/features/org/impl/api.ts': "export const run = (client) => client.org.fetch({ org: 'a' });\nexport const hint = 'https://example.invalid/v1';\n",
    'gui/contract-mock/serve.ts': 'export const plugin = { configureServer(server) { server.middlewares.use(handler); } };\n',
  })), []);
});

const planted = [
  ['a direct fetch call', 'gui/src/features/x/impl/api.ts', "export const load = () => fetch('data.json');\n", 'new-request-path-finding:network-primitive:gui/src/features/x/impl/api.ts:fetch'],
  ['an injectable fetch parameter', 'gui/src/features/x/impl/api.ts', 'export function make(fetcher: typeof fetch) { return fetcher; }\n', 'new-request-path-finding:network-primitive:gui/src/features/x/impl/api.ts:fetch'],
  ['a global fetch reference in dev code', 'gui/src/dev/mock/x.ts', 'export const f = globalThis.fetch;\n', 'new-request-path-finding:network-primitive:gui/src/dev/mock/x.ts:globalThis.fetch'],
  ['an XMLHttpRequest', 'gui/src/pages/x.tsx', 'export const x = new XMLHttpRequest();\n', 'new-request-path-finding:network-primitive:gui/src/pages/x.tsx:XMLHttpRequest'],
  ['an EventSource', 'gui/src/pages/x.tsx', "export const x = new EventSource(name);\n", 'new-request-path-finding:network-primitive:gui/src/pages/x.tsx:EventSource'],
  ['a WebSocket', 'gui/src/pages/x.tsx', 'export const x = new WebSocket(name);\n', 'new-request-path-finding:network-primitive:gui/src/pages/x.tsx:WebSocket'],
  ['a beacon', 'gui/src/pages/x.tsx', 'navigator.sendBeacon(name);\n', 'new-request-path-finding:network-primitive:gui/src/pages/x.tsx:navigator.sendBeacon'],
  ['a second transport', 'gui/src/features/x/impl/api.ts', 'export const t = new HttpTransport(config);\n', 'new-request-path-finding:transport-construction:gui/src/features/x/impl/api.ts:HttpTransport'],
  ['a request path literal', 'gui/src/adapters/x.ts', "export const PATH = '/api/gui/v1/not-wired';\n", 'new-request-path-finding:url-literal:gui/src/adapters/x.ts:/api/gui/v1/not-wired'],
  ['a request path template', 'gui/src/dev/mock/x.ts', 'export const path = (name: string) => `/api/reference/mock-${name}`;\n', 'new-request-path-finding:url-literal:gui/src/dev/mock/x.ts:/api/reference/mock-'],
  ['a daemon path outside the transport', 'gui/src/features/x/impl/api.ts', "export const PATH = '/daemon/request/agent.list';\n", 'new-request-path-finding:url-literal:gui/src/features/x/impl/api.ts:/daemon/request/agent.list'],
  ['an absolute URL', 'gui/src/features/x/impl/api.ts', "export const URL_ = 'http://127.0.0.1:8770';\n", 'new-request-path-finding:url-literal:gui/src/features/x/impl/api.ts:http://127.0.0.1:8770'],
  ['a test-side Vite route plugin', 'gui/tests/support/x.ts', 'export const plugin = { name: "x", configureServer(server) { server.middlewares.use(handler); } };\n', 'new-request-path-finding:host-route-handler:gui/tests/support/x.ts:configureServer'],
  ['a second server directory', 'gui/server/reference/index.ts', 'export const plugin = { configurePreviewServer: install };\n', 'new-request-path-finding:host-route-handler:gui/server/reference/index.ts:configurePreviewServer'],
  ['a test-side HTTP server', 'gui/tests/support/x.mjs', "import { createServer } from 'node:http';\nexport const server = createServer(handler);\n", 'new-request-path-finding:host-http-server:gui/tests/support/x.mjs:createServer@<module>'],
  ['a canned browser response', 'gui/tests/x-browser.mjs', "await page.route('**/daemon/request/org.list', route => route.fulfill({ status: 200, body: '{}' }));\n", 'new-request-path-finding:browser-route-fulfill:gui/tests/x-browser.mjs:fulfill'],
  ['a private host path in a test entry', 'gui/tests/reference/x.tsx', "export const base = '/api/reference/mock-success';\n", 'new-request-path-finding:host-path-literal:gui/tests/reference/x.tsx:/api/reference/mock-success'],
];

for (const [label, path, text, expected] of planted) {
  test(`planted violation fails: ${label}`, () => {
    assert.ok(kinds(tree({ [path]: text })).includes(expected));
  });
}

test('a pending row covers exactly its finding and goes stale when the finding leaves', () => {
  const row = { kind: 'url-literal', path: 'gui/src/dev/mock/x.ts', subject: '/api/reference/mock-', count: 1 };
  const source = { 'gui/src/dev/mock/x.ts': 'export const path = (name: string) => `/api/reference/mock-${name}`;\n' };
  assert.deepEqual(kinds(tree(source, [row])), []);
  assert.deepEqual(kinds(tree({}, [row])), ['stale-pending-entry:url-literal:gui/src/dev/mock/x.ts:/api/reference/mock-']);
  assert.deepEqual(kinds(tree({ 'gui/src/dev/mock/x.ts': `${source['gui/src/dev/mock/x.ts']}export const again = '/api/reference/mock-';\n` }, [row])),
    ['request-path-finding-grew:url-literal:gui/src/dev/mock/x.ts:/api/reference/mock-']);
});

test('an allowlist row needs a reason and must still match', () => {
  const row = { kind: 'url-literal', path: 'gui/src/x.ts', subject: '/api/x', count: 1 };
  const source = { 'gui/src/x.ts': "export const PATH = '/api/x';\n" };
  assert.deepEqual(kinds(tree(source, [], [{ ...row, reason: REASON }])), []);
  assert.deepEqual(kinds(tree(source, [], [{ ...row, reason: '' }])), ['invalid-ledger:::']);
  assert.deepEqual(kinds(tree({}, [], [{ ...row, reason: REASON }])), ['stale-allowlist-entry:url-literal:gui/src/x.ts:/api/x']);
  assert.deepEqual(kinds(tree(source, [row], [{ ...row, reason: REASON }])), ['allowlisted-and-pending:url-literal:gui/src/x.ts:/api/x']);
});

test('a bare scheme in a placeholder is not an absolute URL', () => {
  assert.deepEqual(kinds(tree({ 'gui/src/features/x/Form.tsx': 'export const el = <input placeholder="https://" />;\n' })), []);
});

test('ledger rows with unknown keys, kinds or shapes are rejected', () => {
  const row = { kind: 'url-literal', path: 'gui/src/x.ts', subject: '/api/x', count: 1 };
  const document = (change) => `${JSON.stringify({ ..._ledgerDocument([], [row]), ...change }, null, 2)}\n`;
  for (const [label, text] of [
    ['extra row key', ledgerText([{ ...row, note: 'x' }])],
    ['unknown kind', ledgerText([{ ...row, kind: 'whatever' }])],
    ['zero count', ledgerText([{ ...row, count: 0 }])],
    ['duplicate row', ledgerText([row, row])],
    ['unknown top-level key', document({ extra: true })],
    ['allowlist row without reason key', ledgerText([], [row])],
    ['not json', 'nope'],
  ]) {
    const root = tree({});
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), text);
    assert.deepEqual(kinds(root), ['invalid-ledger:::'], label);
  }
  assert.throws(() => parseLedger({ version: 2 }, 'x'));
});

const ROW_X = { kind: 'url-literal', path: 'gui/src/x.ts', subject: '/api/x', count: 1 };
const ROW_Y = { kind: 'url-literal', path: 'gui/src/y.ts', subject: '/api/y', count: 1 };
const SOURCE_X = "export const PATH = '/api/x';\n";
const SOURCE_Y = "export const PATH = '/api/y';\n";

test('against a base that has the ledger, a new violation with a new pending row fails', () => {
  withBase({ files: { 'gui/src/x.ts': SOURCE_X }, pending: [ROW_X] }, (root) => {
    assert.equal(check(root, { baseRef: 'HEAD' }).status, 'PASS');
    writeFileSync(join(root, 'gui/src/y.ts'), SOURCE_Y);
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText([ROW_X, ROW_Y]));
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['pending-ledger-growth']);
  });
});

test('against a base that has the ledger, a larger pending count fails and a smaller one passes', () => {
  withBase({ files: { 'gui/src/x.ts': `${SOURCE_X}export const again = '/api/x';\n` }, pending: [{ ...ROW_X, count: 2 }] }, (root) => {
    writeFileSync(join(root, 'gui/src/x.ts'), `${SOURCE_X}export const again = '/api/x';\nexport const third = '/api/x';\n`);
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText([{ ...ROW_X, count: 3 }]));
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['pending-ledger-growth']);
    writeFileSync(join(root, 'gui/src/x.ts'), SOURCE_X);
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText([ROW_X]));
    assert.equal(check(root, { baseRef: 'HEAD' }).status, 'PASS');
  });
});

test('against a base that has the ledger, an allowlist row added in the same change fails', () => {
  withBase({ files: { 'gui/src/x.ts': SOURCE_X }, pending: [ROW_X] }, (root) => {
    writeFileSync(join(root, 'gui/src/y.ts'), SOURCE_Y);
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText([ROW_X], [{ ...ROW_Y, reason: REASON }]));
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['allowlist-ledger-growth']);
  });
});

test('against a base that has the ledger, moving a pending row into the allowlist fails', () => {
  withBase({ files: { 'gui/src/x.ts': SOURCE_X }, pending: [ROW_X] }, (root) => {
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText([], [{ ...ROW_X, reason: REASON }]));
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['allowlist-ledger-growth']);
  });
});

test('against a base that has the ledger, an existing allowlist row may stay or shrink', () => {
  const row = { ...ROW_X, reason: REASON };
  withBase({ files: { 'gui/src/x.ts': SOURCE_X }, allowlist: [row] }, (root) => {
    assert.equal(check(root, { baseRef: 'HEAD' }).status, 'PASS');
    rmSync(join(root, 'gui/src/x.ts'));
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText());
    assert.equal(check(root, { baseRef: 'HEAD' }).status, 'PASS');
  });
});

test('an empty, missing or unresolvable base ref fails closed', () => {
  withBase({}, (root) => {
    assert.deepEqual(violationKinds(check(root, { baseRef: '' })), ['invalid-base-ref']);
    assert.deepEqual(violationKinds(check(root, { baseRef: 'refs/heads/does-not-exist' })), ['invalid-base-ref']);
    assert.deepEqual(violationKinds(check(root, { baseRef: undefined })), []);
    for (const args of [['--base-ref', ''], ['--base-ref'], ['--base-ref', 'refs/heads/does-not-exist']]) {
      const run = spawnSync(process.execPath, [checker, '--root', root, ...args], { encoding: 'utf8' });
      assert.equal(run.status, 1, args.join(' '));
      assert.match(run.stderr, /invalid-base-ref/);
    }
  });
});

test('an unreadable base ledger fails instead of passing as a first seed', () => {
  withBase({ baseLedger: 'not json' }, (root) => {
    const result = check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev' });
    assert.deepEqual(violationKinds(result), ['invalid-base-ledger']);
  });
});

test('the first seed needs --allow-first-seed, a dev/main base and the reviewed digest', () => {
  withBase({ baseLedger: null, pending: [], allowlist: [] }, (root) => {
    const digest = createHash('sha256').update(readFileSync(join(root, LEDGER_RELATIVE_PATH))).digest('hex');
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['missing-base-ledger']);
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'feature', firstSeedDigest: digest })), ['missing-base-ledger']);
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: '0'.repeat(64) })), ['seed-digest-mismatch']);
    assert.equal(check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: digest }).status, 'PASS');
    assert.equal(check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'main', firstSeedDigest: digest }).status, 'PASS');
  });
});

function scanned(files, config) {
  const root = tree(files);
  try {
    return scan(root, config).map(row => `${row.kind}|${row.path}|${row.subject}`);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

const SRC = 'gui/src/features/x/impl/api.ts';
const HOST = 'gui/tests/support/x.mjs';

const widenedSource = [
  ['bracket access to fetch', "export const f = globalThis['fetch'];\n", 'network-primitive|globalThis.fetch'],
  ['bracket access on window', "export const f = window['fetch'];\n", 'network-primitive|window.fetch'],
  ['destructured fetch', 'export const { fetch } = globalThis;\n', 'network-primitive|fetch'],
  ['destructured and renamed fetch', 'export const { fetch: f } = window;\n', 'network-primitive|window.fetch'],
  ['destructuring assignment of fetch', 'let f;\n({ fetch: f } = window);\n', 'network-primitive|window.fetch'],
  ['Reflect.get of fetch', "export const f = Reflect.get(globalThis, 'fetch');\n", 'network-primitive|globalThis.fetch'],
  ['a beacon through window.navigator', 'window.navigator.sendBeacon(name);\n', 'network-primitive|window.navigator.sendBeacon'],
  ['fetch through globalThis.window', 'export const f = globalThis.window.fetch;\n', 'network-primitive|globalThis.window.fetch'],
  ['fetch through self', 'export const f = self.fetch;\n', 'network-primitive|self.fetch'],
  ['fetch behind a type assertion', 'export const f = (globalThis as any).fetch;\n', 'network-primitive|globalThis.fetch'],
  ['a legacy prefix in the middle of a string', "export const p = 'x/api/gui/v1/y';\n", 'url-literal|x/api/gui/v1/y'],
  ['a legacy prefix after a substitution', 'export const p = (base: string) => `${base}/api/reference/mock`;\n', 'url-literal|/api/reference/mock'],
  ['a legacy auth prefix in a later template span', 'export const p = (a: string, b: string) => `${a}/x${b}/__gui/auth/login`;\n', 'url-literal|/__gui/auth/login'],
  ['dynamic import of a client library', "export const load = () => import('axios');\n", 'http-client-import|axios'],
  ['require of a client library', "const got = require('got');\n", 'http-client-import|got'],
  ['re-export of a client library', "export * from 'ofetch';\n", 'http-client-import|ofetch'],
  ['a client library subpath', "import x from 'undici/lib/x';\n", 'http-client-import|undici'],
];

for (const [label, text, expected] of widenedSource) {
  test(`widened source scan catches ${label}`, () => {
    assert.ok(scanned({ [SRC]: text }).includes(`${expected.split('|')[0]}|${SRC}|${expected.split('|').slice(1).join('|')}`), label);
  });
}

for (const library of ['axios', 'ky', 'got', 'undici', 'node-fetch', 'superagent', 'cross-fetch', 'ofetch']) {
  test(`widened source scan catches an import of ${library}`, () => {
    assert.ok(scanned({ [SRC]: `import client from '${library}';\nexport default client;\n` }).includes(`http-client-import|${SRC}|${library}`));
  });
  test(`widened scan catches ${library} in gui/package.json`, () => {
    const found = scanned({ 'gui/package.json': JSON.stringify({ devDependencies: { [library]: '1.0.0' } }) });
    assert.ok(found.includes(`http-client-dependency|gui/package.json|${library}`));
  });
}

test('a legacy prefix is a finding inside the transport module too', () => {
  assert.ok(scanned({ 'gui/src/client/transport.ts': "export const p = 'x/api/gui/v1/y';\n" }).includes('url-literal|gui/src/client/transport.ts|x/api/gui/v1/y'));
});

test('dist and node_modules directories inside gui/src are scanned', () => {
  const found = scanned({
    'gui/src/dist/x.ts': 'export const f = fetch;\n',
    'gui/src/features/node_modules/y.ts': 'export const f = fetch;\n',
    'gui/src/dist-parity/z.ts': 'export const f = fetch;\n',
  });
  assert.deepEqual(found.filter(row => row.startsWith('network-primitive')).sort(), [
    'network-primitive|gui/src/dist-parity/z.ts|fetch',
    'network-primitive|gui/src/dist/x.ts|fetch',
    'network-primitive|gui/src/features/node_modules/y.ts|fetch',
  ]);
});

test('inline html scripts are scanned and external or data scripts are not', () => {
  const found = scanned({
    'gui/index.html': '<!doctype html><script type="module">fetch("/x");</script><script src="/main.js"></script><script type="application/json">{"a":"fetch"}</script>\n',
    'gui/src/dev/preview.html': '<script>navigator.sendBeacon(1);</script><img src="/api/gui/v1/pixel">\n',
    'gui/public/page.html': '<script>new WebSocket(url);</script>\n',
  });
  assert.deepEqual(found.sort(), [
    'network-primitive|gui/index.html|fetch',
    'network-primitive|gui/public/page.html|WebSocket',
    'network-primitive|gui/src/dev/preview.html|navigator.sendBeacon',
    'url-literal|gui/src/dev/preview.html|/api/gui/v1/',
  ]);
});

test('benign look-alikes of the widened source rules pass', () => {
  assert.deepEqual(scanned({
    [SRC]: [
      "export const a = client.fetch({ id: 1 });",
      "export const b = options['fetch'];",
      "export const { fetch: f } = options;",
      "export const c = Reflect.get(options, 'fetch');",
      "export const d = window.navigator.userAgent;",
      "export const e = 'api/gui/v1';",
      "import ky from './ky';",
      '',
    ].join('\n'),
    'gui/src/dev/page.html': '<script src="/x.js"></script><p>fetch</p>\n',
  }), []);
});

const widenedHost = [
  ['a default node:http import', "import http from 'node:http';\nexport const s = http.createServer(h);\n", 'host-http-server|createServer@<module>'],
  ['a namespace http import', "import * as http from 'http';\nexport const s = http.createServer(h);\n", 'host-http-server|createServer@<module>'],
  ['a default https import', "import https from 'node:https';\nexport const s = https.createServer(o, h);\n", 'host-http-server|createServer@<module>'],
  ['a default net import', "import net from 'node:net';\nexport const s = net.createServer(h);\n", 'host-http-server|createServer@<module>'],
  ['a named net import', "import { createServer } from 'node:net';\nexport const s = createServer(h);\n", 'host-http-server|createServer@<module>'],
  ['an http2 secure server', "import { createSecureServer } from 'node:http2';\nexport const s = createSecureServer(o);\n", 'host-http-server|createSecureServer@<module>'],
  ['a server class', "import { Server } from 'node:net';\nexport const s = new Server(h);\n", 'host-http-server|Server@<module>'],
  ['an aliased factory', "import { createServer as make } from 'node:http';\nexport const s = make(h);\n", 'host-http-server|createServer@<module>'],
  ['a factory kept in a variable', "import { createServer } from 'node:http';\nconst make = createServer;\nexport const s = make(h);\n", 'host-http-server|createServer@<module>'],
  ['a dynamic import bound to a name', "export async function start() { const http = await import('node:http'); return http.createServer(h); }\n", 'host-http-server|createServer@start'],
  ['a dynamic import used inline', "export async function start() { return (await import('node:http')).createServer(h); }\n", 'host-http-server|createServer@start'],
  ['a destructured dynamic import', "export async function start() { const { createServer } = await import('node:http'); return createServer(h); }\n", 'host-http-server|createServer@start'],
  ['a required module', "export const s = require('node:http').createServer(h);\n", 'host-http-server|createServer@<module>'],
  ['bracket access to the factory', "import http from 'node:http';\nexport const s = http['createServer'](h);\n", 'host-http-server|createServer@<module>'],
  ['vite createServer with plugins', "import { createServer } from 'vite';\nexport const s = createServer({ plugins: [p] });\n", 'host-http-server|vite.createServer@<module>'],
  ['vite namespace createServer with a configureServer plugin', "import * as vite from 'vite';\nexport const s = vite.createServer({ plugins: [{ configureServer() {} }] });\n", 'host-http-server|vite.createServer@<module>'],
  ['vite createServer with an opaque config', "import { createServer } from 'vite';\nexport const s = (config) => createServer(config);\n", 'host-http-server|vite.createServer@s'],
  ['a dynamically imported vite createServer', "export async function start() { const vite = await import('vite'); return vite.createServer({ plugins }); }\n", 'host-http-server|vite.createServer@start'],
  ['middlewares.use through a variable', "export const add = (server) => { const stack = server.middlewares; stack.use(h); };\n", 'host-route-handler|middlewares.use'],
  ['middlewares.use through destructuring', "export const add = (server) => { const { middlewares } = server; middlewares.use(h); };\n", 'host-route-handler|middlewares.use'],
  ['middlewares.use through a renamed destructuring', "export const add = (server) => { const { middlewares: stack } = server; stack.use(h); };\n", 'host-route-handler|middlewares.use'],
  ['middlewares.use by bracket access', "export const add = (server) => server['middlewares']['use'](h);\n", 'host-route-handler|middlewares.use'],
  ['a detached middlewares.use', "export const add = (server) => { const use = server.middlewares.use; use(h); };\n", 'host-route-handler|middlewares.use'],
  ['fulfill by bracket access', "export const f = (route) => route['fulfill']({ status: 200 });\n", 'browser-route-fulfill|fulfill'],
  ['a destructured fulfill', 'export const f = (route) => { const { fulfill } = route; return fulfill({}); };\n', 'browser-route-fulfill|fulfill'],
  ['a legacy prefix mid-string in the test host', 'export const u = (origin) => `${origin}/api/reference/live`;\n', 'host-path-literal|/api/reference/live'],
  ['a configureServer hook with a string key', "export const plugin = { 'configureServer': install };\n", 'host-route-handler|configureServer'],
];

for (const [label, text, expected] of widenedHost) {
  test(`widened host scan catches ${label}`, () => {
    const [kind, ...subject] = expected.split('|');
    assert.ok(scanned({ [HOST]: text }).includes(`${kind}|${HOST}|${subject.join('|')}`), label);
  });
}

for (const library of ['express', 'connect', 'koa', 'fastify', 'polka', 'sirv']) {
  test(`widened host scan catches a ${library} server import`, () => {
    assert.ok(scanned({ [HOST]: `import app from '${library}';\nexport default app;\n` }).includes(`host-http-server|${HOST}|${library}`));
    assert.ok(scanned({ [HOST]: `export const load = () => import('${library}');\n` }).includes(`host-http-server|${HOST}|${library}`));
  });
}

test('every vite and playwright config of gui/ is scanned for route handlers and servers', () => {
  const found = scanned({
    'gui/vite.config.preview.ts': 'export default { plugins: [{ configureServer: install }] };\n',
    'gui/playwright.config.ts': "import http from 'node:http';\nexport default { webServer: http.createServer(h) };\n",
    'gui/sub/vitest.config.mjs': "export default { plugins: [{ configurePreviewServer() {} }] };\n",
  });
  assert.deepEqual(found.sort(), [
    'host-http-server|gui/playwright.config.ts|createServer@<module>',
    'host-route-handler|gui/sub/vitest.config.mjs|configurePreviewServer',
    'host-route-handler|gui/vite.config.preview.ts|configureServer',
  ]);
});

test('two servers in one file are told apart by their enclosing function', () => {
  const found = scanned({ [HOST]: "import { createServer } from 'node:http';\nexport function one() { return createServer(a); }\nexport const two = () => createServer(b);\n" });
  assert.deepEqual(found.sort(), [`host-http-server|${HOST}|createServer@one`, `host-http-server|${HOST}|createServer@two`]);
});

test('benign look-alikes of the widened host rules pass', () => {
  assert.deepEqual(scanned({
    [HOST]: [
      "import http, { request } from 'node:http';",
      "import { createServer } from 'vite';",
      "import type { Server } from 'node:net';",
      "export const get = (url) => http.get(url);",
      "export const post = (url) => request(url);",
      "export const dev = () => createServer({ root: '.', logLevel: 'error' });",
      "export const bare = () => createServer();",
      "export const aborted = (route) => { route.abort(); route.continue(); };",
      "export const call = (plugin) => plugin.use(h);",
      "export const hook = (plugin) => plugin.configureServer;",
      '',
    ].join('\n'),
    'gui/contract-mock/serve.ts': "import { createServer } from 'node:http';\nexport const s = createServer(h);\nexport const m = (server) => server.middlewares.use(h);\n",
  }), []);
});

test('the Contract Mock module is a sanctioned host: its server is no finding, a neighbour directory still is', () => {
  const server = "import { createServer } from 'node:http';\nexport const s = createServer(h);\n";
  assert.deepEqual(scanned({ 'gui/contract-mock/serve.ts': server }), []);
  assert.ok(scanned({ 'gui/contract-mock-other/serve.ts': server, 'gui/tests/serve.ts': server }).some(key => key.startsWith('host-http-server|gui/tests/serve.ts')));
});

test('a legacy prefix is a finding inside the sanctioned host file too', () => {
  assert.ok(scanned({ 'gui/contract-mock/serve.ts': "export const u = 'x/api/gui/v1/y';\n" }).includes('host-path-literal|gui/contract-mock/serve.ts|x/api/gui/v1/y'));
});

test('the retired Node host directory is no longer sanctioned: a server or route handler there is a finding', () => {
  const server = "import { createServer } from 'node:http';\nexport const s = createServer(h);\nexport const m = (server) => server.middlewares.use(h);\n";
  const found = scanned({ 'gui/server/daemon-request/index.ts': server });
  assert.ok(found.some(key => key.startsWith('host-http-server|gui/server/daemon-request/index.ts')));
  assert.ok(found.some(key => key.startsWith('host-route-handler|gui/server/daemon-request/index.ts')));
});

function configDecision(change, options = {}) {
  let result;
  withBase({}, (root) => {
    writeFileSync(join(root, CONFIG_RELATIVE_PATH), configText(change));
    if (change.ledgerPath) writeFileSync(join(root, change.ledgerPath), ledgerText());
    result = check(root, { baseRef: 'HEAD', ...options });
  });
  return result;
}

const narrowing = [
  ['a skipped source directory is added', { skippedSourceDirectories: ['dist'] }, 'scanner-exclusion-added'],
  ['a skipped host directory is added', { skippedHostDirectories: [...BASE_CONFIG.skippedHostDirectories, 'fixtures'] }, 'scanner-exclusion-added'],
  ['a skipped file is added', { skippedFiles: ['gui/src/x.ts'] }, 'scanner-exclusion-added'],
  ['a transport file is added', { transportFiles: [...BASE_CONFIG.transportFiles, 'gui/src/x.ts'] }, 'scanner-exclusion-added'],
  ['a sanctioned host root is added', { sanctionedHostRoots: [...BASE_CONFIG.sanctionedHostRoots, 'gui/tests/'] }, 'scanner-exclusion-added'],
  ['a reserved host pattern is added', { reservedHostPatterns: [...BASE_CONFIG.reservedHostPatterns, '^https://'] }, 'scanner-exclusion-added'],
  ['a request path pattern is removed', { requestPathPatterns: [] }, 'scanner-narrowed'],
  ['a request path pattern is changed', { requestPathPatterns: ['^/api(?:/|$)'] }, 'scanner-narrowed'],
  ['a source root is removed', { sourceRoots: [] }, 'scanner-narrowed'],
  ['a host root is removed', { hostRoots: ['gui/server'] }, 'scanner-narrowed'],
  ['an extension is removed', { extensions: BASE_CONFIG.extensions.filter(item => item !== '.mjs') }, 'scanner-narrowed'],
  ['a banned prefix is removed', { bannedPrefixes: ['/api/gui/v1/'] }, 'scanner-narrowed'],
  ['a network name is removed', { networkNames: ['fetch'] }, 'scanner-narrowed'],
  ['a client library is removed', { httpClientLibraries: ['axios'] }, 'scanner-narrowed'],
  ['the ledger path changes', { ledgerPath: 'scripts/ci/other-ledger.json' }, 'scanner-constant-changed'],
  ['the transport construction file changes', { transportConstructionFile: 'gui/src/x.ts' }, 'scanner-constant-changed'],
];

for (const [label, change, kind] of narrowing) {
  test(`the scanner configuration is pinned to the base: ${label}`, () => {
    const result = configDecision(change);
    assert.equal(result.status, 'FAIL');
    assert.ok(violationKinds(result).includes(kind), violationKinds(result).join());
  });
}

test('a removed or unknown key of the scanner configuration fails', () => {
  const { bannedPrefixes, ...withoutPrefixes } = BASE_CONFIG;
  withBase({}, (root) => {
    writeFileSync(join(root, CONFIG_RELATIVE_PATH), `${JSON.stringify(withoutPrefixes)}\n`);
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['invalid-scanner-config']);
    writeFileSync(join(root, CONFIG_RELATIVE_PATH), configText({ extra: [] }));
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['invalid-scanner-config']);
  });
  withBase({ baseConfig: `${JSON.stringify({ ...BASE_CONFIG, extra: [] })}\n` }, (root) => {
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['invalid-base-scanner-config']);
  });
  assert.deepEqual(compareScannerConfigs({ ...BASE_CONFIG, extra: [] }, BASE_CONFIG).map(row => row.kind), ['scanner-key-removed']);
  assert.deepEqual(compareScannerConfigs(BASE_CONFIG, { ...BASE_CONFIG, extra: [] }).map(row => row.kind), ['scanner-key-unknown']);
});

test('additions of patterns, roots, extensions and removals of skips pass', () => {
  for (const change of [
    { requestPathPatterns: [...BASE_CONFIG.requestPathPatterns, '^/extra(?:/|$)'] },
    { sourceRoots: [...BASE_CONFIG.sourceRoots, 'gui/other'] },
    { hostRoots: [...BASE_CONFIG.hostRoots, 'gui/other'] },
    { extensions: [...BASE_CONFIG.extensions, '.vue'] },
    { bannedPrefixes: [...BASE_CONFIG.bannedPrefixes, '/api/other/'] },
    { skippedHostDirectories: ['dist'] },
    { transportFiles: BASE_CONFIG.transportFiles.slice(0, 1) },
  ]) {
    const result = configDecision(change);
    assert.deepEqual(result.violations, [], JSON.stringify(change));
    assert.equal(result.scannerOverride, false);
  }
});

test('an arbitrary scanner change passes only with the matching reviewed digest', () => {
  const change = { transportConstructionFile: 'gui/src/x.ts' };
  const digest = scannerDigest(JSON.parse(configText(change)));
  assert.equal(configDecision(change).status, 'FAIL');
  assert.equal(configDecision(change, { reviewedScannerSha256: '0'.repeat(64) }).status, 'FAIL');
  assert.equal(configDecision(change, { reviewedScannerSha256: null }).status, 'FAIL');
  const cleared = configDecision(change, { reviewedScannerSha256: digest });
  assert.equal(cleared.status, 'PASS');
  assert.equal(cleared.scannerOverride, true);
  assert.equal(configDecision({ requestPathPatterns: [] }, { reviewedScannerSha256: digest }).status, 'FAIL');
});

test('a base with a ledger but no scanner configuration needs the reviewed digest', () => {
  withBase({ baseConfig: null }, (root) => {
    assert.deepEqual(violationKinds(check(root, { baseRef: 'HEAD' })), ['missing-base-scanner-config']);
    assert.equal(check(root, { baseRef: 'HEAD', reviewedScannerSha256: scannerDigest(BASE_CONFIG) }).status, 'PASS');
  });
});

test('a base without a ledger is the first seed of the configuration as well', () => {
  withBase({ baseLedger: null, baseConfig: null }, (root) => {
    const digest = createHash('sha256').update(readFileSync(join(root, LEDGER_RELATIVE_PATH))).digest('hex');
    assert.equal(check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: digest }).status, 'PASS');
  });
});

test('the first seed digest is never consulted once the base has a ledger', () => {
  withBase({ files: { 'gui/src/x.ts': SOURCE_X }, pending: [ROW_X] }, (root) => {
    writeFileSync(join(root, 'gui/src/y.ts'), SOURCE_Y);
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), ledgerText([ROW_X, ROW_Y]));
    const digest = createHash('sha256').update(readFileSync(join(root, LEDGER_RELATIVE_PATH))).digest('hex');
    const result = check(root, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: digest });
    assert.deepEqual(violationKinds(result), ['pending-ledger-growth']);
    assert.equal(result.firstSeed, false);
  });
});

test('the command line prints the scanner digest and reports the pinned override', () => {
  const printed = spawnSync(process.execPath, [checker, '--print-scanner-digest'], { encoding: 'utf8' });
  assert.equal(printed.status, 0);
  assert.equal(printed.stdout.trim(), scannerDigest(BASE_CONFIG));
});

test('the real repository matches its ledger and every allowlist row carries a reason', () => {
  const result = check(repoRoot);
  assert.deepEqual(result.violations, []);
  const ledgerPath = join(repoRoot, LEDGER_RELATIVE_PATH);
  const parsed = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  const allowed = new Set(parsed.allowlist.map(row => `${row.kind}\0${row.path}\0${row.subject}`));
  assert.deepEqual(scan(repoRoot).filter(row => !allowed.has(`${row.kind}\0${row.path}\0${row.subject}`)), parsed.pending);
  assert.ok(parsed.allowlist.every(row => row.reason.length >= 10));
  assert.match(FIRST_SEED_LEDGER_SHA256, /^[0-9a-f]{64}$/);
  assert.ok(REVIEWED_SCANNER_SHA256 === null || /^[0-9a-f]{64}$/.test(REVIEWED_SCANNER_SHA256));
});

test('a non-null review pin must equal the committed scanner digest, any other pin is a stale pin', () => {
  const committed = scannerDigest(BASE_CONFIG);
  assert.ok(REVIEWED_SCANNER_SHA256 === null || REVIEWED_SCANNER_SHA256 === committed, 'REVIEWED_SCANNER_SHA256 is neither null nor the digest of the committed scanner configuration');
  const change = { sanctionedHostRoots: [...BASE_CONFIG.sanctionedHostRoots, 'gui/src/x.ts'] };
  const own = scannerDigest(JSON.parse(configText(change)));
  assert.equal(configDecision(change, { reviewedScannerSha256: own }).status, 'PASS');
  assert.equal(configDecision(change, { reviewedScannerSha256: committed }).status, 'FAIL');
  assert.equal(configDecision(change, { reviewedScannerSha256: 'f'.repeat(64) }).status, 'FAIL');
});
