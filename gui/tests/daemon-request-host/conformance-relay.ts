/**
 * Decision record (GUI session contract v1 conformance, 2026-11-15)
- These cases pin README sections 5.1, 6, 7 and 10 black-box: the per-route
  check order, relay header precedence, the method envelope checks, query
  handling, the upstream probe's acceptance criteria and the Contract Mock
  driver's synthesised bodies.
- Upstream stubs are mutable so one opened session can be relayed through
  several upstream answers; every assertion on a refusal also asserts that the
  refused call never reached the upstream.
- The Contract Mock driver refuses to start without a serving principal, and
  the mutates cases assert the wire boolean, otherwise true. The Node host's
  deviations from both were removed with it (2026-11-16); the suite now states
  only the contract.
 */

import { describe, expect, it } from 'vitest';
import {
  ANONYMOUS, DECLARATION, DIGEST, EPOCH, PRINCIPAL, browserHeaders, call, catalog, config, conforms, host, methodSuccess,
  mockUpstream, open, ready, upstream, type Mutable, type UpstreamOptions,
} from './host-harness';


export function registerRelayConformance(): void {
  const METHODS = [
    { name: 'agent.list', path: '/daemon/request/agent.list', state: 'available', callable: true, mutates: false },
    { name: 'agent.create', path: '/daemon/request/agent.create', state: 'available', callable: true, mutates: true },
    { name: 'hosts', path: '/daemon/request/hosts', state: 'blocked', callable: false, mutates: false },
  ];

  function methodCalls(target: { captured: Array<{ url?: string }> }): string[] {
    return target.captured.map((entry) => entry.url ?? '').filter((url) => url.startsWith('/daemon/request/'));
  }

  describe('method route check order (README section 5.1)', () => {
    it('authenticates before it reads the body and reports the first failing check', async () => {
      const target = await upstream({ catalogPayload: catalog({ methods: METHODS }) });
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const good = browserHeaders(opened.session, opened.cookies);
      const post = (name: string, headers: Record<string, string>, body: unknown = {}) => call(
        gateway.origin, `/daemon/request/${name}`, { method: 'POST', body, headers },
      );
      const without = (name: string) => { const copy = { ...good }; delete copy[name]; return copy; };

      const get = await call(gateway.origin, '/daemon/request/agent.create', { method: 'GET' });
      expect(get.status).toBe(405);
      expect(get.json).toMatchObject({ code: 'HTTP_METHOD_NOT_ALLOWED', method: 'agent.create' });

      const unauthenticated = await call(gateway.origin, '/daemon/request/agent.list', {
        method: 'POST', body: {}, headers: { 'content-type': 'text/plain' },
      });
      expect(unauthenticated.status).toBe(401);
      expect(unauthenticated.json).toMatchObject({ code: 'SESSION_REQUIRED', method: 'agent.list', dispatch: 'not-dispatched' });

      const wrongViewAndGeneration = await post('agent.list', { ...good, 'x-hyprial-view': 'wrong', 'x-hyprial-generation': 'old' });
      expect(wrongViewAndGeneration.status).toBe(401);
      const retired = await post('agent.list', { ...good, 'x-hyprial-generation': 'old' });
      expect(retired.status).toBe(403);
      expect(retired.json).toMatchObject({ code: 'GENERATION_REJECTED', method: 'agent.list' });

      const wrongDigest = { ...without('x-hyprial-csrf'), 'x-hyprial-catalog-digest': 'sha256:other' };
      const unknown = await post('not.registered', wrongDigest);
      expect(unknown.status).toBe(404);
      expect(unknown.json).toMatchObject({ code: 'METHOD_NOT_FOUND', method: 'not.registered' });
      const blocked = await post('hosts', wrongDigest);
      expect(blocked.status).toBe(409);
      expect(blocked.json).toMatchObject({ code: 'METHOD_NOT_CALLABLE', method: 'hosts' });
      const digestBeforeCsrf = await post('agent.create', wrongDigest);
      expect(digestBeforeCsrf.status).toBe(409);
      expect(digestBeforeCsrf.json).toMatchObject({ code: 'CATALOG_MISMATCH', method: 'agent.create' });

      const textBody = { ...without('x-hyprial-csrf'), 'content-type': 'text/plain' };
      const csrfBeforeBody = await post('agent.create', textBody);
      expect(csrfBeforeBody.status).toBe(403);
      expect(csrfBeforeBody.json).toMatchObject({ code: 'CSRF_REJECTED', method: 'agent.create' });
      const emptyCsrf = await post('agent.create', { ...good, 'x-hyprial-csrf': '' });
      expect(emptyCsrf.json).toMatchObject({ code: 'CSRF_REJECTED' });
      const media = await post('agent.list', { ...without('x-hyprial-csrf'), 'content-type': 'text/plain' });
      expect(media.status).toBe(415);
      expect(media.json).toMatchObject({ code: 'HTTP_UNSUPPORTED_MEDIA_TYPE', method: 'agent.list' });
      const emptyDigest = await post('agent.list', { ...without('x-hyprial-csrf'), 'x-hyprial-catalog-digest': '' });
      expect(emptyDigest.status).toBe(200);
      expect(methodCalls(target)).toEqual(['/daemon/request/agent.list']);

      const ignoredCsrf = await post('agent.list', { ...good, 'x-hyprial-csrf': 'not-the-csrf' });
      expect(ignoredCsrf.status).toBe(200);
    });
  });

  describe('relay rules (README section 6)', () => {
    it('prefers the upstream digest header, then body.catalogDigest, then the pin, on every relayed route', async () => {
      const options: Mutable<UpstreamOptions> = {};
      const target = await upstream(options);
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const headers = browserHeaders(opened.session, opened.cookies);
      const digestOf = async () => (await call(gateway.origin, '/daemon/readyz', { headers })).headers['x-hyprial-catalog-digest'];

      expect(await digestOf()).toBe(DIGEST);
      options.readyPayload = ready({ catalogDigest: 'sha256:from-body' });
      expect(await digestOf()).toBe('sha256:from-body');
      options.readyHeaders = { 'x-hyprial-catalog-digest': 'sha256:from-header' };
      expect(await digestOf()).toBe('sha256:from-header');

      const method = await upstream({ method: () => ({
        status: 200,
        payload: { ...methodSuccess('agent.list'), catalogDigest: 'sha256:body-digest' },
        headers: { 'x-hyprial-catalog-digest': 'sha256:header-digest' },
      }) });
      const methodGateway = await host(config(method.endpoint));
      const methodOpened = await open(methodGateway.origin);
      const answered = await call(methodGateway.origin, '/daemon/request/agent.list', {
        method: 'POST', body: {}, headers: browserHeaders(methodOpened.session, methodOpened.cookies),
      });
      expect(answered.headers['x-hyprial-catalog-digest']).toBe('sha256:header-digest');
      const bodyOnly = await upstream({ method: () => ({
        status: 200, payload: { ...methodSuccess('agent.list'), catalogDigest: 'sha256:body-digest' },
      }) });
      const bodyGateway = await host(config(bodyOnly.endpoint));
      const bodyOpened = await open(bodyGateway.origin);
      const fromBody = await call(bodyGateway.origin, '/daemon/request/agent.list', {
        method: 'POST', body: {}, headers: browserHeaders(bodyOpened.session, bodyOpened.cookies),
      });
      expect(fromBody.headers['x-hyprial-catalog-digest']).toBe('sha256:body-digest');
    });

    it('prefers the upstream body requestId, then its header, then no header; dispatch header wins over the body', async () => {
      const options: Mutable<UpstreamOptions> = {};
      const target = await upstream(options);
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const headers = browserHeaders(opened.session, opened.cookies);
      const idOf = async () => (await call(gateway.origin, '/daemon/readyz', { headers })).headers['x-hyprial-request-id'];

      expect(await idOf()).toBeUndefined();
      options.readyHeaders = { 'x-hyprial-request-id': 'header-id' };
      expect(await idOf()).toBe('header-id');
      options.readyPayload = ready({ requestId: 'body-id' });
      expect(await idOf()).toBe('body-id');

      const answers = [
        { headers: { 'x-hyprial-dispatch': 'unknown' } as Record<string, string>, expected: 'unknown' },
        { headers: {} as Record<string, string>, expected: 'dispatched' },
      ];
      for (const answer of answers) {
        const method = await upstream({ method: () => ({ status: 200, payload: methodSuccess('agent.list'), headers: answer.headers }) });
        const methodGateway = await host(config(method.endpoint));
        const methodOpened = await open(methodGateway.origin);
        const response = await call(methodGateway.origin, '/daemon/request/agent.list', {
          method: 'POST', body: {}, headers: browserHeaders(methodOpened.session, methodOpened.cookies),
        });
        expect(response.headers['x-hyprial-dispatch']).toBe(answer.expected);
      }
      expect((await call(gateway.origin, '/daemon/readyz', { headers })).headers['x-hyprial-dispatch']).toBeUndefined();
    });

    it('relays any upstream status and body, drops every other upstream header and sets Content-Length', async () => {
      const target = await upstream({ method: () => ({
        status: 500,
        payload: { ...methodSuccess('agent.list'), ok: false, code: 'DAEMON_FAILED' },
        headers: { 'set-cookie': 'evil=1', 'x-evil': '1', 'x-powered-by': 'upstream', 'cache-control': 'public' },
      }) });
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const response = await call(gateway.origin, '/daemon/request/agent.list', {
        method: 'POST', body: {}, headers: browserHeaders(opened.session, opened.cookies),
      });
      expect(response.status).toBe(500);
      expect(response.json).toMatchObject({ ok: false, code: 'DAEMON_FAILED' });
      for (const name of ['set-cookie', 'x-evil', 'x-powered-by']) expect(response.headers[name]).toBeUndefined();
      expect(response.headers['cache-control']).toBe('no-store');
      expect(response.headers['content-type']).toBe('application/json; charset=utf-8');
      expect(response.headers['x-content-type-options']).toBe('nosniff');
      expect(response.headers['content-length']).toBe(String(Buffer.byteLength(response.raw)));
    });

    it('refuses a relayed method envelope that is not an envelope for this method', async () => {
      const variants: Array<[string, (name: string) => unknown]> = [
        ['schemaVersion', (name) => ({ ...methodSuccess(name), schemaVersion: 2 })],
        ['ok', (name) => ({ ...methodSuccess(name), ok: 'yes' })],
        ['empty requestId', (name) => ({ ...methodSuccess(name), requestId: '' })],
        ['no requestId', (name) => ({ ...methodSuccess(name), requestId: undefined })],
        ['dispatch', (name) => ({ ...methodSuccess(name), dispatch: 'maybe' })],
        ['method', () => methodSuccess('other.method')],
        ['no method', (name) => ({ ...methodSuccess(name), method: undefined })],
        ['array', (name) => [methodSuccess(name)]],
      ];
      for (const [name, build] of variants) {
        const target = await upstream({ method: (request) => ({
          status: 200, payload: build(String(request.url?.slice('/daemon/request/'.length))),
        }) });
        const gateway = await host(config(target.endpoint));
        const opened = await open(gateway.origin);
        const headers = browserHeaders(opened.session, opened.cookies);
        const write = await call(gateway.origin, '/daemon/request/agent.create', { method: 'POST', body: {}, headers });
        expect(write.status, name).toBe(502);
        expect(write.json, name).toMatchObject({ code: 'UPSTREAM_INVALID_RESPONSE', dispatch: 'unknown', method: 'agent.create' });
        const read = await call(gateway.origin, '/daemon/request/agent.list', { method: 'POST', body: {}, headers });
        expect(read.status, name).toBe(502);
        expect(read.json, name).toMatchObject({ code: 'UPSTREAM_INVALID_RESPONSE', dispatch: 'not-dispatched', method: 'agent.list' });
      }
      const notJson = await upstream({ method: () => ({ status: 200, raw: 'not json' }) });
      const notJsonGateway = await host(config(notJson.endpoint));
      const notJsonOpened = await open(notJsonGateway.origin);
      const refused = await call(notJsonGateway.origin, '/daemon/request/agent.create', {
        method: 'POST', body: {}, headers: browserHeaders(notJsonOpened.session, notJsonOpened.cookies),
      });
      expect(refused.json).toMatchObject({ code: 'UPSTREAM_INVALID_RESPONSE', dispatch: 'unknown' });
    });

    it('refuses a readiness or catalog relay that is not a JSON object, without checking it further', async () => {
      const options: Mutable<UpstreamOptions> = {};
      const target = await upstream(options);
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const headers = browserHeaders(opened.session, opened.cookies);
      options.readyRaw = 'plain text';
      const refused = await call(gateway.origin, '/daemon/readyz', { headers });
      expect(refused.status).toBe(502);
      expect(refused.json).toMatchObject({ code: 'UPSTREAM_INVALID_RESPONSE', dispatch: 'not-dispatched' });
      expect('method' in (refused.json ?? {})).toBe(false);
      options.readyRaw = undefined;
      options.readyStatus = 418;
      options.readyPayload = { anything: 'goes' };
      const relayed = await call(gateway.origin, '/daemon/readyz', { headers });
      expect(relayed.status).toBe(418);
      expect(relayed.json).toEqual({ anything: 'goes' });
    });

    it('forwards the query of the catalog and method routes, drops it for readyz, ignores it on session routes', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const headers = browserHeaders(opened.session, opened.cookies);
      await call(gateway.origin, '/daemon/readyz?a=1&b=2', { headers });
      await call(gateway.origin, '/daemon/request?view=inbox&size=2', { headers });
      await call(gateway.origin, '/daemon/request/agent.list?x=%20y', { method: 'POST', body: {}, headers });
      const status = await call(gateway.origin, '/daemon/session/status?x=1', { method: 'POST', body: {}, headers });
      expect(status.json?.current).toBe(true);
      expect(target.captured.slice(2).map((entry) => entry.url)).toEqual([
        '/readyz', '/daemon/request?view=inbox&size=2', '/daemon/request/agent.list?x=%20y',
      ]);
    });
  });

  describe('the daemon probe (README section 10)', () => {
    it('maps upstream refusals as specified and never mints on a refusal', async () => {
      const cases: Array<[string, UpstreamOptions, number, string, unknown?]> = [
        ['other status with code', { readyStatus: 500, readyPayload: { code: 'WEIRD' } }, 502, 'WEIRD', 'WEIRD'],
        ['other status without code', { readyStatus: 500, readyPayload: {} }, 502, 'UPSTREAM_NOT_READY', 'UPSTREAM_NOT_READY'],
        ['503 without code', { readyStatus: 503, readyPayload: {} }, 503, 'HTTP_NOT_READY', 'UPSTREAM_NOT_READY'],
        ['403', { readyStatus: 403, readyPayload: { code: 'X' } }, 502, 'UPSTREAM_AUTH_REJECTED'],
        ['non-JSON 401', { readyStatus: 401, readyRaw: 'denied' }, 502, 'UPSTREAM_INVALID_RESPONSE'],
        ['non-JSON 200', { readyRaw: 'plain' }, 502, 'UPSTREAM_INVALID_RESPONSE'],
        ['catalog status', { catalogStatus: 500, catalogPayload: { code: 'CATALOG_DOWN' } }, 502, 'CATALOG_DOWN', 'CATALOG_DOWN'],
        ['not ok', { readyPayload: ready({ ok: false }) }, 503, 'HTTP_NOT_READY'],
        ['transport not ready', { readyPayload: ready({ transport: 'starting' }) }, 503, 'HTTP_NOT_READY'],
        ['backend unavailable', { readyPayload: ready({ backend: { available: false, owner: PRINCIPAL.owner, nodeId: PRINCIPAL.node, epoch: EPOCH } }) }, 503, 'HTTP_NOT_READY'],
        ['methods not ready', { readyPayload: ready({ notReady: ['agent.list'] }) }, 503, 'HTTP_NOT_READY'],
        ['no backend', { readyPayload: ready({ backend: undefined }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['owner key absent', { readyPayload: ready({ backend: { available: true, nodeId: PRINCIPAL.node, epoch: EPOCH } }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['node missing', { readyPayload: ready({ backend: { available: true, owner: PRINCIPAL.owner, epoch: EPOCH } }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['catalog not ok', { catalogPayload: catalog({ ok: false }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['catalog not the daemon catalog', { catalogPayload: catalog({ generatedFrom: 'contract-mock' }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['catalog schema', { catalogPayload: catalog({ schemaVersion: 2 }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['catalog principal owner absent', { catalogPayload: catalog({ principal: { actor: PRINCIPAL.actor } }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['catalog owner differs', { catalogPayload: catalog({ principal: { owner: 'other', actor: PRINCIPAL.actor } }) }, 502, 'PRINCIPAL_MISMATCH'],
        ['no method list', { catalogPayload: catalog({ methods: undefined }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['incomplete method', { catalogPayload: catalog({ methods: [{ name: 'agent.list', callable: true, state: 'available' }] }) }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['digest header differs from body', { catalogHeaders: { 'x-hyprial-catalog-digest': 'sha256:header' } }, 502, 'CATALOG_MISMATCH'],
        ['declaration differs', { catalogPayload: catalog({ declarationDigest: 'sha256:other' }) }, 502, 'CATALOG_MISMATCH'],
        ['digest absent', { catalogPayload: catalog({ catalogDigest: undefined }), catalogHeaders: { 'x-hyprial-catalog-digest': '' } }, 502, 'CATALOG_MISMATCH'],
      ];
      for (const [name, options, status, code, upstreamCode] of cases) {
        const target = await upstream(options);
        const gateway = await host(config(target.endpoint));
        const opened = await open(gateway.origin);
        expect(opened.response.status, name).toBe(status);
        expect(opened.response.json, name).toMatchObject({ ok: false, code, dispatch: 'not-dispatched' });
        expect(opened.response.headers['set-cookie'], name).toBeUndefined();
        const data = opened.response.json?.data as Record<string, unknown> | undefined;
        if (upstreamCode === undefined) expect(data, name).toBeUndefined();
        else expect(data, name).toEqual({ upstreamCode });
        conforms('hostError', opened.response.json);
      }
    });

    it('refuses an unreachable upstream with 503 and a mismatched bootstrap or serving identity with 502', async () => {
      const closed = await host(config('http://127.0.0.1:1'));
      const unreachable = await open(closed.origin);
      expect(unreachable.response.status).toBe(503);
      expect(unreachable.response.json).toMatchObject({ code: 'UPSTREAM_UNAVAILABLE', dispatch: 'not-dispatched' });

      const ownerless = await upstream({ readyPayload: ready({ backend: { available: true, owner: null, nodeId: PRINCIPAL.node, epoch: EPOCH } }) });
      const serving = await host(config(ownerless.endpoint));
      expect((await open(serving.origin)).response.json).toMatchObject({ code: 'PRINCIPAL_MISMATCH' });
      const owned = await upstream();
      const bootstrap = await host(config(owned.endpoint, { principal: ANONYMOUS }));
      expect((await open(bootstrap.origin)).response.json).toMatchObject({ code: 'PRINCIPAL_MISMATCH' });
      const claiming = await upstream({
        readyPayload: ready({ backend: { available: true, owner: null, nodeId: PRINCIPAL.node, epoch: EPOCH } }),
        catalogPayload: catalog({
          backend: { kind: 'daemon', owner: null, nodeId: PRINCIPAL.node, epoch: EPOCH },
          principal: { owner: null, actor: PRINCIPAL.actor },
        }),
      });
      const claimed = await host(config(claiming.endpoint, { principal: ANONYMOUS }));
      expect((await open(claimed.origin)).response.json).toMatchObject({ code: 'PRINCIPAL_MISMATCH' });
    });

    it('pins the catalog it probed: digest, declarations, epoch, versions and every method', async () => {
      const target = await upstream({ catalogPayload: catalog({ httpContractVersion: 3, catalogVersion: '7', methods: METHODS }) });
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      expect(opened.session.catalog).toEqual({
        digest: DIGEST, declarationDigest: DECLARATION, backendEpoch: EPOCH, httpContractVersion: 3, catalogVersion: '7',
        methodCount: 3,
        methods: METHODS.map(({ name, callable, state, mutates }) => ({ name, callable, state, mutates })),
      });
      const defaults = await upstream({ catalogPayload: catalog({ httpContractVersion: 0, catalogVersion: '' }) });
      const defaulted = await open((await host(config(defaults.endpoint))).origin);
      expect(defaulted.session.catalog).toMatchObject({ httpContractVersion: 1, catalogVersion: '1' });
    });
  });

  describe('the Contract Mock driver (README section 10)', () => {
    it('refuses probe answers that are not the Contract Mock', async () => {
      const cases: Array<[string, Parameters<typeof mockUpstream>[1], number, string]> = [
        ['unhealthy', { health: { ok: false } }, 503, 'HTTP_NOT_READY'],
        ['not a mock', { contract: { mock: false, contractDigest: 'sha256:x', methods: {} } }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['no digest', { contract: { mock: true, methods: {} } }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['no methods', { contract: { mock: true, contractDigest: 'sha256:x' } }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
        ['method without scenarios', { contract: { mock: true, contractDigest: 'sha256:x', methods: { 'agent.list': {} } } }, 502, 'UPSTREAM_CONTRACT_MISMATCH'],
      ];
      for (const [name, overrides, status, code] of cases) {
        const target = await mockUpstream(undefined, overrides);
        const gateway = await host(config(target.endpoint, { driver: 'contract-mock' }));
        const opened = await open(gateway.origin);
        expect(opened.response.status, name).toBe(status);
        expect(opened.response.json, name).toMatchObject({ code, dispatch: 'not-dispatched' });
      }
    });

    it('serves a ready session, synthesises readiness and catalog from the pin and takes mutates from the wire and defaults a missing one to true', async () => {
      const target = await mockUpstream({
        'agent.list': { scenarios: ['success'], mutates: false },
        'agent.create': { scenarios: ['success'], mutates: true },
        'totally.unknown': { scenarios: ['success'] },
        'wire.read': { scenarios: ['success'], mutates: false },
      });
      const gateway = await host(config(target.endpoint, { driver: 'contract-mock' }));
      const opened = await open(gateway.origin);
      expect(opened.response.status).toBe(200);
      conforms('sessionResponse', opened.session);
      expect(opened.session).toMatchObject({
        driver: 'contract-mock', phase: 'ready', principal: { kind: 'serving', ...PRINCIPAL },
        catalog: {
          digest: 'sha256:mock-contract', declarationDigest: 'sha256:mock-contract',
          backendEpoch: 'contract-mock:sha256:mock-contract', httpContractVersion: 1, catalogVersion: '2',
        },
      });
      const methods = (opened.session.catalog as { methods: Array<Record<string, unknown>> }).methods;
      expect(methods.map((method) => [method.name, method.callable, method.state])).toEqual([
        ['agent.list', true, 'available'], ['agent.create', true, 'available'],
        ['totally.unknown', true, 'available'], ['wire.read', true, 'available'],
      ]);
      const mutates = Object.fromEntries(methods.map((method) => [method.name, method.mutates]));
      expect(mutates).toMatchObject({ 'agent.list': false, 'agent.create': true, 'totally.unknown': true });
      expect(mutates['wire.read']).toBe(false);

      const headers = browserHeaders(opened.session, opened.cookies);
      const readyz = await call(gateway.origin, '/daemon/readyz', { headers });
      expect(readyz.status).toBe(200);
      expect(readyz.json).toEqual({
        schemaVersion: 1, ok: true, requestId: readyz.headers['x-hyprial-request-id'],
        ready: true, transport: 'ready', driver: 'contract-mock',
        backend: { available: true, epoch: 'contract-mock:sha256:mock-contract' },
        catalog: { fresh: true, catalogDigest: 'sha256:mock-contract', declarationDigest: 'sha256:mock-contract' },
      });
      expect(readyz.headers['x-hyprial-request-id']).toMatch(/^gui-/);
      expect(readyz.headers['x-hyprial-catalog-digest']).toBe('sha256:mock-contract');

      const listing = await call(gateway.origin, '/daemon/request', { headers });
      expect(listing.status).toBe(200);
      expect(listing.json).toEqual({
        schemaVersion: 1, ok: true, requestId: listing.headers['x-hyprial-request-id'],
        httpContractVersion: 1, catalogVersion: '2', catalogDigest: 'sha256:mock-contract',
        declarationDigest: 'sha256:mock-contract', generatedFrom: 'contract-mock',
        backend: { kind: 'contract-mock', epoch: 'contract-mock:sha256:mock-contract' },
        principal: { kind: 'serving', ...PRINCIPAL },
        methods: methods.map((method) => ({
          name: method.name, path: `/daemon/request/${String(method.name)}`, state: 'available', callable: true, mutates: method.mutates,
        })),
      });
      expect(listing.headers['x-hyprial-catalog-digest']).toBe('sha256:mock-contract');
      expect(target.captured.map((entry) => entry.url)).toEqual(['/healthz', '/mock/contract']);
    });

    it('takes mutates from the wire for a method the GUI build does not know: a read needs no CSRF, a write does', async () => {
      const target = await mockUpstream({
        'identity.whoami': { scenarios: ['success'], mutates: false },
        'identity.rotate': { scenarios: ['success'], mutates: true },
      });
      const gateway = await host(config(target.endpoint, { driver: 'contract-mock' }));
      const opened = await open(gateway.origin);
      const methods = (opened.session.catalog as { methods: Array<Record<string, unknown>> }).methods;
      expect(methods.map((method) => [method.name, method.mutates])).toEqual([['identity.whoami', false], ['identity.rotate', true]]);

      const headers = browserHeaders(opened.session, opened.cookies);
      const withoutCsrf = { ...headers };
      delete withoutCsrf['x-hyprial-csrf'];
      const read = await call(gateway.origin, '/daemon/request/identity.whoami', { method: 'POST', body: {}, headers: withoutCsrf });
      expect(read.status).toBe(200);
      const write = await call(gateway.origin, '/daemon/request/identity.rotate', { method: 'POST', body: {}, headers: withoutCsrf });
      expect(write.status).toBe(403);
      expect(write.json).toMatchObject({ code: 'CSRF_REJECTED', method: 'identity.rotate', dispatch: 'not-dispatched' });
      const accepted = await call(gateway.origin, '/daemon/request/identity.rotate', { method: 'POST', body: {}, headers });
      expect(accepted.status).toBe(200);
    });

    it.each([
      ['a missing field', undefined],
      ['the string "false"', 'false'],
      ['the number 0', 0],
      ['null', null],
    ])('fails closed to mutating for %s, even for a method the GUI build declares read-only', async (_name, value) => {
      const declared = value === undefined ? { scenarios: ['success'] } : { scenarios: ['success'], mutates: value };
      const target = await mockUpstream({ 'agent.list': declared, 'identity.whoami': declared });
      const gateway = await host(config(target.endpoint, { driver: 'contract-mock' }));
      const opened = await open(gateway.origin);
      const methods = (opened.session.catalog as { methods: Array<Record<string, unknown>> }).methods;
      expect(methods.map((method) => [method.name, method.mutates])).toEqual([['agent.list', true], ['identity.whoami', true]]);
      const withoutCsrf = { ...browserHeaders(opened.session, opened.cookies) };
      delete withoutCsrf['x-hyprial-csrf'];
      for (const name of ['agent.list', 'identity.whoami']) {
        const refused = await call(gateway.origin, `/daemon/request/${name}`, { method: 'POST', body: {}, headers: withoutCsrf });
        expect(refused.status, name).toBe(403);
        expect(refused.json, name).toMatchObject({ code: 'CSRF_REJECTED', dispatch: 'not-dispatched' });
      }
    });

    it('refuses to start without a serving principal', async () => {
      const target = await mockUpstream();
      let refusal: unknown = null;
      try {
        await host(config(target.endpoint, { driver: 'contract-mock', principal: ANONYMOUS }));
      } catch (error) {
        refusal = error;
      }
      expect(refusal).not.toBeNull();
    });
  });
}
