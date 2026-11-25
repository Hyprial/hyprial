/**
 * Decision record (GUI session contract v1 conformance, 2026-11-15)
- These cases pin README sections 2, 5 and 5.1 (origin admission, request
  target, check order) and the failure shape of section 7, black-box.
- An admitted request is recognised by its first refusal further down the
  pipeline (a wrong grant is 403 LAUNCH_GRANT_INVALID), so admission cases never
  need a live session.
 */

import { describe, expect, it } from 'vitest';
import {
  call, config, host, rawCall, upstream,
} from './host-harness';


export function registerAdmissionConformance(): void {
  async function started() {
    const target = await upstream();
    const gateway = await host(config(target.endpoint));
    const port = new URL(gateway.origin).port;
    const probe = (options: { host?: string; origin?: string; headers?: Record<string, string>; omitOrigin?: boolean; method?: string }) => call(
      gateway.origin, '/daemon/session', {
        method: options.method ?? 'POST', body: { grant: 'wrong-grant' }, ...options,
      },
    );
    return { target, gateway, port, probe };
  }

  describe('listener and Host normalisation (README section 2)', () => {
    it('binds only to a loopback address', async () => {
      const { gateway } = await started();
      expect(['127.0.0.1', '::1']).toContain(gateway.listenAddress);
    });

    it('accepts a normalised loopback Host and refuses everything else with the JSON 403', async () => {
      const { gateway, port, probe, target } = await started();
      for (const [name, hostHeader, origin] of [
        ['leading zeros in the port', `127.0.0.1:0${port}`, `http://127.0.0.1:${port}`],
        ['bracketed IPv6 loopback', `[::1]:${port}`, `http://[::1]:${port}`],
        ['localhost', `localhost:${port}`, `http://localhost:${port}`],
      ] as const) {
        const admitted = await probe({ host: hostHeader, origin });
        expect(admitted.json, name).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
      }
      for (const [name, hostHeader] of [
        ['no port', '127.0.0.1'],
        ['default port', '127.0.0.1:80'],
        ['another port', `127.0.0.1:${Number(port) + 1}`],
        ['a path', `127.0.0.1:${port}/x`],
        ['a routable IP literal', `10.0.0.1:${port}`],
      ] as const) {
        const refused = await probe({ host: hostHeader, origin: `http://${hostHeader}` });
        expect(refused.status, name).toBe(403);
        expect(refused.json, name).toMatchObject({ ok: false, code: 'HTTP_HOST_DENIED', dispatch: 'not-dispatched' });
      }
      expect(gateway.origin).toContain(port);
      expect(target.captured).toHaveLength(0);
    });

    it('answers DNS-name, userinfo and unbracketed Hosts with the JSON refusal and accepts an upper-case loopback name', async () => {
      const { port, probe } = await started();
      const dns = await probe({ host: `evil.example:${port}`, origin: `http://evil.example:${port}` });
      const userinfo = await probe({ host: `user@127.0.0.1:${port}` });
      const folded = await probe({ host: `LocalHost:${port}`, origin: `http://localhost:${port}` });
      const unbracketed = await probe({ host: `::1:${port}` });
      expect(dns.json).toMatchObject({ code: 'HTTP_HOST_DENIED' });
      expect(userinfo.json).toMatchObject({ code: 'HTTP_HOST_DENIED' });
      expect(unbracketed.json).toMatchObject({ code: 'HTTP_HOST_DENIED' });
      expect(folded.json).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
      const upperOrigin = await probe({ host: `LocalHost:${port}`, origin: `http://LocalHost:${port}` });
      expect(upperOrigin.json).toMatchObject({ code: 'ORIGIN_REJECTED' });
    });

    it('compares Origin as an exact string against the normalised Host origin', async () => {
      const { gateway, port, probe } = await started();
      expect((await probe({})).json).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
      for (const origin of [
        `HTTP://127.0.0.1:${port}`, `https://127.0.0.1:${port}`, 'http://127.0.0.1', `http://localhost:${port}`,
        'null', '', `http://127.0.0.1:${port}/`, `http://127.0.0.1:0${port}`,
      ]) {
        const refused = await probe({ origin });
        expect(refused.status, JSON.stringify(origin)).toBe(403);
        expect(refused.json, JSON.stringify(origin)).toMatchObject({ code: 'ORIGIN_REJECTED', dispatch: 'not-dispatched' });
      }
      expect(gateway.origin).toBe(`http://127.0.0.1:${port}`);
    });

    it('requires Origin on every method but GET', async () => {
      const { gateway, probe } = await started();
      for (const method of ['POST', 'HEAD', 'DELETE']) {
        const missing = await call(gateway.origin, '/daemon/readyz', { method, omitOrigin: true });
        expect(missing.status, method).toBe(403);
        if (method !== 'HEAD') expect(missing.json, method).toMatchObject({ code: 'ORIGIN_REQUIRED' });
      }
      const get = await call(gateway.origin, '/daemon/readyz', { omitOrigin: true });
      expect(get.status).toBe(401);
      expect((await probe({ omitOrigin: true })).json).toMatchObject({ code: 'ORIGIN_REQUIRED' });
    });

    it('accepts only same-origin and none as Fetch Metadata, case-sensitively', async () => {
      const { probe } = await started();
      for (const site of ['same-origin', 'none']) {
        expect((await probe({ headers: { 'sec-fetch-site': site } })).json, site).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
      }
      for (const site of ['same-site', 'cross-site', 'Same-Origin', 'user', '']) {
        const refused = await probe({ headers: { 'sec-fetch-site': site } });
        expect(refused.status, site).toBe(403);
        expect(refused.json, site).toMatchObject({ code: 'ORIGIN_REJECTED', dispatch: 'not-dispatched' });
      }
    });

    it('refuses CORS: no Access-Control headers and OPTIONS through the normal pipeline', async () => {
      const { gateway, probe } = await started();
      const preflight = await call(gateway.origin, '/daemon/session', { method: 'OPTIONS', omitOrigin: true });
      const sameOrigin = await call(gateway.origin, '/daemon/session', { method: 'OPTIONS' });
      const plain = await probe({});
      expect(preflight.status).toBe(403);
      expect(preflight.json).toMatchObject({ code: 'ORIGIN_REQUIRED' });
      expect(sameOrigin.status).toBe(405);
      expect(sameOrigin.json).toMatchObject({ code: 'HTTP_METHOD_NOT_ALLOWED' });
      for (const response of [preflight, sameOrigin, plain]) {
        expect(Object.keys(response.headers).filter((name) => name.startsWith('access-control-'))).toEqual([]);
      }
    });
  });

  describe('request target and check order (README sections 5 and 5.1)', () => {
    it('normalises dot segments and absolute-form targets and decodes nothing else', async () => {
      const { gateway, port } = await started();
      const headers = { host: `127.0.0.1:${port}`, origin: gateway.origin, 'content-type': 'application/json' };
      const body = JSON.stringify({ grant: 'wrong-grant' });
      for (const target of [
        'POST /daemon/x/../session',
        'POST /daemon/%2e%2e/daemon/session',
        'POST /daemon/./session',
        `POST http://127.0.0.1:${port}/daemon/session`,
        'POST /daemon/session?ignored=1',
        'POST /daemon/session#fragment',
      ]) {
        const response = await rawCall(gateway.origin, target, headers, body);
        expect(response.json, target).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
      }
      const wrongMethod = await rawCall(gateway.origin, 'GET /daemon/session?x=1', { host: headers.host });
      expect(wrongMethod.status).toBe(405);
      const encoded = await rawCall(gateway.origin, 'GET /daemon/request/a%2Fb', { host: headers.host });
      expect(encoded.status).toBe(404);
      expect(encoded.json).toMatchObject({ code: 'METHOD_NOT_FOUND', method: 'a%2Fb' });
      const trailing = await rawCall(gateway.origin, 'GET /daemon/readyz/', { host: headers.host });
      expect(trailing.status).toBe(404);
      expect(trailing.json).toMatchObject({ code: 'METHOD_NOT_FOUND' });
    });

    it('runs Host, Origin, Fetch Metadata and the route in that order, and the route classification before the method check', async () => {
      const { gateway, port, probe } = await started();
      const hostAndOrigin = await probe({ host: `10.0.0.1:${port}`, origin: 'http://evil.example', headers: { 'sec-fetch-site': 'cross-site' } });
      expect(hostAndOrigin.json).toMatchObject({ code: 'HTTP_HOST_DENIED' });
      const originFirst = await probe({ omitOrigin: true, headers: { 'sec-fetch-site': 'cross-site' } });
      expect(originFirst.json).toMatchObject({ code: 'ORIGIN_REQUIRED' });

      const unknownWithoutOrigin = await call(gateway.origin, '/daemon/nothing', { method: 'POST', body: {}, omitOrigin: true });
      expect(unknownWithoutOrigin.status).toBe(403);
      expect(unknownWithoutOrigin.json).toMatchObject({ code: 'ORIGIN_REQUIRED' });
      const unknown = await call(gateway.origin, '/daemon/nothing');
      expect(unknown.status).toBe(404);
      expect(unknown.json).toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched' });
      expect('method' in (unknown.json ?? {})).toBe(false);

      const malformed = await call(gateway.origin, '/daemon/request/Bad', { method: 'DELETE', body: {} });
      expect(malformed.status).toBe(404);
      expect(malformed.json).toMatchObject({ code: 'METHOD_NOT_FOUND', method: 'Bad' });
      const nested = await call(gateway.origin, '/daemon/request/a/b');
      expect(nested.json).toMatchObject({ code: 'METHOD_NOT_FOUND', method: 'a/b' });
      const bare = await call(gateway.origin, '/daemon/request/');
      expect(bare.status).toBe(404);
      expect('method' in (bare.json ?? {})).toBe(false);
    });

    it('puts the request id in the header and the body of a host failure and no dispatch header', async () => {
      const { gateway } = await started();
      const cases: Array<[string, string, string | null]> = [
        ['GET', '/daemon/request/agent.create', 'agent.create'],
        ['POST', '/daemon/request/agent.create', 'agent.create'],
        ['POST', '/daemon/request/a%2Fb', 'a%2Fb'],
        ['GET', '/daemon/request', null],
        ['GET', '/daemon/readyz', null],
        ['GET', '/daemon/session', null],
      ];
      for (const [method, path, expected] of cases) {
        const response = await call(gateway.origin, path, { method, body: method === 'POST' ? {} : undefined });
        const label = `${method} ${path}`;
        expect(response.json?.ok, label).toBe(false);
        expect(response.json?.requestId, label).toMatch(/^gui-/);
        expect(response.headers['x-hyprial-request-id'], label).toBe(response.json?.requestId);
        expect(response.headers['x-hyprial-dispatch'], label).toBeUndefined();
        expect(response.headers['content-type'], label).toBe('application/json; charset=utf-8');
        expect(response.headers['cache-control'], label).toBe('no-store');
        expect(response.headers['x-content-type-options'], label).toBe('nosniff');
        if (expected === null) expect('method' in (response.json ?? {}), label).toBe(false);
        else expect(response.json?.method, label).toBe(expected);
      }
    });
  });
}
