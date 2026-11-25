/**
 * Decision record (M1 browser acceptance, 2026-11-05; S4 split, 2026-11-22)
 * - Ephemeral loopback ports are picked via `freePort()` so parallel tests
 *   never collide on fixed ports; the socket is closed immediately after port
 *   allocation and never serves requests.
 * - A test-owned AF_UNIX proxy (`startIpcDropProxy`) sits between
 *   `request_http` (inside the Python session host) and the isolated daemon
 *   for lost-response fault-injection tests. It forwards real daemon replies
 *   or drops a single matching response to exercise unknown-outcome handling,
 *   and never impersonates `request_http` or the daemon application.
 * - Kept minimal (only `freePort` and `startIpcDropProxy`) and isolated from
 *   non-network CLI/daemon/process helpers so `sanctionedHostRoots` in
 *   `scripts/ci/gui-request-paths-scanner.json` remains exact.
 */

import { connect as connectNet, createServer as createNetServer } from 'node:net';

export async function freePort() {
  const server = createNetServer();
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(0, '127.0.0.1', resolveListen);
  });
  const port = server.address().port;
  await new Promise((resolveClose) => server.close(resolveClose));
  return port;
}

export async function startIpcDropProxy(
  listenPath,
  upstreamPath,
  dropMethod,
  { armAfterMethod = null, onDrop = null } = {},
) {
  const posts = [];
  const dropped = [];
  const connections = new Set();
  let armed = armAfterMethod === null;
  const server = createNetServer({ allowHalfOpen: true }, (client) => {
    connections.add(client);
    let requestBuffer = '';
    let requestSeen = false;
    let dropId = null;
    let responseBuffer = '';
    let upstream = null;
    client.setEncoding('utf8');
    client.on('data', (chunk) => {
      if (requestSeen) return;
      requestBuffer += chunk;
      const newline = requestBuffer.indexOf('\n');
      if (newline < 0) return;
      requestSeen = true;
      const line = requestBuffer.slice(0, newline + 1);
      let frame;
      try { frame = JSON.parse(line); } catch { client.destroy(); return; }
      posts.push({ method: frame.method, params: frame.params ?? null, id: frame.id });
      if (armAfterMethod !== null && frame.method === armAfterMethod) armed = true;
      const shouldDrop = armed && frame.method === dropMethod;
      if (shouldDrop) {
        armed = false;
        dropId = frame.id;
      }
      upstream = connectNet(upstreamPath);
      connections.add(upstream);
      upstream.setEncoding('utf8');
      upstream.on('connect', () => upstream.write(line));
      upstream.on('data', async (upstreamChunk) => {
        if (dropId === null) {
          client.write(upstreamChunk);
          return;
        }
        responseBuffer += upstreamChunk;
        while (true) {
          const responseEnd = responseBuffer.indexOf('\n');
          if (responseEnd < 0) return;
          const responseLine = responseBuffer.slice(0, responseEnd + 1);
          responseBuffer = responseBuffer.slice(responseEnd + 1);
          let response;
          try { response = JSON.parse(responseLine); } catch { client.destroy(); upstream.destroy(); return; }
          if (response.id !== dropId) {
            client.write(responseLine);
            continue;
          }
          const record = { method: frame.method, request: frame, response };
          if (onDrop !== null) await onDrop(record, upstreamPath);
          dropped.push(record);
          client.destroy();
          upstream.destroy();
          return;
        }
      });
      upstream.on('error', () => client.destroy());
      client.on('close', () => upstream?.destroy());
      upstream.on('close', () => connections.delete(upstream));
    });
    client.on('error', () => client.destroy());
    client.on('close', () => connections.delete(client));
  });
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(listenPath, resolveListen);
  });
  return {
    posts,
    dropped,
    endpointPath: listenPath,
    arm() { armed = true; },
    close: async () => {
      for (const connection of connections) connection.destroy();
      await new Promise((resolveClose) => server.close(resolveClose));
    },
  };
}
