#!/usr/bin/env node
/**
 * Contract Mock Node process form: loopback HTTP framing and the CLI.
 *
 * Decision record (1e-2, 2026-11-15)
 * - This file only frames HTTP around `createContractMock`; every answer comes
 *   from the same core the in-browser form uses. It replaces the Python mock
 *   server and keeps its observable protocol, because the source launcher and
 *   browser acceptance scripts parse it: the stderr banner lines
 *   `endpoint: http://<host>:<port>/daemon/request/<method>` and
 *   `token: Bearer <token>`, exit code 2 with `BLOCKED <reason>` on a refused
 *   configuration, and `--check` printing one line per method.
 * - Loopback only, with a per-process synthetic bearer. Path, Host, Origin,
 *   duplicate headers, query, Content-Type and Content-Length are judged before
 *   the body is read, mirroring the real transport's fail-closed framing.
 *   `/healthz` and `/mock/contract` are mock metadata; business calls stay on
 *   `POST /daemon/request/<method>`.
 * - Every response closes its connection, as the previous HTTP/1.0 server did,
 *   so `close()` never waits on a keep-alive socket.
 * - `--presets PATH` swaps the preset document, so a custom `--openapi` whose
 *   methods differ from the real catalog can run without the real guards.
 * - `table` lets a test hand over an already projected contract instead of
 *   reading the files again, which keeps per-case servers cheap.
 * - `startContractMockServer` is the programmatic entry for tests and the GUI
 *   harness; `runCli` is the process entry. Neither reads a daemon, a home or
 *   a credential store.
 */

import { randomBytes, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import type { IncomingMessage, Server, ServerResponse } from 'node:http';
import { isIPv4, isIPv6 } from 'node:net';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ContractError, SCENARIOS, availableScenarios, projectContract, withDigest } from './contract.ts';
import type { ContractTable, Scenario } from './contract.ts';
import { createContractMock } from './index.ts';
import { schemaErrors } from './schema.ts';
import type { ContractMock, MockResponse, PresetDocument, Selection } from './respond.ts';

export interface ContractMockServerOptions extends Selection {
  openapiPath?: string;
  envelopePath?: string;
  presets?: PresetDocument;
  table?: ContractTable;
  token?: string;
  host?: string;
  port?: number;
  allowOrigins?: readonly string[];
}

export interface ContractMockServer {
  endpoint: string;
  token: string;
  digest: string;
  mock: ContractMock;
  origins: readonly string[];
  close(): Promise<void>;
}

export const DEFAULT_ORIGINS: readonly string[] = [
  'http://localhost:5173',
  'http://127.0.0.1:5173',
  'http://localhost:4173',
  'http://127.0.0.1:4173',
];

const DEFAULT_OPENAPI = fileURLToPath(new URL('../../contract/daemon-request/openapi.json', import.meta.url));
const DEFAULT_ENVELOPE = fileURLToPath(new URL('../../contract/daemon-request/http-envelope.schema.json', import.meta.url));
const MAX_BODY_BYTES = 1024 * 1024;
const METHOD_PATH = /^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$/;
const FIXED_GET_PATHS = ['/healthz', '/mock/contract'];
const DUPLICATE_GUARDED = ['host', 'origin', 'authorization', 'content-type', 'content-length', 'x-hyprial-catalog-digest'];
const PREFIX = '/daemon/request/';

