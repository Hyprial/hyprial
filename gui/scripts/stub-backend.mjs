#!/usr/bin/env node

import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

export const BACKEND_NOT_WIRED =
  'not implemented: hyprial backend not wired';
export const BACKEND_NOT_WIRED_CODE = 'BACKEND_NOT_WIRED';
export const STUB_BUSINESS_PATH = '/api/gui/v1/not-wired';

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);
const MAX_BODY_BYTES = 64 * 1024;
const SENSITIVE_KEY =
  /(token|secret|password|passwd|credential|authorization|cookie|api[-_]?key|private[-_]?key|access[-_]?key|key)$/i;

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function redactValue(value, key, depth) {
  if (SENSITIVE_KEY.test(key)) {
    return '<redacted>';
  }
  if (depth >= 4) {
    return '<truncated>';
  }
  if (Array.isArray(value)) {
    return value
      .slice(0, 20)
      .map((entry) => redactValue(entry, key, depth + 1));
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value)
        .slice(0, 50)
        .map(([childKey, childValue]) => [
          childKey,
          redactValue(childValue, childKey, depth + 1),
        ]),
    );
  }
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : '<non-finite>';
  }
  return '<unsupported>';
}

export function redactParams(params) {
  if (!isRecord(params)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(params)
      .slice(0, 50)
      .map(([key, value]) => [key, redactValue(value, key, 0)]),
  );
}

function json(response, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  response.statusCode = status;
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  for (const [name, value] of Object.entries(headers)) {
    response.setHeader(name, value);
  }
  response.end(body);
}

function notWired(response, command, params) {
  json(
    response,
    501,
    {
      ok: false,
      code: BACKEND_NOT_WIRED_CODE,
      message: BACKEND_NOT_WIRED,
      command,
      params: redactParams(params),
    },
    { 'X-Hyprial-Backend': 'not-wired' },
  );
}

function readJsonBody(request) {
  return new Promise((resolveBody, rejectBody) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    request.on('data', (chunk) => {
      if (settled) {
        return;
      }
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        settled = true;
        chunks.length = 0;
        rejectBody(
          Object.assign(new Error('request body is too large'), {
            code: 'REQUEST_TOO_LARGE',
          }),
        );
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      if (settled) {
        return;
      }
      settled = true;
      resolveBody(Buffer.concat(chunks).toString('utf8'));
    });
    request.on('error', (error) => {
      if (settled) {
        return;
      }
      settled = true;
      rejectBody(error);
    });
  });
}

async function handleBusinessPost(request, response) {
  const contentType = String(request.headers['content-type'] ?? '').split(';')[0];
  if (contentType.trim().toLowerCase() !== 'application/json') {
    json(response, 415, {
      ok: false,
      code: 'UNSUPPORTED_MEDIA_TYPE',
      message: 'expected application/json',
      command: 'invalid.request',
      params: {},
    });
    return;
  }

  let raw;
  try {
    raw = await readJsonBody(request);
  } catch (error) {
    if (error && error.code === 'REQUEST_TOO_LARGE') {
      json(response, 413, {
        ok: false,
        code: 'REQUEST_TOO_LARGE',
        message: `request body exceeds ${MAX_BODY_BYTES} bytes`,
        command: 'invalid.request',
        params: {},
      });
      return;
    }
    json(response, 400, {
      ok: false,
      code: 'INVALID_REQUEST',
      message: 'could not read request body',
      command: 'invalid.request',
      params: {},
    });
    return;
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    json(response, 400, {
      ok: false,
      code: 'INVALID_JSON',
      message: 'request body must be valid JSON',
      command: 'invalid.request',
      params: {},
    });
    return;
  }

  if (
    !isRecord(body) ||
    typeof body.command !== 'string' ||
    !/^[a-z][a-z0-9._-]{0,79}$/.test(body.command)
  ) {
    json(response, 400, {
      ok: false,
      code: 'INVALID_COMMAND',
      message:
        'command must match ^[a-z][a-z0-9._-]{0,79}$; business is not implemented',
      command: 'invalid.command',
      params: {},
    });
    return;
  }

  if (body.params !== undefined && !isRecord(body.params)) {
    json(response, 400, {
      ok: false,
      code: 'INVALID_PARAMS',
      message: 'params must be a JSON object',
      command: body.command,
      params: {},
    });
    return;
  }

  notWired(response, body.command, body.params ?? {});
}

