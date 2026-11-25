// @vitest-environment node
/**
 * Contract Mock Node process form: HTTP framing, CLI protocol and `--check`.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Ported from tests/daemon/contract_mock/test_mock_http.py (eight tests) and
 *   extended with the framing rules the Python server enforced but did not
 *   test: Host, duplicate headers, Content-Length and body size.
 * - The CLI tests spawn the real entry with plain Node, which also proves the
 *   module loads without a build step. The stderr banner must stay parseable by
 *   `MOCK_ENDPOINT_RE` and `MOCK_TOKEN_PREFIX` in the source launcher; a test
 *   reads those literals from the Python source so the two cannot drift.
 */

import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { EMPTY_PRESETS, schemaErrors } from '../../contract-mock/index.ts';
import { startContractMockServer } from '../../contract-mock/serve.ts';
import type { ContractMockServer } from '../../contract-mock/serve.ts';
import { realEnvelope, syntheticOpenapi } from './synthetic.ts';

const TOKEN = 'mock-test-token';
const ORIGIN = 'http://localhost:5173';
const SERVE = fileURLToPath(new URL('../../contract-mock/serve.ts', import.meta.url));
const SOURCE_PY = fileURLToPath(new URL('../../../src/hyprial/shell/impl/gui/backend/source.py', import.meta.url));

let directory: string;
let openapiPath: string;
let presetsPath: string;
let live: ContractMockServer;
let base: URL;

beforeAll(async () => {
  directory = mkdtempSync(join(tmpdir(), 'contract-mock-serve-'));
  openapiPath = join(directory, 'openapi.json');
  presetsPath = join(directory, 'presets.json');
  writeFileSync(openapiPath, JSON.stringify(syntheticOpenapi()));
  writeFileSync(presetsPath, JSON.stringify(EMPTY_PRESETS));
  live = await startContractMockServer({ scenario: 'success', openapiPath, presets: EMPTY_PRESETS, token: TOKEN, allowOrigins: [ORIGIN], port: 0 });
  base = new URL(live.endpoint);
});

afterAll(async () => {
  await live.close();
  rmSync(directory, { recursive: true, force: true });
});

interface Reply {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: any;
}

function call(
  path: string,
  options: { method?: string; payload?: unknown; raw?: string; headers?: Record<string, string | string[]>; length?: number | null } = {},
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const data = options.raw ?? (options.payload === undefined ? undefined : JSON.stringify(options.payload));
    const headers: Record<string, string | string[]> = { 'Content-Type': 'application/json', ...options.headers };
    if (options.length !== null && data !== undefined) headers['Content-Length'] = String(options.length ?? Buffer.byteLength(data));
    const pending = request(
      { host: base.hostname, port: base.port, path, method: options.method ?? 'POST', headers },
      (response) => {
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => chunks.push(chunk));
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          resolve({ status: response.statusCode ?? 0, headers: response.headers, body: text ? JSON.parse(text) : null });
        });
      },
    );
    pending.on('error', reject);
    if (data !== undefined) pending.write(data);
    pending.end();
  });
}

const auth = { Authorization: `Bearer ${TOKEN}` };