function readJson(path: string, label: string): unknown {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    throw new ContractError(`cannot read ${label} ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export function loadTable(openapiPath = DEFAULT_OPENAPI, envelopePath = DEFAULT_ENVELOPE): ContractTable {
  return withDigest(projectContract(readJson(openapiPath, 'business OpenAPI'), readJson(envelopePath, 'HTTP envelope schema')));
}

function isLoopback(host: string): boolean {
  if (host === 'localhost') return true;
  if (isIPv4(host)) return host.startsWith('127.');
  return isIPv6(host) && (host === '::1' || host === '0:0:0:0:0:0:0:1');
}

function hostName(value: string): string {
  if (value.startsWith('[')) return `${value.split(']', 1)[0]}]`;
  return value.split(':').length === 2 ? value.slice(0, value.lastIndexOf(':')) : value;
}

function duplicated(request: IncomingMessage, name: string): boolean {
  let seen = 0;
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    if (request.rawHeaders[index].toLowerCase() === name) seen += 1;
  }
  return seen > 1;
}

function header(request: IncomingMessage, name: string): string | undefined {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function sameToken(given: string | undefined, token: string): boolean {
  if (typeof given !== 'string') return false;
  const left = Buffer.from(given);
  const right = Buffer.from(`Bearer ${token}`);
  return left.length === right.length && timingSafeEqual(left, right);
}

function readBody(request: IncomingMessage, length: number): Promise<Buffer | null> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let received = 0;
    request.on('data', (chunk: Buffer) => {
      chunks.push(chunk);
      received += chunk.length;
    });
    request.on('end', () => resolve(received === length ? Buffer.concat(chunks) : null));
    request.on('error', () => resolve(null));
    request.on('aborted', () => resolve(null));
  });
}

export async function startContractMockServer(options: ContractMockServerOptions): Promise<ContractMockServer> {
  const host = options.host ?? '127.0.0.1';
  if (!isLoopback(host)) throw new ContractError('the Contract Mock only binds a loopback address');
  const table = options.table ?? loadTable(options.openapiPath, options.envelopePath);
  const token = options.token ?? `mock-not-a-credential-${randomBytes(8).toString('hex')}`;
  if (token === '') throw new ContractError('the mock token must be a non-empty string');
  const origins = options.allowOrigins !== undefined && options.allowOrigins.length > 0 ? [...options.allowOrigins] : [...DEFAULT_ORIGINS];
  const allowedOrigins = new Set(origins);
  const allowedHosts = new Set(['127.0.0.1', 'localhost', '[::1]', host]);
  const mock = createContractMock(
    table,
    {
      preset: options.preset,
      scenario: options.scenario,
      methodScenarios: options.methodScenarios,
      cases: options.cases,
    },
    { presets: options.presets },
  );

  const send = (response: ServerResponse, origin: string | undefined, reply: MockResponse, extra: Record<string, string> = {}) => {
    const body = Buffer.from(JSON.stringify(reply.payload), 'utf8');
    response.statusCode = reply.status;
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Content-Length', String(body.length));
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Connection', 'close');
    if (typeof reply.payload.requestId === 'string') response.setHeader('X-Hyprial-Request-Id', reply.payload.requestId);
    for (const [name, value] of Object.entries({ ...corsHeaders(origin), ...extra })) response.setHeader(name, value);
    response.end(body);
  };
  const corsHeaders = (origin: string | undefined): Record<string, string> =>
    origin !== undefined && allowedOrigins.has(origin)
      ? {
          'Access-Control-Allow-Origin': origin,
          Vary: 'Origin',
          'Access-Control-Expose-Headers': 'X-Hyprial-Request-Id',
        }
      : {};
  const fail = (response: ServerResponse, origin: string | undefined, status: number, code: string, message: string) =>
    send(response, origin, mock.errorResponse(code, message, { status }));

  const route = async (request: IncomingMessage, response: ServerResponse) => {
    const origin = header(request, 'origin');
    const url = request.url ?? '';
    const command = request.method ?? '';
    const authorization = header(request, 'authorization');
    const hostHeader = header(request, 'host');
    if (hostHeader === undefined || hostHeader === '') return fail(response, origin, 403, 'HTTP_HOST_DENIED', 'Host header is required');
    if (!allowedHosts.has(hostName(hostHeader))) return fail(response, origin, 403, 'HTTP_HOST_DENIED', 'Host is not allowed');
    if (origin !== undefined && !allowedOrigins.has(origin)) return fail(response, origin, 403, 'HTTP_ORIGIN_DENIED', 'Origin is not allowed');
    for (const name of DUPLICATE_GUARDED) {
      if (duplicated(request, name)) {
        return fail(response, origin, 400, 'INVALID_REQUEST', `duplicate ${name.replace(/(^|-)([a-z])/g, (_, dash: string, letter: string) => dash + letter.toUpperCase())} header`);
      }
    }
    if (url.includes('?') || url.includes('#')) return fail(response, origin, 400, 'INVALID_REQUEST', 'query and fragment are not accepted');
    if (command === 'OPTIONS') {
      if (origin === undefined) return fail(response, undefined, 403, 'HTTP_ORIGIN_DENIED', 'preflight requires an Origin');
      response.statusCode = 204;
      const headers = {
        ...corsHeaders(origin),
        'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
        'Access-Control-Allow-Headers': 'Authorization, Content-Type, X-Hyprial-Catalog-Digest',
        'Access-Control-Max-Age': '600',
        'Cache-Control': 'no-store',
        Connection: 'close',
      };
      for (const [name, value] of Object.entries(headers)) response.setHeader(name, value);
      response.end();
      return;
    }
    if (command === 'GET') {
      if (url === '/healthz') return send(response, origin, { status: 200, payload: { ok: true } });
      if (url === '/mock/contract') {
        if (!sameToken(authorization, token)) return unauthorized(response, origin);
        return send(response, origin, { status: 200, payload: mock.describe() });
      }
      if (url.startsWith(PREFIX)) return fail(response, origin, 405, 'HTTP_METHOD_NOT_ALLOWED', 'method is not allowed');
      return fail(response, origin, 404, 'METHOD_NOT_FOUND', 'unknown path');
    }
    if (command !== 'POST') return fail(response, origin, 405, 'HTTP_METHOD_NOT_ALLOWED', 'method is not allowed');
    if (FIXED_GET_PATHS.includes(url)) return fail(response, origin, 405, 'HTTP_METHOD_NOT_ALLOWED', 'method is not allowed');
    if (!url.startsWith(PREFIX)) return fail(response, origin, 404, 'METHOD_NOT_FOUND', 'unknown path');
    const method = url.slice(PREFIX.length);
    if (!METHOD_PATH.test(method)) return fail(response, origin, 404, 'METHOD_NOT_FOUND', 'unknown daemon method');
    if (!sameToken(authorization, token)) return unauthorized(response, origin);
    const contentType = (header(request, 'content-type') ?? '').split(';', 1)[0].trim().toLowerCase();
    if (contentType !== 'application/json') {
      return fail(response, origin, 415, 'HTTP_UNSUPPORTED_MEDIA_TYPE', 'Content-Type must be application/json');
    }
    const rawLength = header(request, 'content-length');
    if (rawLength === undefined) return fail(response, origin, 411, 'HTTP_LENGTH_REQUIRED', 'Content-Length is required');
    if (!/^[+-]?\d+$/.test(rawLength.trim())) return fail(response, origin, 400, 'INVALID_REQUEST', 'Content-Length is not an integer');
    const length = Number(rawLength);
    if (length < 0) return fail(response, origin, 400, 'INVALID_REQUEST', 'Content-Length is negative');
    if (length > MAX_BODY_BYTES) return fail(response, origin, 413, 'HTTP_BODY_TOO_LARGE', 'request body is too large');
    const body = await readBody(request, length);
    if (body === null) return fail(response, origin, 400, 'INVALID_REQUEST', 'request body is truncated');
    let params: unknown;
    try {
      params = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body));
    } catch {
      return fail(response, origin, 400, 'INVALID_REQUEST', 'request body must be a JSON object');
    }
    return send(response, origin, mock.handle(method, params));
  };
  const unauthorized = (response: ServerResponse, origin: string | undefined) =>
    fail(response, origin, 401, 'HTTP_UNAUTHORIZED', 'a mock bearer token is required');

  const server: Server = createServer((request, response) => {
    route(request, response).catch(() => {
      if (!response.headersSent) fail(response, undefined, 500, 'HTTP_INTERNAL', 'mock failed to answer');
      else response.destroy();
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port ?? 0, host, () => {
      server.off('error', reject);
      resolve();
    });
  });
  const address = server.address();
  const port = typeof address === 'object' && address !== null ? address.port : 0;
  const shown = host.includes(':') ? `[${host}]` : host;
  return {
    endpoint: `http://${shown}:${port}`,
    token,
    digest: table.digest,
    mock,
    origins,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      }),
  };
}

