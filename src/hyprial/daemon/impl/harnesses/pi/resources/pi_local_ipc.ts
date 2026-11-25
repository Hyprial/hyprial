/**
 * The one shared Node client for the daemon's local IPC endpoint.
 *
 * Decisions (ipc-node task, 2026-11-19):
 * - `<state>/daemon.sock` is a Unix domain socket on POSIX and a JSON
 *   endpoint FILE on Windows whose `pipe` member names the real
 *   `\\.\pipe\hyprial-<32 lowercase hex>` channel. Validation mirrors the
 *   Python authority, src/hyprial/kernel/impl/platform/windows_pipe.py
 *   (PipeListener.__init__ writes the file, connect() validates it); the
 *   frozen corpus lives in contract/wire/local-ipc-endpoint-samples.json.
 * - Node only CONNECTS through this module; listening stays in the Python
 *   kernel wrapper. scripts/ci/check_node_net_apis.py keeps every other
 *   net.createConnection / net.connect / net.createServer out of the tree.
 * - `platform` is a parameter so non-Windows machines can exercise the
 *   win32 branch in tests (the caller default is process.platform).
 */

import { readFileSync } from "node:fs";
import * as net from "node:net";

const WINDOWS_TRANSPORT = "windows-pipe-v1";
const WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\hyprial-";
const ENDPOINT_MAX_BYTES = 4096;
const LOWERCASE_HEX_32 = /^[0-9a-f]{32}$/;

export function resolveLocalIpcEndpoint(
  endpointPath: string,
  platform: string = process.platform,
): string {
  if (platform !== "win32") return endpointPath;
  const raw = readFileSync(endpointPath);
  let document: unknown = null;
  if (raw.length <= ENDPOINT_MAX_BYTES) {
    try {
      document = JSON.parse(raw.toString("utf8"));
    } catch {
      document = null;
    }
  }
  if (typeof document !== "object" || document === null || Array.isArray(document)) {
    throw new Error("invalid local Windows IPC endpoint");
  }
  const record = document as Record<string, unknown>;
  const name = record.pipe;
  const pipe = typeof name === "string" ? name : "";
  const suffix = pipe.startsWith(WINDOWS_PIPE_PREFIX)
    ? pipe.slice(WINDOWS_PIPE_PREFIX.length)
    : "";
  if (
    record.transport !== WINDOWS_TRANSPORT ||
    typeof name !== "string" ||
    !pipe.startsWith(WINDOWS_PIPE_PREFIX) ||
    !LOWERCASE_HEX_32.test(suffix)
  ) {
    throw new Error("invalid local Windows IPC endpoint");
  }
  return pipe;
}

export function connectLocalIpc(
  endpointPath: string,
  platform: string = process.platform,
): net.Socket {
  return net.createConnection(resolveLocalIpcEndpoint(endpointPath, platform));
}