describe('framing', () => {
  it('serves success in the pinned envelope with the pinned headers', async () => {
    const reply = await call('/daemon/request/demo.list', { payload: { filter: null }, headers: { ...auth, Origin: ORIGIN } });
    expect(reply.status).toBe(200);
    expect(reply.body.ok).toBe(true);
    expect(reply.body.dispatch).toBe('dispatched');
    expect(reply.body.result.items).toEqual(['alpha']);
    expect(schemaErrors(realEnvelope().$defs.success, reply.body)).toEqual([]);
    expect(reply.headers['x-hyprial-request-id']).toBe(reply.body.requestId);
    expect(reply.headers['access-control-allow-origin']).toBe(ORIGIN);
    expect(reply.headers['cache-control']).toBe('no-store');
    expect(reply.headers['content-type']).toBe('application/json; charset=utf-8');
  });

  it('keeps /healthz the only unauthenticated GET', async () => {
    const health = await call('/healthz', { method: 'GET', headers: {} });
    expect(health.status).toBe(200);
    expect(health.body).toEqual({ ok: true });
    const metadata = await call('/mock/contract', { method: 'GET', headers: {} });
    expect(metadata.status).toBe(401);
    expect(metadata.body.code).toBe('HTTP_UNAUTHORIZED');
  });

  it('requires the bearer for the metadata document', async () => {
    const reply = await call('/mock/contract', { method: 'GET', headers: auth });
    expect(reply.status).toBe(200);
    expect(reply.body.mock).toBe(true);
    expect(reply.body.contractDigest).toBe(live.digest);
    expect(reply.body.scenario).toBe('success');
  });

  it('refuses a disallowed Origin without echoing it', async () => {
    const reply = await call('/daemon/request/demo.list', {
      payload: { filter: null },
      headers: { ...auth, Origin: 'http://evil.example' },
    });
    expect(reply.status).toBe(403);
    expect(reply.body.code).toBe('HTTP_ORIGIN_DENIED');
    expect(reply.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('answers a preflight only for configured origins', async () => {
    const allowed = await call('/daemon/request/demo.list', { method: 'OPTIONS', headers: { Origin: ORIGIN } });
    expect(allowed.status).toBe(204);
    expect(allowed.headers['access-control-allow-origin']).toBe(ORIGIN);
    expect(String(allowed.headers['access-control-allow-headers'])).toContain('Authorization');
    expect(allowed.body).toBeNull();
    const denied = await call('/daemon/request/demo.list', { method: 'OPTIONS', headers: { Origin: 'http://evil.example' } });
    expect(denied.status).toBe(403);
    expect(denied.body.code).toBe('HTTP_ORIGIN_DENIED');
    const bare = await call('/daemon/request/demo.list', { method: 'OPTIONS', headers: {} });
    expect(bare.status).toBe(403);
  });

  it('refuses invalid JSON and a non-object body', async () => {
    const invalid = await call('/daemon/request/demo.list', { raw: '{not json', headers: auth });
    expect(invalid.status).toBe(400);
    expect(invalid.body.code).toBe('INVALID_REQUEST');
    const array = await call('/daemon/request/demo.list', { payload: [1, 2], headers: auth });
    expect(array.status).toBe(400);
    expect(array.body.code).toBe('INVALID_ARGUMENT');
  });

  it('refuses a wrong media type and an unknown method', async () => {
    const media = await call('/daemon/request/demo.list', { raw: '{}', headers: { ...auth, 'Content-Type': 'text/plain' } });
    expect(media.status).toBe(415);
    expect(media.body.code).toBe('HTTP_UNSUPPORTED_MEDIA_TYPE');
    const missing = await call('/daemon/request/demo.missing', { payload: {}, headers: auth });
    expect(missing.status).toBe(404);
    expect(missing.body.code).toBe('METHOD_NOT_FOUND');
  });

  it('refuses the wrong HTTP method and a query string', async () => {
    const wrong = await call('/daemon/request/demo.list', { method: 'GET', headers: auth });
    expect(wrong.status).toBe(405);
    expect(wrong.body.code).toBe('HTTP_METHOD_NOT_ALLOWED');
    const query = await call('/daemon/request/demo.list?x=1', { payload: { filter: null }, headers: auth });
    expect(query.status).toBe(400);
    expect(query.body.code).toBe('INVALID_REQUEST');
    const put = await call('/daemon/request/demo.list', { method: 'PUT', payload: {}, headers: auth });
    expect(put.status).toBe(405);
  });

  it('refuses a missing or wrong bearer before reading the body', async () => {
    for (const headers of [{}, { Authorization: 'Bearer wrong' }, { Authorization: TOKEN }] as Record<string, string>[]) {
      const reply = await call('/daemon/request/demo.list', { payload: { filter: null }, headers });
      expect(reply.status).toBe(401);
      expect(reply.body.code).toBe('HTTP_UNAUTHORIZED');
      expect(reply.body.dispatch).toBe('not-dispatched');
    }
  });

  it('refuses a Host that is not loopback', async () => {
    const reply = await call('/healthz', { method: 'GET', headers: { Host: 'evil.example' } });
    expect(reply.status).toBe(403);
    expect(reply.body.code).toBe('HTTP_HOST_DENIED');
  });

  it('refuses duplicate guarded headers', async () => {
    const reply = await call('/daemon/request/demo.list', {
      payload: { filter: null },
      headers: { ...auth, 'X-Hyprial-Catalog-Digest': ['a', 'b'] },
    });
    expect(reply.status).toBe(400);
    expect(reply.body).toMatchObject({ code: 'INVALID_REQUEST', message: 'duplicate X-Hyprial-Catalog-Digest header' });
  });

  it('judges Content-Length before the body', async () => {
    const missing = await call('/daemon/request/demo.list', { raw: '{}', headers: auth, length: null });
    expect(missing.status).toBe(411);
    const wrongPath = await call('/nope', { payload: {}, headers: auth });
    expect(wrongPath.status).toBe(404);
    const large = await call('/daemon/request/demo.list', { raw: '', headers: { ...auth, 'Content-Length': '2000000' }, length: null });
    expect(large.status).toBe(413);
    expect(large.body.code).toBe('HTTP_BODY_TOO_LARGE');
  });

  it('routes the fixed GET paths away from POST', async () => {
    const reply = await call('/healthz', { payload: {}, headers: auth });
    expect(reply.status).toBe(405);
  });
});

describe('startContractMockServer', () => {
  it('refuses a non-loopback bind', async () => {
    await expect(startContractMockServer({ scenario: 'success', openapiPath, presets: EMPTY_PRESETS, host: '0.0.0.0' })).rejects.toThrow(/loopback/);
  });

  it('mints a synthetic token and reports a base endpoint', async () => {
    const other = await startContractMockServer({ scenario: 'empty', openapiPath, presets: EMPTY_PRESETS });
    try {
      expect(other.token).toMatch(/^mock-not-a-credential-[0-9a-f]{16}$/);
      expect(other.endpoint).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
      expect(other.mock.describe().scenario).toBe('empty');
    } finally {
      await other.close();
    }
  });

  it('applies explicit selection overrides', async () => {
    const other = await startContractMockServer({ scenario: 'success', openapiPath, presets: EMPTY_PRESETS, cases: { 'demo.list': 'daemon-restoring' }, token: TOKEN });
    try {
      const url = new URL(other.endpoint);
      const reply = await new Promise<number>((resolve) => {
        const pending = request(
          { host: url.hostname, port: url.port, path: '/daemon/request/demo.list', method: 'POST', headers: { ...auth, 'Content-Type': 'application/json', 'Content-Length': '15' } },
          (response) => {
            response.resume();
            resolve(response.statusCode ?? 0);
          },
        );
        pending.end('{"filter":null}');
      });
      expect(reply).toBe(503);
    } finally {
      await other.close();
    }
  });
});

function runNode(args: string[]) {
  return spawnSync(process.execPath, [SERVE, ...args], { encoding: 'utf8', timeout: 60_000 });
}

describe('command line', () => {
  it('prints a banner the source launcher can parse', async () => {
    const launcher = readFileSync(SOURCE_PY, 'utf8');
    expect(launcher).toContain(String.raw`r"^endpoint: (http://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]+)/daemon/request/<method>$"`);
    expect(launcher).toContain('MOCK_TOKEN_PREFIX = "token: Bearer "');
    const child = spawn(process.execPath, [SERVE, '--scenario', 'success', '--openapi', openapiPath, '--presets', presetsPath, '--host', '127.0.0.1', '--port', '0', '--allow-origin', ORIGIN], {
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let output = '';
    try {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`no banner:\n${output}`)), 20_000);
        child.stderr.setEncoding('utf8');
        child.stderr.on('data', (chunk: string) => {
          output += chunk;
          if (output.includes('unsupported for this scenario:')) {
            clearTimeout(timer);
            resolve();
          }
        });
        child.once('exit', (code) => reject(new Error(`exited ${code}:\n${output}`)));
      });
      const lines = output.split('\n');
      const endpoint = /^endpoint: (http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]+)\/daemon\/request\/<method>$/;
      const found = lines.map((line) => endpoint.exec(line)).find(Boolean);
      expect(found).toBeTruthy();
      const token = lines.find((line) => line.startsWith('token: Bearer '))!.slice('token: Bearer '.length);
      expect(token).toMatch(/^mock-not-a-credential-[0-9a-f]{16}$/);
      expect(lines[0]).toBe('hyprial Contract Mock (not the daemon, not H)');
      expect(lines).toContain('scenario: success');
      expect(lines.some((line) => /^contract: sha256:[0-9a-f]{64} version 1$/.test(line))).toBe(true);
      expect(lines).toContain(`allowed origins: ${ORIGIN}`);
      expect(lines).toContain('unsupported for this scenario: none');
      const url = new URL(found![1]);
      const status = await new Promise<number>((resolve, reject) => {
        const pending = request({ host: url.hostname, port: url.port, path: '/mock/contract', headers: { Authorization: `Bearer ${token}` } }, (response) => {
          response.resume();
          resolve(response.statusCode ?? 0);
        });
        pending.on('error', reject);
        pending.end();
      });
      expect(status).toBe(200);
    } finally {
      child.kill('SIGTERM');
      await new Promise((resolve) => child.once('exit', resolve));
    }
  });

  it('exits 0 on SIGTERM', async () => {
    const child = spawn(process.execPath, [SERVE, '--scenario', 'success', '--openapi', openapiPath, '--presets', presetsPath, '--port', '0'], { stdio: ['ignore', 'ignore', 'pipe'] });
    await new Promise<void>((resolve) => {
      child.stderr.on('data', (chunk: Buffer) => chunk.toString().includes('unsupported for this scenario') && resolve());
    });
    const exited = new Promise<number | null>((resolve) => child.once('exit', resolve));
    child.kill('SIGTERM');
    expect(await exited).toBe(0);
  });

  it('checks the whole contract with one line per method', () => {
    const result = runNode(['--scenario', 'success', '--openapi', openapiPath, '--presets', presetsPath, '--check']);
    expect(result.status).toBe(0);
    const lines = result.stdout.trim().split('\n');
    expect(lines[0]).toMatch(/^PASS contract sha256:[0-9a-f]{64} version 1$/);
    expect(lines).toContain('demo.list: scenarios=success, empty, error');
    expect(lines).toContain('demo.list/all: 200 success');
    expect(lines).toContain('demo.write/removed: 200 success');
    expect(lines.length).toBe(1 + 3 + 3);
  });

  it('exercises a forced invalid-params case with params that pass the schema and never answers ok:true', () => {
    const result = runNode([
      '--scenario', 'success', '--openapi', openapiPath, '--presets', presetsPath, '--case', 'demo.list=wrong-filter-type', '--check',
    ]);
    expect(result.status).toBe(0);
    const lines = result.stdout.trim().split('\n');
    expect(lines).toContain('demo.list/wrong-filter-type: 400 invalid-params');
    expect(lines).toContain('demo.list/wrong-filter-type (schema-valid params): 400 invalid-params');
  });

  it('answers a forced invalid-params case over HTTP for schema-valid params', async () => {
    const server = await startContractMockServer({
      scenario: 'success', cases: { 'demo.list': 'wrong-filter-type' }, openapiPath, presets: EMPTY_PRESETS, token: 'forced-token', port: 0,
    });
    try {
      const reply = await fetch(`${server.endpoint}/daemon/request/demo.list`, {
        method: 'POST',
        headers: { authorization: 'Bearer forced-token', 'content-type': 'application/json' },
        body: JSON.stringify({ filter: null }),
      });
      const body = await reply.json();
      expect(reply.status).toBe(400);
      expect(body).toMatchObject({ ok: false, code: 'INVALID_ARGUMENT', dispatch: 'not-dispatched' });
      expect(body).not.toHaveProperty('result');
    } finally {
      await server.close();
    }
  });

  it('reports the methods a scenario cannot serve on stderr', () => {
    const result = runNode(['--scenario', 'empty', '--openapi', openapiPath, '--presets', presetsPath, '--check']);
    expect(result.status).toBe(0);
    expect(result.stderr).toContain('unsupported for the selected scenario: demo.get, demo.write');
    expect(result.stdout).toContain('demo.write/empty: not-declared');
  });

  it('checks the real contract with plain Node', () => {
    const result = runNode(['--scenario', 'success', '--check']);
    expect(result.status).toBe(0);
    const lines = result.stdout.trim().split('\n');
    const methods = lines.filter((line) => line.includes(': scenarios=')).length;
    expect(methods).toBeGreaterThan(100);
    expect(lines.length).toBe(1 + 2 * methods);
    expect(lines.filter((line) => /: 200 success$/.test(line)).length).toBe(methods);
  });

  it.each([
    [[], /required: --scenario/],
    [['--scenario', 'bogus'], /invalid choice/],
    [['--scenario', 'success', '--bogus'], /unrecognized argument/],
    [['--scenario', 'success', '--method-scenario', 'demo.write=empty'], /no empty example for the explicit override/],
    [['--scenario', 'success', '--case', 'demo.list=all', '--case', 'demo.list=all'], /repeats method/],
    [['--scenario', 'success', '--case', 'oops'], /must be METHOD=VALUE/],
    [['--scenario', 'success', '--host', '0.0.0.0'], /loopback/],
    [['--scenario', 'success', '--openapi', '/nonexistent/openapi.json'], /cannot read business OpenAPI/],
  ])('blocks a refused configuration with exit 2: %j', (args, pattern) => {
    const result = runNode([...args, ...(args.includes('--openapi') ? [] : ['--openapi', openapiPath]), '--presets', presetsPath]);
    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/^BLOCKED /);
    expect(result.stderr).toMatch(pattern);
  });
});