interface CliArguments {
  scenario?: string;
  preset?: string;
  methodScenarios: Record<string, string>;
  cases: Record<string, string>;
  token?: string;
  host: string;
  port: number;
  allowOrigins: string[];
  openapiPath?: string;
  envelopePath?: string;
  presetsPath?: string;
  check: boolean;
}

function pair(value: string, label: string): [string, string] {
  const at = value.indexOf('=');
  if (at <= 0 || at === value.length - 1) throw new ContractError(`${label} must be METHOD=VALUE`);
  return [value.slice(0, at), value.slice(at + 1)];
}

export function parseArguments(argv: readonly string[]): CliArguments {
  const parsed: CliArguments = { methodScenarios: {}, cases: {}, host: '127.0.0.1', port: 8770, allowOrigins: [], check: false };
  const valued = ['--scenario', '--preset', '--method-scenario', '--case', '--token', '--host', '--port', '--allow-origin', '--openapi', '--envelope', '--presets'];
  for (let index = 0; index < argv.length; index += 1) {
    let name = argv[index];
    let value: string | undefined;
    const equals = name.indexOf('=');
    if (name.startsWith('--') && equals > 0) {
      value = name.slice(equals + 1);
      name = name.slice(0, equals);
    }
    if (name === '--check') {
      parsed.check = true;
      continue;
    }
    if (!valued.includes(name)) throw new ContractError(`unrecognized argument ${name}`);
    if (value === undefined) {
      index += 1;
      value = argv[index];
      if (value === undefined) throw new ContractError(`argument ${name}: expected one argument`);
    }
    switch (name) {
      case '--scenario':
        if (!(SCENARIOS as readonly string[]).includes(value)) throw new ContractError(`argument --scenario: invalid choice ${value}`);
        parsed.scenario = value;
        break;
      case '--preset':
        parsed.preset = value;
        break;
      case '--method-scenario':
      case '--case': {
        const [method, item] = pair(value, name);
        const target = name === '--case' ? parsed.cases : parsed.methodScenarios;
        if (method in target) throw new ContractError(`${name} repeats method '${method}'`);
        target[method] = item;
        break;
      }
      case '--token':
        parsed.token = value;
        break;
      case '--host':
        parsed.host = value;
        break;
      case '--port':
        if (!/^\d+$/.test(value) || Number(value) > 65535) throw new ContractError(`argument --port: invalid port ${value}`);
        parsed.port = Number(value);
        break;
      case '--allow-origin':
        parsed.allowOrigins.push(value);
        break;
      case '--openapi':
        parsed.openapiPath = value;
        break;
      case '--envelope':
        parsed.envelopePath = value;
        break;
      default:
        parsed.presetsPath = value;
    }
  }
  if (parsed.scenario === undefined && parsed.preset === undefined) throw new ContractError('the following arguments are required: --scenario');
  return parsed;
}

