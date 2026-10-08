/**
 * Shared daemon IPC for hyprial's pi extensions (harness-bridge tools and the
 * interactive attach carrier).
 *
 * Both extensions answer with ONE request against the owning daemon's
 * version-1 newline-delimited JSON IPC (the exact wire
 * `hyprial.mcp.unix.UnixDaemonConnection` speaks), signed with the session's own
 * canonical actor + session ref, read from the injected environment:
 *
 *   HYPRIAL_WORKER_ACTOR        the session's canonical agent URI
 *   HYPRIAL_WORKER_SESSION_REF  the session ref the daemon's fence accepts
 *   HYPRIAL_WORKER_TMUX_SESSION detached-tmux session name when the TUI runs
 *                           under `hyprial start --tmux` (optional; recorded on
 *                           the interactive registration so `hyprial ps` shows
 *                           where to attach)
 *   HYPRIAL_HOME / HARNESS_STATE_DIR  locate THIS daemon's socket, never an
 *                           ambient production daemon
 *
 * Extracted from pi_harness_bridge.ts so the attach carrier speaks the
 * identical wire without duplicating it; the bridge's behavior is unchanged.
 */

import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import * as net from "node:net";

// Mirror hyprial.daemon.application._serve_connection / hyprial.cli._daemon_request.
export const IPC_VERSION = 1;
export const IPC_TIMEOUT_MS = 15_000;
export const IPC_MAX_BYTES = 8 * 1024 * 1024;

export interface WorkerIdentity {
  actor: string;
  sessionRef: string;
  /** Detached-tmux session name, present only on `hyprial start --tmux` launches. */
  tmuxSession?: string;
}

export type DaemonResult = Record<string, unknown>;

export interface ProcessBirthIdentityOptions {
  platform?: string;
  readPs?: (pid: number) => string | null;
}

function readPsStart(pid: number): string | null {
  try {
    return execFileSync("ps", ["-o", "lstart=", "-p", String(pid)], {
      encoding: "utf-8",
      env: { PATH: "/bin:/usr/bin", TZ: "UTC0", LC_ALL: "C" },
      stdio: ["ignore", "pipe", "ignore"],
    }).trim() || null;
  } catch {
    return null;
  }
}

/** Match the daemon's process-birth marker grammar for PID-reuse fencing. */
export function processBirthIdentity(
  pid: number,
  options: ProcessBirthIdentityOptions = {},
): string | null {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const platform = options.platform ?? process.platform;
  if (platform === "linux") {
    try {
      const stat = readFileSync(`/proc/${pid}/stat`, "utf-8");
      const closing = stat.lastIndexOf(")");
      const fields = closing >= 0 ? stat.slice(closing + 2).trim().split(/\s+/) : [];
      const starttime = fields[19];
      if (starttime && /^\d+$/.test(starttime)) {
        return `proc-starttime:${starttime}`;
      }
    } catch {
      // Fall through to the daemon's portable ps-lstart component.
    }
  }
  // On Darwin, Date.now() - process.uptime() can round one second later than
  // the kernel birth timestamp. ps reads the same lstart component as the
  // daemon, so boundary starts compare exactly instead of intermittently.
  const started = (options.readPs ?? readPsStart)(pid);
  if (started) return `ps-lstart:${started}`;
  if (platform === "darwin") {
    // Sandboxed hosts can deny ps. The daemon grants only this coarse
    // component a documented one-second comparison tolerance.
    return `darwin-startsec:${Math.floor(Date.now() / 1000 - process.uptime())}`;
  }
  return null;
}

export function identityFromEnv(): WorkerIdentity | null {
  const actor = process.env.HYPRIAL_WORKER_ACTOR?.trim();
  const sessionRef = process.env.HYPRIAL_WORKER_SESSION_REF?.trim();
  if (!actor || !sessionRef) return null;
  const tmuxSession = process.env.HYPRIAL_WORKER_TMUX_SESSION?.trim();
  return { actor, sessionRef, ...(tmuxSession ? { tmuxSession } : {}) };
}
/** Mirror hyprial.cli._socket_path: an explicit state/home root is an isolation
 * boundary and always wins over an ambient HARNESS_SOCKET_PATH override. */
export function daemonSocketPath(): string {
  const stateDir = process.env.HARNESS_STATE_DIR?.trim();
  const hyprialHome = process.env.HYPRIAL_HOME?.trim();
  if (stateDir) return `${stateDir}/daemon.sock`;
  if (hyprialHome) return `${hyprialHome}/state/daemon.sock`;
  const explicit = process.env.HARNESS_SOCKET_PATH?.trim();
  if (explicit) return explicit;
  return `${process.env.HOME}/.hyprial/state/daemon.sock`;
}

/** One daemon request over a fresh connection, like StatelessDaemonProxy.
 * Mutations carry a fresh request id, which the daemon also adopts as the
 * message.send idempotency key. */
export function callDaemon(
  method: string,
  params: Record<string, unknown>,
  mutation: boolean,
): Promise<DaemonResult> {
  return new Promise((resolve, reject) => {
    const frame = {
      version: IPC_VERSION,
      id: mutation ? randomUUID() : null,
      method,
      params,
    };
    const socket = net.createConnection(daemonSocketPath());
    let buffer = Buffer.alloc(0);
    let settled = false;
    const timer = setTimeout(() => {
      finish(() =>
        reject(
          new Error(`IPC_TIMEOUT: timed out waiting for daemon method ${method}`),
        ),
      );
    }, IPC_TIMEOUT_MS);
    const finish = (action: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      action();
    };
    socket.on("connect", () => {
      socket.write(JSON.stringify(frame) + "\n");
    });
    socket.on("data", (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > IPC_MAX_BYTES) {
        finish(() =>
          reject(
            new Error("IPC_RESPONSE_TOO_LARGE: daemon IPC response exceeded 8 MiB"),
          ),
        );
        return;
      }
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) return;
      const line = buffer.subarray(0, newline).toString("utf-8");
      let document: Record<string, unknown>;
      try {
        document = JSON.parse(line);
      } catch {
        finish(() => reject(new Error("daemon returned invalid JSON")));
        return;
      }
      if (!document || document.version !== IPC_VERSION) {
        finish(() => reject(new Error("daemon returned an invalid IPC envelope")));
        return;
      }
      const failure = document.error;
      if (failure && typeof failure === "object") {
        const envelope = failure as Record<string, unknown>;
        const code = String(envelope.code ?? "DAEMON_ERROR");
        const message = String(envelope.message ?? "daemon request failed");
        finish(() => reject(new Error(`${code}: ${message}`)));
        return;
      }
      const result = document.result;
      if (!result || typeof result !== "object" || Array.isArray(result)) {
        finish(() => reject(new Error("daemon result must be an object")));
        return;
      }
      finish(() => resolve(result as DaemonResult));
    });
    socket.on("error", (error: Error) => {
      finish(() =>
        reject(
          new Error(
            `DAEMON_UNAVAILABLE: Harness daemon is not reachable (${error.message})`,
          ),
        ),
      );
    });
    socket.on("end", () => {
      finish(() =>
        reject(new Error("DAEMON_DISCONNECTED: daemon closed IPC before responding")),
      );
    });
  });
}
