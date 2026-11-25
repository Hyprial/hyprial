// @vitest-environment node

/**
 * Decision record (host conformance entry, 2026-11-15)
- This file is the single entry of the black-box host suite. It owns the one
  HTTP listener that upstream stubs use (the request-path ledger records that
  createServer here) and binds it into host-harness.ts, then registers the
  conformance modules (admission, session, relay) that hold the contract
  cases. Those modules are plain functions so they add no listener of their
  own.
- Cases below the registrations predate the contract suites and keep their
  original wording.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  DECLARATION, DIGEST, EPOCH, GRANT, PRINCIPAL, UPSTREAM_TOKEN,
  bindListen, browserHeaders, call, catalog, cleanups, config, conforms, cookiesFrom, host, methodSuccess, open,
  ready, runCleanups, sendJson, upstream,
} from './host-harness';
import { loadSessionContract, type JsonObject } from './session-corpus';
import { HOST_TEST_TIMEOUT_MS } from '../support/python-host';

vi.setConfig({ testTimeout: HOST_TEST_TIMEOUT_MS });
import { registerAdmissionConformance } from './conformance-admission';
import { registerRelayConformance } from './conformance-relay';
import { registerSessionConformance } from './conformance-session';

const LIMITS = loadSessionContract().limits as JsonObject;
const MAX_REQUEST_BYTES = LIMITS.methodBodyBytes as number;
const MAX_RESPONSE_BYTES = LIMITS.upstreamResponseBytes as number;

async function listen(handler: (request: IncomingMessage, response: ServerResponse) => void | Promise<void>): Promise<string> {
  const server = createServer((request, response) => {
    void Promise.resolve(handler(request, response)).catch(() => {
      response.statusCode = 500;
      response.end('{"ok":false}');
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  cleanups.push(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}


bindListen(listen);
registerAdmissionConformance();
registerSessionConformance();
registerRelayConformance();


afterEach(runCleanups);


describe('same-origin host admission', () => {
  it('requires exact loopback Host/Origin and same-origin Fetch Metadata', async () => {
    const target = await upstream();
    const gateway = await host(config(target.endpoint));

    const missing = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: GRANT }, omitOrigin: true });
    expect(missing.status).toBe(403);
    expect(missing.json).toMatchObject({ ok: false, code: 'ORIGIN_REQUIRED', dispatch: 'not-dispatched' });

    const wrongOrigin = await call(gateway.origin, '/daemon/session', {
      method: 'POST', body: { grant: GRANT }, origin: 'http://hostile.invalid',
    });
    expect(wrongOrigin.status).toBe(403);
    expect(wrongOrigin.json).toMatchObject({ ok: false, code: 'ORIGIN_REJECTED' });
    conforms('hostError', wrongOrigin.json);

    const wrongHost = await call(gateway.origin, '/daemon/session', {
      method: 'POST', body: { grant: GRANT }, host: 'localhost:9999',
    });
    expect(wrongHost.status).toBe(403);
    expect(wrongHost.json).toMatchObject({ ok: false, code: 'HTTP_HOST_DENIED' });

    const crossSite = await call(gateway.origin, '/daemon/session', {
      method: 'POST', body: { grant: GRANT }, headers: { 'sec-fetch-site': 'cross-site' },
    });
    expect(crossSite.status).toBe(403);
    expect(crossSite.json).toMatchObject({ ok: false, code: 'ORIGIN_REJECTED' });
    expect(target.captured).toHaveLength(0);
  });

  it('allows an authenticated read without Origin but keeps state changes explicit', async () => {
    const target = await upstream();
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    expect(opened.response.status).toBe(200);

    const read = await call(gateway.origin, '/daemon/readyz', {
      omitOrigin: true,
      headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(read.status).toBe(200);
    expect(read.json).toMatchObject({ ready: true, transport: 'ready' });

    const drift = await call(gateway.origin, '/daemon/readyz', {
      headers: { ...browserHeaders(opened.session, opened.cookies), 'x-hyprial-catalog-digest': 'sha256:other' },
    });
    expect(drift.status).toBe(409);
    expect(drift.json).toMatchObject({ code: 'CATALOG_MISMATCH', dispatch: 'not-dispatched' });

    const write = await call(gateway.origin, '/daemon/session', {
      method: 'POST', body: {}, omitOrigin: true, headers: { 'x-hyprial-view': String(opened.session.view) },
    });
    expect(write.status).toBe(403);
    expect(write.json).toMatchObject({ code: 'ORIGIN_REQUIRED' });
  });

  it('consumes one launch grant, rejects expiry, and never returns the upstream bearer', async () => {
    const target = await upstream();
    const launchDirectory = mkdtempSync(join(tmpdir(), 'gui-launch-'));
    cleanups.push(() => rmSync(launchDirectory, { recursive: true, force: true }));
    const handoffPath = join(launchDirectory, 'launch.url');
    const publicationPath = join(launchDirectory, 'launch.ready');
    writeFileSync(handoffPath, 'http://127.0.0.1/#grant=private', { mode: 0o600 });
    writeFileSync(publicationPath, '', { mode: 0o600 });
    const gateway = await host(config(target.endpoint, { handoffPath, publicationPath }));
    expect(existsSync(handoffPath)).toBe(true);
    const opened = await open(gateway.origin);
    expect(opened.response.status).toBe(200);
    expect(opened.response.raw).not.toContain(UPSTREAM_TOKEN);
    expect(JSON.stringify(opened.response.headers)).not.toContain(UPSTREAM_TOKEN);
    expect(opened.session).toMatchObject({ driver: 'daemon', phase: 'ready', ok: true, schemaVersion: 1 });
    conforms('sessionResponse', opened.session);
    expect(existsSync(handoffPath)).toBe(false);
    expect(existsSync(publicationPath)).toBe(false);

    const repeated = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: GRANT } });
    expect(repeated.status).toBe(409);
    expect(repeated.json).toMatchObject({ code: 'LAUNCH_GRANT_USED' });

    const expiring = await host(config(target.endpoint, { launchTtlMs: 1_000 }));
    await expiring.advance(1_001);
    const expired = await call(expiring.origin, '/daemon/session', { method: 'POST', body: { grant: GRANT } });
    expect(expired.status).toBe(401);
    expect(expired.json).toMatchObject({ code: 'LAUNCH_GRANT_EXPIRED' });

    const wrong = await call(expiring.origin, '/daemon/session', { method: 'POST', body: { grant: 'wrong-grant' } });
    expect(wrong.status).toBe(403);
    expect(wrong.json).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
  });

  it('rejects a copied business cookie without the independent view proof', async () => {
    const target = await upstream();
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    const response = await call(gateway.origin, '/daemon/readyz', {
      headers: {
        cookie: opened.cookies,
        'x-hyprial-generation': String(opened.session.generation),
      },
    });
    expect(response.status).toBe(401);
    expect(response.json).toMatchObject({ code: 'SESSION_REQUIRED', dispatch: 'not-dispatched' });
  });
});

describe('session rotation and fencing', () => {
  it('rotates generation and CSRF on refresh and rejects retired credentials', async () => {
    const target = await upstream();
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    const control = opened.cookies.split('; ').find((part) => part.startsWith('hyprial_gui_control='));
    expect(control).toBeDefined();

    const refreshed = await call(gateway.origin, '/daemon/session', {
      method: 'POST',
      body: {},
      headers: {
        cookie: control as string,
        'x-hyprial-view': String(opened.session.view),
      },
    });
    expect(refreshed.status).toBe(200);
    const next = refreshed.json as Record<string, unknown>;
    expect(next.view).toBe(opened.session.view);
    expect(next.generation).not.toBe(opened.session.generation);
    expect(next.csrf).not.toBe(opened.session.csrf);

    const retired = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: {
        cookie: opened.cookies,
        'x-hyprial-view': String(opened.session.view),
        'x-hyprial-generation': String(opened.session.generation),
      },
    });
    expect(retired.status).toBe(401);
    expect(retired.json).toMatchObject({ ok: false, dispatch: 'not-dispatched' });

    const staleCsrf = await call(gateway.origin, '/daemon/request/agent.create', {
      method: 'POST', body: { name: 'blocked' }, headers: browserHeaders(next, cookiesFrom(refreshed), String(opened.session.csrf)),
    });
    expect(staleCsrf.status).toBe(403);
    expect(staleCsrf.json).toMatchObject({ code: 'CSRF_REJECTED', dispatch: 'not-dispatched' });
    conforms('hostError', staleCsrf.json);
    conforms('sessionResponse', next);

    const readWithoutCsrf = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {},
      headers: {
        cookie: cookiesFrom(refreshed),
        'x-hyprial-view': String(next.view),
        'x-hyprial-generation': String(next.generation),
        'x-hyprial-catalog-digest': DIGEST,
      },
    });
    expect(readWithoutCsrf.status).toBe(200);
    expect(target.captured.filter((entry) => entry.url?.startsWith('/daemon/request/agent.create'))).toHaveLength(0);
  });

  it('expires and revokes sessions', async () => {
    const target = await upstream();
    const expiring = await host(config(target.endpoint, { sessionTtlMs: 1_000 }));
    const opened = await open(expiring.origin);
    await expiring.advance(1_001);
    const expired = await call(expiring.origin, '/daemon/readyz', { headers: browserHeaders(opened.session, opened.cookies) });
    expect(expired.status).toBe(401);
    expect(expired.json).toMatchObject({ code: 'SESSION_REQUIRED' });

    const revoking = await host(config(target.endpoint));
    const live = await open(revoking.origin);
    const logout = await call(revoking.origin, '/daemon/session/logout', {
      method: 'POST', body: {}, headers: browserHeaders(live.session, live.cookies),
    });
    expect(logout.status).toBe(200);
    expect(logout.json).toMatchObject({ ok: true, revoked: true });
    conforms('logoutResponse', logout.json);
    const after = await call(revoking.origin, '/daemon/readyz', { headers: browserHeaders(live.session, live.cookies) });
    expect(after.status).toBe(401);
  });
});

describe('developer tools session attach', () => {
  it('exists only with the host argument, returns the live session and rotates nothing', async () => {
    const target = await upstream();
    const plain = await host(config(target.endpoint));
    const plainSession = await open(plain.origin);
    expect(plainSession.session.devTools).toBeUndefined();
    const absent = await call(plain.origin, '/daemon/session/attach', {
      method: 'POST', body: {}, headers: { cookie: plainSession.cookies, 'x-hyprial-view': String(plainSession.session.view) },
    });
    expect(absent.status).toBe(404);
    expect(absent.json).toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched' });
    conforms('hostError', absent.json);

    const gateway = await host(config(target.endpoint, { devTools: true }));
    const opened = await open(gateway.origin);
    conforms('sessionResponse', opened.session);
    expect(opened.session.devTools).toEqual({ apiDocs: false });
    const proof = { cookie: opened.cookies, 'x-hyprial-view': String(opened.session.view) };
    const attached = await call(gateway.origin, '/daemon/session/attach', { method: 'POST', body: {}, headers: proof });
    expect(attached.status).toBe(200);
    conforms('attachResponse', attached.json);
    expect(attached.json).toMatchObject({
      generation: opened.session.generation, csrf: opened.session.csrf, expiresAt: opened.session.expiresAt,
    });
    expect(attached.headers['set-cookie']).toBeUndefined();
    expect(attached.raw).not.toContain(UPSTREAM_TOKEN);

    const again = await call(gateway.origin, '/daemon/session/attach', { method: 'POST', body: {}, headers: proof });
    expect(again.json?.generation).toBe(opened.session.generation);
    const stillLive = await call(gateway.origin, '/daemon/request/agent.create', {
      method: 'POST', body: { name: 'after-attach' }, headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(stillLive.status).toBe(200);

    const withoutView = await call(gateway.origin, '/daemon/session/attach', {
      method: 'POST', body: {}, headers: { cookie: opened.cookies },
    });
    expect(withoutView.status).toBe(401);
    expect(withoutView.json).toMatchObject({ code: 'SESSION_REQUIRED' });
    const withoutOrigin = await call(gateway.origin, '/daemon/session/attach', {
      method: 'POST', body: {}, headers: proof, omitOrigin: true,
    });
    expect(withoutOrigin.status).toBe(403);
  }, HOST_TEST_TIMEOUT_MS);
});

describe('logout CSRF', () => {
  it('refuses logout without the CSRF value or with a retired one and keeps the session live', async () => {
    const target = await upstream();
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    const control = opened.cookies.split('; ').find((part) => part.startsWith('hyprial_gui_control=')) as string;
    const refreshed = await call(gateway.origin, '/daemon/session', {
      method: 'POST', body: {}, headers: { cookie: control, 'x-hyprial-view': String(opened.session.view) },
    });
    const next = refreshed.json as Record<string, unknown>;
    const headers = browserHeaders(next, cookiesFrom(refreshed));
    const withoutCsrf = { ...headers };
    delete withoutCsrf['x-hyprial-csrf'];
    const attempts: Array<[string, Record<string, string>]> = [
      ['missing', withoutCsrf],
      ['empty', { ...headers, 'x-hyprial-csrf': '' }],
      ['retired', { ...headers, 'x-hyprial-csrf': String(opened.session.csrf) }],
    ];
    for (const [name, attempt] of attempts) {
      const refused = await call(gateway.origin, '/daemon/session/logout', { method: 'POST', body: {}, headers: attempt });
      expect(refused.status, name).toBe(403);
      expect(refused.json, name).toMatchObject({ ok: false, code: 'CSRF_REJECTED', dispatch: 'not-dispatched' });
      expect('method' in (refused.json ?? {}), name).toBe(false);
      expect(refused.headers['set-cookie'], name).toBeUndefined();
      conforms('hostError', refused.json);
    }
    const status = await call(gateway.origin, '/daemon/session/status', { method: 'POST', body: {}, headers });
    expect(status.json?.current).toBe(true);
    const accepted = await call(gateway.origin, '/daemon/session/logout', { method: 'POST', body: {}, headers });
    expect(accepted.status).toBe(200);
    expect(accepted.json).toMatchObject({ ok: true, revoked: true });
  });
});

describe('upstream readiness, identity and catalog pin', () => {
  it('refuses not-ready, auth-rejected and identity/catalog mismatches before minting', async () => {
    const cases: Array<{
      readonly payload: { readyPayload?: Record<string, unknown>; readyStatus?: number; catalogPayload?: Record<string, unknown> };
      readonly status: number;
      readonly code: string;
    }> = [
      { payload: { readyStatus: 503, readyPayload: { ok: false, code: 'DAEMON_RESTORING' } }, status: 503, code: 'HTTP_NOT_READY' },
      { payload: { readyStatus: 401, readyPayload: { ok: false } }, status: 502, code: 'UPSTREAM_AUTH_REJECTED' },
      { payload: { readyPayload: ready({ backend: { available: true, owner: 'other', nodeId: PRINCIPAL.node, epoch: EPOCH } }) }, status: 502, code: 'PRINCIPAL_MISMATCH' },
      { payload: { readyPayload: ready({ backend: { available: true, owner: PRINCIPAL.owner, nodeId: 'other', epoch: EPOCH } }) }, status: 502, code: 'PRINCIPAL_MISMATCH' },
      { payload: { catalogPayload: catalog({ principal: { owner: PRINCIPAL.owner, actor: 'agent:owner:node:other' } }) }, status: 502, code: 'PRINCIPAL_MISMATCH' },
      { payload: { catalogPayload: catalog({ backend: { kind: 'daemon', owner: PRINCIPAL.owner, nodeId: PRINCIPAL.node, epoch: 'other-epoch' } }) }, status: 502, code: 'UPSTREAM_CONTRACT_MISMATCH' },
      { payload: { catalogPayload: catalog({ catalogDigest: 'sha256:other' }) }, status: 502, code: 'CATALOG_MISMATCH' },
    ];
    for (const item of cases) {
      const target = await upstream(item.payload);
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      expect(opened.response.status).toBe(item.status);
      expect(opened.response.json).toMatchObject({ ok: false, code: item.code, dispatch: 'not-dispatched' });
    }
  });

  it('rejects a ready projection whose catalog is not explicitly fresh', async () => {
    const cases = [
      ready({ catalog: { fresh: false, catalogDigest: DIGEST, declarationDigest: DECLARATION } }),
      ready({ catalog: { catalogDigest: DIGEST, declarationDigest: DECLARATION } }),
    ];
    for (const readyPayload of cases) {
      const target = await upstream({ readyPayload });
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      expect(opened.response.status).toBe(503);
      expect(opened.response.json).toMatchObject({
        ok: false,
        code: 'HTTP_NOT_READY',
        dispatch: 'not-dispatched',
      });
    }
  });

  it('relays the authoritative restoring refusal without inventing a daemon phase', async () => {
    const target = await upstream({
      readyStatus: 503,
      readyPayload: {
        schemaVersion: 1,
        ok: false,
        ready: false,
        transport: 'ready',
        code: 'DAEMON_RESTORING',
        reason: 'RESTORE_IN_PROGRESS',
        requestId: 'upstream-ready-restoring',
      },
    });
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    expect(opened.response.status).toBe(503);
    expect(opened.response.json).toMatchObject({
      ok: false,
      code: 'HTTP_NOT_READY',
      dispatch: 'not-dispatched',
      data: { upstreamCode: 'DAEMON_RESTORING' },
    });
    expect(opened.session.phase).toBeUndefined();
  });

  it('probes the real Contract Mock metadata shape instead of inventing readiness', async () => {
    const methods: string[] = [];
    const endpoint = await listen(async (request, response) => {
      if (request.url === '/healthz') return sendJson(response, 200, { ok: true });
      if (request.url === '/mock/contract') {
        return sendJson(response, 200, {
          mock: true,
          contractVersion: '2',
          contractDigest: 'sha256:mock-contract',
          methods: {
            'agent.list': { scenarios: ['success'], mutates: false },
            'agent.create': { scenarios: ['success'], mutates: true },
          },
        });
      }
      if (request.url?.startsWith('/daemon/request/')) {
        const method = request.url.slice('/daemon/request/'.length);
        methods.push(method);
        return sendJson(response, 200, methodSuccess(method));
      }
      return sendJson(response, 404, { ok: false });
    });
    const gateway = await host(config(endpoint, { driver: 'contract-mock' }));
    const opened = await open(gateway.origin);
    expect(opened.response.status).toBe(200);
    expect(opened.session).toMatchObject({
      driver: 'contract-mock',
      catalog: { digest: 'sha256:mock-contract', catalogVersion: '2' },
    });

    const catalogResponse = await call(gateway.origin, '/daemon/request', {
      headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(catalogResponse.status).toBe(200);
    expect(catalogResponse.json).toMatchObject({ catalogVersion: '2' });
    expect(catalogResponse.json?.methods).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'agent.list', mutates: false }),
      expect.objectContaining({ name: 'agent.create', mutates: true }),
    ]));

    const readHeaders = browserHeaders(opened.session, opened.cookies);
    delete readHeaders['x-hyprial-csrf'];
    const read = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: readHeaders,
    });
    expect(read.status).toBe(200);
    expect(methods).toContain('agent.list');

    const write = await call(gateway.origin, '/daemon/request/agent.create', {
      method: 'POST', body: { name: 'x' }, headers: readHeaders,
    });
    expect(write.status).toBe(403);
    expect(write.json).toMatchObject({ code: 'CSRF_REJECTED' });
    expect(methods).not.toContain('agent.create');
  });
});

describe('method relay and dispatch receipts', () => {
  it('admits only callable catalog methods with the pinned digest', async () => {
    let observed: IncomingMessage['headers'] | null = null;
    let dispatches = 0;
    const target = await upstream({
      catalogPayload: catalog({
        methods: [
          { name: 'agent.list', path: '/daemon/request/agent.list', state: 'available', callable: true, mutates: false },
          { name: 'agent.create', path: '/daemon/request/agent.create', state: 'available', callable: true, mutates: true },
          { name: 'hosts', path: '/daemon/request/hosts', state: 'blocked', callable: false, mutates: false },
        ],
      }),
      method: (request) => {
        dispatches += 1;
        observed = request.headers;
        return { status: 200, payload: methodSuccess('agent.list') };
      },
    });
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);

    const getWrite = await call(gateway.origin, '/daemon/request/agent.create', {
      method: 'GET', headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(getWrite.status).toBe(405);
    expect(getWrite.json).toMatchObject({ code: 'HTTP_METHOD_NOT_ALLOWED', dispatch: 'not-dispatched' });

    const unknown = await call(gateway.origin, '/daemon/request/not.registered', {
      method: 'POST', body: {}, headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(unknown.status).toBe(404);
    expect(unknown.json).toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched' });

    const blocked = await call(gateway.origin, '/daemon/request/hosts', {
      method: 'POST', body: {}, headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(blocked.status).toBe(409);
    expect(blocked.json).toMatchObject({ code: 'METHOD_NOT_CALLABLE', dispatch: 'not-dispatched' });

    const driftHeaders = browserHeaders(opened.session, opened.cookies);
    driftHeaders['x-hyprial-catalog-digest'] = 'sha256:other';
    const drift = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: driftHeaders,
    });
    expect(drift.status).toBe(409);
    expect(drift.json).toMatchObject({ code: 'CATALOG_MISMATCH', dispatch: 'not-dispatched' });
    expect(dispatches).toBe(0);

    const pinnedHeaders = browserHeaders(opened.session, opened.cookies);
    delete pinnedHeaders['x-hyprial-catalog-digest'];
    const pinned = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: pinnedHeaders,
    });
    expect(pinned.status).toBe(200);
    expect(observed).toMatchObject({ 'x-hyprial-catalog-digest': DIGEST });
    expect(dispatches).toBe(1);
  });

  it('preserves query, request id, catalog pin, bearer custody and upstream receipts', async () => {
    let observed: Record<string, unknown> | null = null;
    const target = await upstream({
      method: (request, body) => {
        observed = { url: request.url, headers: request.headers, body };
        return {
          status: 202,
          payload: methodSuccess('agent.list', 'upstream-request-42'),
          headers: {
            'x-hyprial-request-id': 'header-request',
            'x-hyprial-catalog-digest': DIGEST,
            'x-hyprial-dispatch': 'dispatched',
          },
        };
      },
    });
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    const response = await call(gateway.origin, '/daemon/request/agent.list?view=inbox&size=2', {
      method: 'POST',
      body: { query: 'ignored-body-value' },
      headers: {
        ...browserHeaders(opened.session, opened.cookies),
        'x-hyprial-request-id': 'browser-request-id',
        'x-hyprial-catalog-digest': DIGEST,
        cookie: `${opened.cookies}; hostile=value`,
      },
    });
    expect(response.status).toBe(202);
    expect(response.headers['x-hyprial-request-id']).toBe('upstream-request-42');
    expect(response.headers['x-hyprial-catalog-digest']).toBe(DIGEST);
    expect(response.headers['x-hyprial-dispatch']).toBe('dispatched');
    expect(observed).toMatchObject({
      url: '/daemon/request/agent.list?view=inbox&size=2',
      headers: {
        authorization: `Bearer ${UPSTREAM_TOKEN}`,
        'x-hyprial-request-id': 'browser-request-id',
        'x-hyprial-catalog-digest': DIGEST,
      },
    });
    expect(JSON.stringify(observed)).not.toContain('hyprial_gui_session');
    expect(JSON.stringify(observed)).not.toContain('hyprial_gui_control');
  });

  it('rejects malformed method bodies before dispatch and forwards a host request id', async () => {
    let dispatches = 0;
    let forwardedRequestId: string | undefined;
    const target = await upstream({
      method: (request) => {
        dispatches += 1;
        const requestId = request.headers['x-hyprial-request-id'];
        forwardedRequestId = Array.isArray(requestId) ? requestId[0] : requestId;
        return { status: 200, payload: methodSuccess('agent.list') };
      },
    });
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    const headers = browserHeaders(opened.session, opened.cookies);

    const arrayBody = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: [], headers,
    });
    expect(arrayBody.status).toBe(400);
    expect(arrayBody.json).toMatchObject({ code: 'INVALID_REQUEST', dispatch: 'not-dispatched' });

    const scalarBody = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: 'not-an-object', headers,
    });
    expect(scalarBody.status).toBe(400);
    expect(scalarBody.json).toMatchObject({ code: 'INVALID_REQUEST', dispatch: 'not-dispatched' });

    const mediaType = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: { ...headers, 'content-type': 'text/plain' },
    });
    expect(mediaType.status).toBe(415);
    expect(mediaType.json).toMatchObject({ code: 'HTTP_UNSUPPORTED_MEDIA_TYPE', dispatch: 'not-dispatched' });

    const oversized = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: 'x'.repeat(MAX_REQUEST_BYTES), headers,
    });
    expect(oversized.status).toBe(413);
    expect(oversized.json).toMatchObject({ code: 'HTTP_REQUEST_TOO_LARGE', dispatch: 'not-dispatched' });
    expect(dispatches).toBe(0);

    const accepted = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers,
    });
    expect(accepted.status).toBe(200);
    expect(forwardedRequestId).toMatch(/^gui-/);
    expect(accepted.headers['x-hyprial-request-id']).toBe('upstream-request');
  });

  it('relays a missing Contract Mock scenario as a truthful 409 receipt', async () => {
    const missing = {
      schemaVersion: 1,
      ok: false,
      method: 'agent.list',
      requestId: 'mock-request',
      code: 'MOCK_SCENARIO_MISSING',
      message: 'agent.list has no empty example',
      dispatch: 'not-dispatched',
    };
    const target = await upstream({ method: () => ({ status: 409, payload: missing }) });
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    const response = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(response.status).toBe(409);
    expect(response.json).toEqual(missing);
    expect(response.headers['x-hyprial-request-id']).toBe('mock-request');
  });

  it('rejects invalid or oversized upstream method envelopes', async () => {
    const invalid = await upstream({ method: () => ({ status: 200, raw: '{"schemaVersion":1,"ok":true}' }) });
    const invalidGateway = await host(config(invalid.endpoint));
    const invalidOpen = await open(invalidGateway.origin);
    const invalidResponse = await call(invalidGateway.origin, '/daemon/request/agent.create', {
      method: 'POST', body: { name: 'x' }, headers: browserHeaders(invalidOpen.session, invalidOpen.cookies),
    });
    expect(invalidResponse.status).toBe(502);
    expect(invalidResponse.json).toMatchObject({ code: 'UPSTREAM_INVALID_RESPONSE', dispatch: 'unknown' });

    const oversized = await upstream({
      method: () => ({ status: 200, raw: 'x'.repeat(MAX_RESPONSE_BYTES + 1) }),
    });
    const oversizedGateway = await host(config(oversized.endpoint));
    const oversizedOpen = await open(oversizedGateway.origin);
    const oversizedResponse = await call(oversizedGateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: browserHeaders(oversizedOpen.session, oversizedOpen.cookies),
    });
    expect(oversizedResponse.status).toBe(502);
    expect(oversizedResponse.json).toMatchObject({ code: 'UPSTREAM_RESPONSE_TOO_LARGE', dispatch: 'not-dispatched' });
  });

  it('classifies transport loss without retrying and preserves write uncertainty', async () => {
    const target = await upstream({ method: () => ({ status: 200, destroy: true }) });
    const gateway = await host(config(target.endpoint));
    const opened = await open(gateway.origin);
    expect(target.captured).toHaveLength(2);

    const write = await call(gateway.origin, '/daemon/request/agent.create', {
      method: 'POST', body: { name: 'x' }, headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(write.status).toBe(502);
    expect(write.json).toMatchObject({ ok: false, code: 'UPSTREAM_UNAVAILABLE', dispatch: 'unknown' });

    const read = await call(gateway.origin, '/daemon/request/agent.list', {
      method: 'POST', body: {}, headers: browserHeaders(opened.session, opened.cookies),
    });
    expect(read.status).toBe(502);
    expect(read.json).toMatchObject({ ok: false, code: 'UPSTREAM_UNAVAILABLE', dispatch: 'not-dispatched' });
    expect(target.captured).toHaveLength(4);
    expect(target.captured.map((entry) => entry.url)).toEqual([
      '/readyz', '/daemon/request', '/daemon/request/agent.create', '/daemon/request/agent.list',
    ]);
  });
});