function checkLines(mock: ContractMock): { out: string[]; err: string[] } {
  const table = mock.table;
  const out = [`PASS contract ${table.digest} version ${table.contractVersion}`];
  const err: string[] = [];
  const names = Object.keys(table.methods).sort();
  for (const name of names) out.push(`${name}: scenarios=${availableScenarios(table.methods[name]).join(', ') || 'none'}`);
  const unsupported = mock.unsupportedMethods();
  if (unsupported.length > 0) err.push(`unsupported for the selected scenario: ${unsupported.join(', ')}`);
  for (const name of names) {
    const example = mock.selectedCase(name);
    if (example === null) {
      out.push(`${name}/${mock.scenarioFor(name)}: not-declared`);
      continue;
    }
    const reply = mock.handle(name, example.params);
    out.push(`${name}/${example.name}: ${reply.status} ${example.kind}`);
    if (reply.payload.ok === true && !['success', 'empty', 'unconfigured'].includes(example.kind)) {
      err.push(`${name}/${example.name}: answered ok:true for an example of kind ${example.kind}`);
    }
    if (example.kind === 'invalid-params') {
      const entry = table.methods[name];
      const valid = entry.examples.find((other) => other.kind !== 'invalid-params' && schemaErrors(entry.paramsSchema, other.params).length === 0);
      if (valid !== undefined) {
        const again = mock.handle(name, valid.params);
        out.push(`${name}/${example.name} (schema-valid params): ${again.status} ${example.kind}`);
        if (again.payload.ok !== false) err.push(`${name}/${example.name}: a schema-valid request was answered ok:true for an invalid-params example`);
      }
    }
  }
  return { out, err };
}

