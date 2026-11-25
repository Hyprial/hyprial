/**
 * Shared daemon IPC for hyprial's pi extensions (harness-bridge tools and the
 * interactive attach carrier).
 *
 * Decision record (W1E-1, 2026-11-21):
 * - processBirthIdentity supports win32 by reading the live process creation
 *   FILETIME (100-ns ticks since 1601-01-01 UTC) via PowerShell's
 *   Process.StartTime.ToFileTimeUtc() and formatting `win-filetime:<stamp>`,
 *   matching hyprial.kernel.windows_process_identity byte-for-byte.
 * - sessionRefFromFile drops the POSIX-only group/other mode-bit mask check per
 *   Owner Shell-3 ruling (relying on the private Home/state root boundary while
 *   keeping non-symlink, regular-file, size, and POSIX euid checks) so Windows
 *   synthesized file modes do not reject valid interactive session-ref files.
 * - daemonSocketPath resolves the default home directory fallback via Node's
 *   `os.homedir()` instead of an ambient POSIX home environment variable.
 *
 * Both extensions answer with ONE request against the owning daemon's
 * version-1 newline-delimited JSON IPC (the exact wire
 * `hyprial.mcp.unix.UnixDaemonConnection` speaks), signed with the session's own
 * canonical actor + session ref, read from the injected environment:
 *
 *   HYPRIAL_WORKER_ACTOR        the session's canonical agent URI
 *   HYPRIAL_WORKER_SESSION_REF  the session ref the daemon's fence accepts
 *   HYPRIAL_WORKER_SESSION_REF_FILE  secure interactive binding file when the
 *                           ref cannot be published before the TUI starts
 *   HYPRIAL_HOME / HARNESS_STATE_DIR  locate THIS daemon's socket, never an
 *                           ambient production daemon
 */

import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { closeSync, constants, fstatSync, lstatSync, openSync, readFileSync } from "node:fs";
import { homedir } from "node:os";

import { connectLocalIpc } from "./pi_local_ipc.ts";

export const IPC_VERSION = 1;
export const IPC_TIMEOUT_MS = 15_000;
export const IPC_MAX_BYTES = 8 * 1024 * 1024;

export interface WorkerIdentity {
  actor: string;
  sessionRef: string;
}

export type DaemonResult = Record<string, unknown>;

export class DaemonIpcError extends Error {
  readonly code: string;
  readonly data: unknown;

  constructor(code: string, message: string, data?: unknown) {
    super(`${code}: ${message}`);
    this.name = "DaemonIpcError";
    this.code = code;
    this.data = data;
  }
}

export interface ProcessBirthIdentityOptions {
  platform?: string;
  readPs?: (pid: number) => string | null;
  readWinFiletime?: (pid: number) => string | null;
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

function windowsPowershellExecutable(): string {
  const root = (
    process.env.SystemRoot ||
    process.env.SYSTEMROOT ||
    process.env.WINDIR
  )?.trim();
  return root
    ? `${root}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`
    : "powershell.exe";
}

function readWinFiletime(pid: number): string | null {
  const script = `$p = [System.Diagnostics.Process]::GetProcessById(${pid}); if (-not $p.HasExited) { $p.StartTime.ToFileTimeUtc() }`;
  try {
    const output = execFileSync(
      windowsPowershellExecutable(),
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
      {
        encoding: "utf-8",
        stdio: ["ignore", "pipe", "ignore"],
        timeout: 5000,
        windowsHide: true,
      },
    ).trim();
    return /^\d+$/.test(output) ? output : null;
  } catch {
    return null;
  }
}

export function processBirthIdentity(
  pid: number,
  options: ProcessBirthIdentityOptions = {},
): string | null {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const platform = options.platform ?? process.platform;
  if (platform === "win32") {
    const filetime = (options.readWinFiletime ?? readWinFiletime)(pid);
    return filetime && /^\d+$/.test(filetime) ? `win-filetime:${filetime}` : null;
  }
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
      void 0;
    }
  }
  const started = (options.readPs ?? readPsStart)(pid);
  if (started) return `ps-lstart:${started}`;
  if (platform === "darwin") {
    return `darwin-startsec:${Math.floor(Date.now() / 1000 - process.uptime())}`;
  }
  return null;
}

function sessionRefFromFile(path: string): string | null {
  let descriptor: number | null = null;
  try {
    if (lstatSync(path).isSymbolicLink()) return null;
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const metadata = fstatSync(descriptor);
    const effectiveUid = process.geteuid?.();
    if (
      !metadata.isFile() ||
      metadata.size > 4096 ||
      (effectiveUid !== undefined && metadata.uid !== effectiveUid)
    ) return null;
    const value = readFileSync(descriptor, "utf8").trim();
    return value && !/\s/.test(value) ? value : null;
  } catch {
    return null;
  } finally {
    if (descriptor !== null) closeSync(descriptor);
  }
}

export function identityFromEnv(): WorkerIdentity | null {
  const actor = process.env.HYPRIAL_WORKER_ACTOR?.trim();
  const directRef = process.env.HYPRIAL_WORKER_SESSION_REF?.trim();
  const refFile = process.env.HYPRIAL_WORKER_SESSION_REF_FILE?.trim();
  if (directRef && refFile) return null;
  const sessionRef = directRef || (refFile ? sessionRefFromFile(refFile) : null);
  if (!actor || !sessionRef) return null;
  return { actor, sessionRef };
}

export function daemonSocketPath(): string {
  const stateDir = process.env.HARNESS_STATE_DIR?.trim();
  const hyprialHome = process.env.HYPRIAL_HOME?.trim();
  if (stateDir) return `${stateDir}/daemon.sock`;
  if (hyprialHome) return `${hyprialHome}/state/daemon.sock`;
  const explicit = process.env.HARNESS_SOCKET_PATH?.trim();
  if (explicit) return explicit;
  return `${homedir()}/.hyprial/state/daemon.sock`;
}

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
    const socket = connectLocalIpc(daemonSocketPath());
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
        finish(() => reject(new DaemonIpcError(code, message, envelope.data)));
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
