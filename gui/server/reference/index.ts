/** Explicit local reference capability. Credentials stay in the trusted host, never in Vite defines. */
import { readFileSync, statSync } from 'node:fs';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin } from 'vite';

type Target = { endpoint: string; token: string; principal: string; driver: 'daemon' | 'contract-mock' };
const loopback = new Set(['localhost', '127.0.0.1', '[::1]']);
export function referenceHttpPlugin(configPath: string | undefined, development = false): Plugin {
  const targets: Record<string, Target> = {};
  if (configPath) {
    const metadata = statSync(configPath);
    if ((metadata.mode & 0o777) !== 0o600 || metadata.uid !== process.getuid?.()) throw new Error('Reference config requires owner-only 0600 permissions');
    const config = JSON.parse(readFileSync(configPath, 'utf8')) as Record<string, Target>;
    for (const [name, target] of Object.entries(config)) {
      if (!/^(live|mock-(success|empty|error|unconfigured))$/.test(name) || (!development && name !== 'live')) throw new Error('Mock reference targets require development mode');
      const endpoint = new URL(target.endpoint);
      if (endpoint.protocol !== 'http:' || !loopback.has(endpoint.hostname) || !endpoint.port || endpoint.pathname !== '/' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error('Reference endpoint must be loopback HTTP');
      if (!target.token || !target.principal || target.driver !== (name === 'live' ? 'daemon' : 'contract-mock')) throw new Error('Reference target requires explicit driver and trusted principal');
      targets[name] = target;
    }
  }
  async function handle(req: IncomingMessage, res: ServerResponse, next: () => void) {
    if (!req.url?.startsWith('/api/reference/')) { next(); return; }
    const match = /^\/api\/reference\/(live|mock-(?:success|empty|error|unconfigured))\/daemon\/request\/([a-z][A-Za-z0-9.]*)$/.exec(req.url);
    const target = match ? targets[match[1]] : undefined;
    const fail = (status: number) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end('{"ok":false,"message":"reference capability unavailable or refused"}'); };
    if (!match || !target) { fail(503); return; }
    if (req.method !== 'POST' || req.headers['content-type']?.split(';')[0] !== 'application/json' ||
        req.headers.authorization !== 'Bearer same-origin-reference' || req.headers['sec-fetch-site'] === 'cross-site' ||
        (req.headers.origin && req.headers.origin !== `http://${req.headers.host}`)) { fail(403); return; }
    const chunks: Buffer[] = [];
    let length = 0;
    try {
      for await (const part of req) {
        const chunk = Buffer.from(part); length += chunk.length;
        if (length > 65536) { fail(413); return; }
        chunks.push(chunk);
      }
      const response = await fetch(`${target.endpoint}/daemon/request/${match[2]}`, {
        method: 'POST', headers: { Authorization: `Bearer ${target.token}`, 'Content-Type': 'application/json' },
        body: Buffer.concat(chunks), signal: AbortSignal.timeout(15000), redirect: 'error',
      });
      res.writeHead(response.status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store',
        ...(response.headers.has('x-hyprial-catalog-digest') ? { 'X-Hyprial-Catalog-Digest': response.headers.get('x-hyprial-catalog-digest')! } : {}) });
      res.end(await response.text());
    } catch { if (!res.headersSent) fail(502); else res.end(); }
  }
  return { name: 'hyprial-reference-http',
    configureServer(server) { server.middlewares.use((req, res, next) => { void handle(req, res, next); }); },
    configurePreviewServer(server) { server.middlewares.use((req, res, next) => { void handle(req, res, next); }); },
  };
}