export async function runCli(argv: readonly string[]): Promise<number> {
  let args: CliArguments;
  try {
    args = parseArguments(argv);
  } catch (error) {
    process.stderr.write(`BLOCKED ${error instanceof Error ? error.message : String(error)}\n`);
    return 2;
  }
  let started: ContractMockServer;
  try {
    const options: ContractMockServerOptions = {
      preset: args.preset,
      scenario: args.scenario as Scenario | undefined,
      methodScenarios: args.methodScenarios as Record<string, Scenario>,
      cases: args.cases,
      openapiPath: args.openapiPath,
      envelopePath: args.envelopePath,
      presets: args.presetsPath === undefined ? undefined : (readJson(args.presetsPath, 'preset document') as PresetDocument),
      token: args.token,
      host: args.host,
      port: args.check ? 0 : args.port,
      allowOrigins: args.allowOrigins,
    };
    started = await startContractMockServer(options);
  } catch (error) {
    process.stderr.write(`BLOCKED ${error instanceof Error ? error.message : String(error)}\n`);
    return 2;
  }
  const { mock, token, digest } = started;
  if (args.check) {
    const lines = checkLines(mock);
    await started.close();
    if (lines.err.length > 0) process.stderr.write(`${lines.err.join('\n')}\n`);
    process.stdout.write(`${lines.out.join('\n')}\n`);
    return lines.err.some((line) => line.includes('ok:true')) ? 1 : 0;
  }
  const stopped = new Promise<void>((resolve) => {
    for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => resolve());
  });
  const described = mock.describe() as { scenario: string; envelopeSource: { commit?: string }; unsupportedForScenario: string[] };
  const shown = args.host.includes(':') ? `[${args.host}]` : args.host;
  const port = new URL(started.endpoint).port;
  process.stderr.write(
    [
      'hyprial Contract Mock (not the daemon, not H)',
      `endpoint: http://${shown}:${port}/daemon/request/<method>`,
      `scenario: ${described.scenario}`,
      `token: Bearer ${token}`,
      `contract: ${digest} version ${mock.table.contractVersion}`,
      `envelope source: ${described.envelopeSource.commit ?? 'unknown'}`,
      `allowed origins: ${[...started.origins].sort().join(', ')}`,
      `unsupported for this scenario: ${described.unsupportedForScenario.join(', ') || 'none'}`,
    ].join('\n') + '\n',
  );
  await stopped;
  await started.close();
  return 0;
}

const invoked = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) {
  runCli(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (error: unknown) => {
      process.stderr.write(`BLOCKED ${error instanceof Error ? error.message : String(error)}\n`);
      process.exit(2);
    },
  );
}
