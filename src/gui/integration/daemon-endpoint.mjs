import { closeSync, openSync, readSync } from 'node:fs';

// Windows has no Unix socket: the daemon listens on a current-user named pipe
// whose name is generated per start, and publishes it as a small JSON document
// at the usual daemon.sock path. A plain path handed to `net.createConnection`
// is read by libuv as a pipe name, so the GUI side must resolve the document
// first. Mirrors the main-process reader in `desktop/src/daemon-ipc.cjs`,
// including its read cap, and accepts only a local `\\.\pipe\hyprial-*` name —
// a remote name is rejected here and again by the pipe's own DACL.
export function daemonEndpoint(socketPath, platform = process.platform) {
  if (platform !== 'win32') return socketPath;
  const descriptor = openSync(socketPath, 'r');
  let text;
  try {
    const buffer = Buffer.alloc(4097);
    const bytesRead = readSync(descriptor, buffer, 0, buffer.length, 0);
    if (bytesRead > 4096) throw new Error('Invalid local Windows daemon endpoint');
    text = buffer.subarray(0, bytesRead).toString('utf8');
  } finally { closeSync(descriptor); }
  const endpoint = JSON.parse(text);
  if (endpoint?.transport !== 'windows-pipe-v1' || typeof endpoint.pipe !== 'string' ||
      !/^\\\\\.\\pipe\\hyprial-[0-9a-f]{32}$/.test(endpoint.pipe)) {
    throw new Error('Invalid local Windows daemon endpoint');
  }
  return endpoint.pipe;
}
