/**
 * Decision record (GUI session contract v1 conformance, 2026-11-15)
- These cases pin the session model of contract/gui-session/README.md sections
  3, 4, 5, 11 and 13 black-box, through the Python host started by
  ../support/python-host.ts. Each case states the contract rule it pins.
- Lifetime cases use the 1 000 ms minimum TTL steps because an out-of-process
  host cannot share a clock; their sleeps only ever deepen expiry.
 */

import { describe, expect, it } from 'vitest';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  GRANT, PRINCIPAL, browserHeaders, call, cleanups, config, conforms, cookieOf,
  host, maxAge, open, rawCall, upstream,
} from './host-harness';


export function registerSessionConformance(): void {
  describe('session and control lifetime (README section 4)', () => {
    it('fixes the control expiry at mint: a refresh never extends it', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint, { sessionTtlMs: 3_000 }));
      const before = Date.now();
      const opened = await open(gateway.origin);
      const after = Date.now();
      expect(opened.response.status).toBe(200);
      const expiresAt = opened.session.expiresAt as number;
      expect(expiresAt).toBeGreaterThanOrEqual(before + 3_000);
      expect(expiresAt).toBeLessThanOrEqual(after + 3_000);
      for (const name of ['hyprial_gui_control', 'hyprial_gui_session']) {
        expect(maxAge(opened.response, name)).toBeGreaterThanOrEqual(2);
        expect(maxAge(opened.response, name)).toBeLessThanOrEqual(3);
      }

      await gateway.advance(1_200);
      const refreshed = await call(gateway.origin, '/daemon/session', {
        method: 'POST', body: {}, headers: {
          cookie: cookieOf(opened.cookies, 'hyprial_gui_control'), 'x-hyprial-view': String(opened.session.view),
        },
      });
      expect(refreshed.status).toBe(200);
      expect(refreshed.json?.expiresAt).toBe(expiresAt);
      expect(maxAge(refreshed, 'hyprial_gui_control')).toBe(1);
      expect(maxAge(refreshed, 'hyprial_gui_session')).toBe(1);

      await gateway.advance(1_900);
      const readyz = await call(gateway.origin, '/daemon/readyz', {
        headers: browserHeaders(refreshed.json as Record<string, unknown>, cookieJoin(refreshed)),
      });
      expect(readyz.status).toBe(401);
      const late = await call(gateway.origin, '/daemon/session', {
        method: 'POST', body: {}, headers: {
          cookie: cookieOf(opened.cookies, 'hyprial_gui_control'), 'x-hyprial-view': String(opened.session.view),
        },
      });
      expect(late.status).toBe(401);
      expect(late.json).toMatchObject({ code: 'SESSION_REQUIRED', dispatch: 'not-dispatched' });
    });

    it('serves the launcher principal in the exact serving shape', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      expect(opened.session.principal).toEqual({
        kind: 'serving', owner: PRINCIPAL.owner, node: PRINCIPAL.node, actor: PRINCIPAL.actor,
      });
      expect(opened.session.phase).toBe('ready');
      conforms('sessionResponse', opened.session);
    });
  });

  function cookieJoin(response: { headers: Record<string, unknown> }): string {
    const values = response.headers['set-cookie'];
    const list = Array.isArray(values) ? values as string[] : [];
    return list.map((value) => value.split(';', 1)[0]).join('; ');
  }

  describe('the one-time launch grant (README section 3)', () => {
    it('reports a used grant as used even after its TTL passed (invalid, used, expired)', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint, { launchTtlMs: 1_000 }));
      expect((await open(gateway.origin)).response.status).toBe(200);
      await gateway.advance(1_100);
      const repeated = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: GRANT } });
      expect(repeated.status).toBe(409);
      expect(repeated.json).toMatchObject({ code: 'LAUNCH_GRANT_USED', dispatch: 'not-dispatched' });
      const wrong = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: 'wrong' } });
      expect(wrong.status).toBe(403);
      expect(wrong.json).toMatchObject({ code: 'LAUNCH_GRANT_INVALID' });
    });

    it('treats a grant presented before the publication marker exists as expired, then usable once it appears', async () => {
      const target = await upstream();
      const directory = mkdtempSync(join(tmpdir(), 'gui-launch-'));
      cleanups.push(() => rmSync(directory, { recursive: true, force: true }));
      const publicationPath = join(directory, 'launch.ready');
      const gateway = await host(config(target.endpoint, { publicationPath }));
      const early = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: GRANT } });
      expect(early.status).toBe(401);
      expect(early.json).toMatchObject({ code: 'LAUNCH_GRANT_EXPIRED', dispatch: 'not-dispatched' });
      const wrong = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: 'wrong' } });
      expect(wrong.status).toBe(403);
      expect(target.captured).toHaveLength(0);

      writeFileSync(publicationPath, '', { mode: 0o600 });
      const opened = await open(gateway.origin);
      expect(opened.response.status).toBe(200);
      expect(existsSync(publicationPath)).toBe(false);
    });

    it('does not consume the grant when the upstream probe fails', async () => {
      const options = { readyStatus: 503 as number | undefined, readyPayload: { ok: false, code: 'DAEMON_RESTORING' } as Record<string, unknown> | undefined };
      const target = await upstream(options);
      const directory = mkdtempSync(join(tmpdir(), 'gui-launch-'));
      cleanups.push(() => rmSync(directory, { recursive: true, force: true }));
      const handoffPath = join(directory, 'launch.url');
      writeFileSync(handoffPath, 'http://127.0.0.1/#grant=private', { mode: 0o600 });
      const gateway = await host(config(target.endpoint, { handoffPath }));
      const failed = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { grant: GRANT } });
      expect(failed.status).toBe(503);
      expect(failed.json).toMatchObject({ code: 'HTTP_NOT_READY' });
      expect(failed.headers['set-cookie']).toBeUndefined();
      expect(existsSync(handoffPath)).toBe(true);

      options.readyStatus = undefined;
      options.readyPayload = undefined;
      const retried = await open(gateway.origin);
      expect(retried.response.status).toBe(200);
      expect(existsSync(handoffPath)).toBe(false);
    });

    it('answers a malformed initial body before it looks at the grant', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      for (const body of [{ grant: GRANT, extra: 1 }, { grant: '' }, { grant: 5 }, { other: 1 }]) {
        const refused = await call(gateway.origin, '/daemon/session', { method: 'POST', body });
        expect(refused.status, JSON.stringify(body)).toBe(400);
        expect(refused.json).toMatchObject({ code: 'INVALID_REQUEST', dispatch: 'not-dispatched' });
      }
      expect(target.captured).toHaveLength(0);
      expect((await open(gateway.origin)).response.status).toBe(200);
    });
  });

  describe('contract version negotiation (README section 13)', () => {
    it('refuses an unsupported or malformed version before the grant, the probe and consumption', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const send = (version: string | string[], body: unknown = { grant: GRANT }, extra: Record<string, string> = {}) => call(
        gateway.origin, '/daemon/session', {
          method: 'POST', body, headers: { 'x-hyprial-session-contract': version, ...extra },
        },
      );
      const refusals: Array<string | string[]> = ['2', '99999999999999999999', 'abc', '1.0', '', '-1', '+1', '0', '01', ['1', '1']];
      for (const version of refusals) {
        const refused = await send(version);
        expect(refused.status, JSON.stringify(version)).toBe(409);
        expect(refused.json).toMatchObject({
          ok: false, code: 'SESSION_CONTRACT_UNSUPPORTED', dispatch: 'not-dispatched', data: { min: 1, max: 1 },
        });
        conforms('hostError', refused.json);
      }
      expect((await send('2', { grant: 'wrong-grant' })).json?.code).toBe('SESSION_CONTRACT_UNSUPPORTED');
      expect((await send('2', { grant: 5 })).json?.code).toBe('INVALID_REQUEST');
      const media = await send('2', undefined, { 'content-type': 'text/plain' });
      expect(media.json?.code).toBe('HTTP_UNSUPPORTED_MEDIA_TYPE');
      expect(target.captured).toHaveLength(0);

      const accepted = await send('1');
      expect(accepted.status).toBe(200);
      expect(accepted.json?.sessionContract).toEqual({ version: 1, min: 1, max: 1 });
      conforms('sessionResponse', accepted.json);
      expect(target.captured).toHaveLength(2);

      const control = cookieOf(cookiesOf(accepted), 'hyprial_gui_control');
      const refresh = await send('2', {}, { cookie: control, 'x-hyprial-view': String(accepted.json?.view) });
      expect(refresh.status).toBe(409);
      expect(refresh.json).toMatchObject({ code: 'SESSION_CONTRACT_UNSUPPORTED' });
      expect(target.captured).toHaveLength(2);
    });

    it('applies version 1 when the client declares none', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      expect(opened.response.status).toBe(200);
      expect(opened.session.sessionContract).toEqual({ version: 1, min: 1, max: 1 });
    });
  });

  function cookiesOf(response: { headers: Record<string, unknown> }): string {
    return cookieJoin(response);
  }

  describe('refresh, status and logout bodies (README sections 4 and 5)', () => {
    it('changes nothing when a refresh is refused', async () => {
      const options = { readyStatus: undefined as number | undefined, readyPayload: undefined as Record<string, unknown> | undefined };
      const target = await upstream(options);
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const headers = browserHeaders(opened.session, opened.cookies);
      options.readyStatus = 503;
      options.readyPayload = { ok: false, code: 'DAEMON_RESTORING' };
      const refused = await call(gateway.origin, '/daemon/session', { method: 'POST', body: {}, headers });
      expect(refused.status).toBe(503);
      expect(refused.json).toMatchObject({ code: 'HTTP_NOT_READY' });
      options.readyStatus = undefined;
      options.readyPayload = undefined;
      const status = await call(gateway.origin, '/daemon/session/status', { method: 'POST', body: {}, headers });
      expect(status.json?.current).toBe(true);
      expect((await call(gateway.origin, '/daemon/readyz', { headers })).status).toBe(200);
    });

    it('requires the exact empty object for refresh and status, and any object for logout', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const headers = browserHeaders(opened.session, opened.cookies);
      const refresh = await call(gateway.origin, '/daemon/session', { method: 'POST', body: { extra: 1 }, headers });
      expect(refresh.status).toBe(400);
      expect(refresh.json).toMatchObject({ code: 'INVALID_REQUEST', dispatch: 'not-dispatched' });
      const status = await call(gateway.origin, '/daemon/session/status', { method: 'POST', body: { extra: 1 }, headers });
      expect(status.status).toBe(400);
      expect(status.json).toMatchObject({ code: 'INVALID_REQUEST' });
      const logout = await call(gateway.origin, '/daemon/session/logout', { method: 'POST', body: { extra: 1 }, headers });
      expect(logout.status).toBe(200);
      expect(logout.json).toMatchObject({ ok: true, revoked: true });
      expect(logout.headers['set-cookie']).toEqual([
        'hyprial_gui_control=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict',
        'hyprial_gui_session=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict',
      ]);
    });

    it('reports status without contacting the upstream, false without a session cookie, 401 without the control proof', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const seen = target.captured.length;
      const control = cookieOf(opened.cookies, 'hyprial_gui_control');
      const view = String(opened.session.view);
      const live = await call(gateway.origin, '/daemon/session/status', {
        method: 'POST', body: {}, headers: browserHeaders(opened.session, opened.cookies),
      });
      expect(live.json?.current).toBe(true);
      const partial = await call(gateway.origin, '/daemon/session/status', {
        method: 'POST', body: {}, headers: { cookie: control, 'x-hyprial-view': view },
      });
      expect(partial.status).toBe(200);
      expect(partial.json?.current).toBe(false);
      const staleGeneration = await call(gateway.origin, '/daemon/session/status', {
        method: 'POST', body: {}, headers: { ...browserHeaders(opened.session, opened.cookies), 'x-hyprial-generation': 'old' },
      });
      expect(staleGeneration.json?.current).toBe(false);
      const noControl = await call(gateway.origin, '/daemon/session/status', {
        method: 'POST', body: {}, headers: { 'x-hyprial-view': view },
      });
      expect(noControl.status).toBe(401);
      expect(noControl.json).toMatchObject({ code: 'SESSION_REQUIRED' });
      const wrongView = await call(gateway.origin, '/daemon/session/status', {
        method: 'POST', body: {}, headers: { cookie: control, 'x-hyprial-view': 'wrong' },
      });
      expect(wrongView.status).toBe(401);
      expect(target.captured).toHaveLength(seen);
    });

    it('reads and validates the body before it authenticates on session routes', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      for (const path of ['/daemon/session', '/daemon/session/status', '/daemon/session/logout']) {
        const media = await call(gateway.origin, path, {
          method: 'POST', body: {}, headers: { 'content-type': 'text/plain' },
        });
        expect(media.status, path).toBe(415);
        expect(media.json).toMatchObject({ code: 'HTTP_UNSUPPORTED_MEDIA_TYPE', dispatch: 'not-dispatched' });
        expect(media.json?.method).toBeUndefined();
        const scalar = await call(gateway.origin, path, { method: 'POST', body: [] });
        expect(scalar.status, path).toBe(400);
        expect(scalar.json).toMatchObject({ code: 'INVALID_REQUEST' });
        const large = await call(gateway.origin, path, { method: 'POST', body: 'x'.repeat(8_193) });
        expect(large.status, path).toBe(413);
        expect(large.json).toMatchObject({ code: 'HTTP_REQUEST_TOO_LARGE' });
        const wrong = await call(gateway.origin, path, { method: 'GET' });
        expect(wrong.status, path).toBe(405);
        expect(wrong.json).toMatchObject({ code: 'HTTP_METHOD_NOT_ALLOWED' });
      }
      const empty = await rawCall(gateway.origin, 'POST /daemon/session/status', {
        host: new URL(gateway.origin).host, origin: gateway.origin, 'content-type': 'application/json',
      });
      expect(empty.status).toBe(400);
      expect(empty.json).toMatchObject({ code: 'INVALID_REQUEST' });
      const authenticated = await call(gateway.origin, '/daemon/session/logout', { method: 'POST', body: {} });
      expect(authenticated.status).toBe(401);
      expect(authenticated.json).toMatchObject({ code: 'SESSION_REQUIRED' });
      expect(target.captured).toHaveLength(0);
    });

    it('parses cookies: the first non-empty duplicate wins, empty and nameless pairs are skipped, an undecodable first value is 400', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const good = cookieOf(opened.cookies, 'hyprial_gui_session');
      const headers = browserHeaders(opened.session, opened.cookies);
      const read = (cookie: string) => call(gateway.origin, '/daemon/readyz', { headers: { ...headers, cookie } });
      expect((await read(good)).status).toBe(200);
      expect((await read(`hyprial_gui_session=bad; ${good}`)).status).toBe(401);
      expect((await read(`${good}; hyprial_gui_session=bad`)).status).toBe(200);
      expect((await read(`${good}; hyprial_gui_session=%zz`)).status).toBe(200);
      expect((await read(`hyprial_gui_session=; ${good}`)).status).toBe(200);
      expect((await read(`=x; junk; ${good}`)).status).toBe(200);
      const undecodable = await read(`hyprial_gui_session=%zz; ${good}`);
      expect(undecodable.status).toBe(400);
      expect(undecodable.json).toMatchObject({ code: 'INVALID_REQUEST', dispatch: 'not-dispatched' });
    });

    it('accepts a case-insensitive application/json media type with parameters', async () => {
      const target = await upstream();
      const gateway = await host(config(target.endpoint));
      const opened = await open(gateway.origin);
      const status = await call(gateway.origin, '/daemon/session/status', {
        method: 'POST', body: {}, headers: {
          ...browserHeaders(opened.session, opened.cookies), 'content-type': 'Application/JSON; charset=utf-8',
        },
      });
      expect(status.status).toBe(200);
      expect(status.json?.current).toBe(true);
    });
  });
}
