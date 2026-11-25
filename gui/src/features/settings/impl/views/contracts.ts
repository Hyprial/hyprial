/** UI capability view models retained from the settings prototype.
 * These are component inputs, NOT a daemon schema or callable runtime contract.
 * Missing adapters are intentionally not implemented or injected on the GUI baseline.
 * Live HTTP adapters must use the current generated client when their owners land.
 */

export interface AutoupdateStatusResult {
  active: boolean;
  autoUpgradeEnabled: boolean | null;
  environmentPathDigest: string;
  lastError?: null | string;
  lastExitCode: null | number;
  lastRun: Record<string, Array<unknown> | Record<string, unknown> | boolean | null | number | string> | null;
  lastSkipReason: null | string;
  lastTrigger: null | string;
  lastTriggeredAt: null | string;
  nextRunAt: null | string;
  ok: true;
  pending: boolean;
  running: boolean;
  schedule: Array<{ hour: number; minute: number }>;
  trigger: "daemon";
}

export interface DaemonStatusResult {
  phase: "reconciled" | "serving";
  running: true;
  version: string;
}

export interface LogPageResult {
  entries: Array<{ component: "adapter" | "daemon" | "other" | "worker"; event: "daemon.started" | "daemon.stopped" | "dead-letter" | "other" | "worker.exited"; level: "debug" | "error" | "info" | "warn"; timestamp: string }>;
  nextCursor: null | string;
  order: "file-offset";
  partial: boolean;
  partialReasons: Array<"discovery-limit" | "invalid-record" | "logs-unavailable" | "scan-limit" | "unreadable-file" | "unsafe-path">;
  skippedLines: number;
  snapshotExpiresInSeconds: number;
}