function createRequestHandler() {
  return (request, response) => {
    const method = request.method ?? 'GET';
    let pathname = '/';
    try {
      pathname = new URL(request.url ?? '/', 'http://127.0.0.1').pathname;
    } catch {
      json(response, 400, {
        ok: false,
        code: 'INVALID_PATH',
        message: 'invalid request path',
        command: 'invalid.request',
        params: {},
      });
      return;
    }

    if (method === 'GET' && pathname === '/healthz') {
      json(response, 200, {
        ok: true,
        service: 'hyprial-gui-stub',
        status: 'ok',
      });
      return;
    }

    if (method === 'GET' && pathname === '/readyz') {
      json(response, 200, {
        ok: true,
        service: 'hyprial-gui-stub',
        status: 'ready',
        backend: 'not-wired',
      });
      return;
    }

    if (method === 'POST' && pathname === STUB_BUSINESS_PATH) {
      void handleBusinessPost(request, response).catch(() => {
        if (!response.headersSent) {
          json(response, 500, {
            ok: false,
            code: 'STUB_INTERNAL_ERROR',
            message: 'stub failed without executing business',
            command: 'invalid.request',
            params: {},
          });
        }
      });
      return;
    }

    request.resume();
    notWired(response, 'http.request', {
      method,
      path: pathname,
    });
  };
}

function endpointFor(host, port) {
  const hostPart = host.includes(':') ? `[${host}]` : host;
  return `http://${hostPart}:${port}`;
}

export function createStubServer({ host = '127.0.0.1', port = 0 } = {}) {
  return new Promise((resolveServer, rejectServer) => {
    const server = http.createServer(createRequestHandler());
    server.on('clientError', (_error, socket) => {
      if (socket.writable) {
        socket.end(
          'HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n',
        );
      }
    });
    server.once('error', rejectServer);
    server.listen(port, host, () => {
      server.off('error', rejectServer);
      const address = server.address();
      if (typeof address !== 'object' || address === null) {
        rejectServer(new Error('stub server did not bind a TCP port'));
        return;
      }
      resolveServer({
        server,
        host: address.address,
        port: address.port,
        endpoint: endpointFor(address.address, address.port),
      });
    });
  });
}

export function closeServer(server) {
  return new Promise((resolveClose, rejectClose) => {
    server.close((error) => {
      if (error) {
        rejectClose(error);
        return;
      }
      resolveClose();
    });
  });
}

function usage() {
  return [
    'Usage: node scripts/stub-backend.mjs [--host HOST] [--port PORT]',
    '',
    'Defaults: --host 127.0.0.1 --port 0 (ephemeral loopback port)',
    'HTTP: GET /healthz, GET /readyz; every business request returns 501.',
  ].join('\n');
}

export function parseArgs(argv) {
  let host = '127.0.0.1';
  let port = 0;

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--help' || argument === '-h') {
      return { help: true, host, port };
    }
    if (argument === '--host') {
      const value = argv[index + 1];
      if (!value) {
        throw new Error('--host requires a value');
      }
      host = value;
      index += 1;
      continue;
    }
    if (argument.startsWith('--host=')) {
      host = argument.slice('--host='.length);
      continue;
    }
    if (argument === '--port') {
      const value = argv[index + 1];
      if (!value) {
        throw new Error('--port requires a value');
      }
      port = Number(value);
      index += 1;
      continue;
    }
    if (argument.startsWith('--port=')) {
      port = Number(argument.slice('--port='.length));
      continue;
    }
    throw new Error(`unknown argument: ${argument}`);
  }

  if (!LOOPBACK_HOSTS.has(host)) {
    throw new Error('--host must be a loopback host');
  }
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error('--port must be an integer between 0 and 65535');
  }

  return { help: false, host, port };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log(usage());
    return;
  }

  const started = await createStubServer(args);
  console.log(
    `HYPRIAL_STUB_READY ${JSON.stringify({
      service: 'hyprial-gui-stub',
      host: started.host,
      port: started.port,
      endpoint: started.endpoint,
      backend: 'not-wired',
    })}`,
  );

  let closing = false;
  const shutdown = async (signal) => {
    if (closing) {
      return;
    }
    closing = true;
    try {
      await closeServer(started.server);
    } finally {
      console.log(`HYPRIAL_STUB_STOPPED ${JSON.stringify({ signal })}`);
    }
  };

  process.on('SIGTERM', () => {
    void shutdown('SIGTERM');
  });
  process.on('SIGINT', () => {
    void shutdown('SIGINT');
  });
}

const invokedDirectly =
  process.argv[1] !== undefined &&
  pathToFileURL(resolve(process.argv[1])).href === import.meta.url;

if (invokedDirectly) {
  main().catch((error) => {
    console.error(
      `HYPRIAL_STUB_ERROR ${JSON.stringify({
        code: error && error.code ? error.code : 'STUB_ERROR',
        message: error instanceof Error ? error.message : 'unknown error',
      })}`,
    );
    process.exitCode = 1;
  });
}
