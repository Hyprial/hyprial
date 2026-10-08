/**
 * I/O-free inbox-watch state machine for the pi interactive carrier.
 *
 * This is the TypeScript port of hyprial.daemon.impl.inbox_watch. Keep the
 * action/outcome vocabulary and transitions aligned with the shared JSON
 * cases in contract/inbox-watch/cases.
 */

export const BACKOFF_CAP_MS = 5_000;

const METHOD_NOT_FOUND = "METHOD_NOT_FOUND";
const SESSION_SUPERSEDED = "SESSION_SUPERSEDED";
const STALE_DAEMON_GENERATION = "STALE_DAEMON_GENERATION";
const STALE_SESSION = "STALE_SESSION";

export function backoffMs(baseMs: number, failures: number): number {
  if (failures <= 0) return baseMs;
  return Math.min(BACKOFF_CAP_MS, baseMs * 2 ** Math.min(failures - 1, 6));
}

export class Register {
  readonly op = "register";
}

export class Refresh {
  readonly op = "refresh";
}

export class Heartbeat {
  readonly op = "heartbeat";
}

export class ListInbox {
  readonly op = "list";
}

export class Wait {
  readonly op = "wait";
  readonly knownMessageIds: readonly string[];
  readonly holdMs: number | null;

  constructor(
    knownMessageIds: readonly string[],
    holdMs: number | null,
  ) {
    this.knownMessageIds = knownMessageIds;
    this.holdMs = holdMs;
  }
}

export class Sleep {
  readonly op = "sleep";
  readonly ms: number;

  constructor(ms: number) {
    this.ms = ms;
  }
}

export class Quiet {
  readonly op = "quiet";
}

export type Request = Register | Refresh | Heartbeat | ListInbox | Wait;
export type Action = Request | Sleep | Quiet;

export class Ok {
  readonly response: Record<string, unknown>;

  constructor(response: Record<string, unknown>) {
    this.response = response;
  }
}

export class Rejected {
  readonly code: string;

  constructor(code: string) {
    this.code = code;
  }
}

export class Unreachable {}

export type Outcome = Ok | Rejected | Unreachable;
export type InboxRow = Record<string, unknown>;

export class InboxChange {
  readonly newMessages: readonly InboxRow[];
  readonly rewake: readonly InboxRow[];
  readonly rewakeReason: string | null;
  readonly completed: readonly string[];

  constructor(
    newMessages: readonly InboxRow[] = [],
    rewake: readonly InboxRow[] = [],
    rewakeReason: string | null = null,
    completed: readonly string[] = [],
  ) {
    this.newMessages = newMessages;
    this.rewake = rewake;
    this.rewakeReason = rewakeReason;
    this.completed = completed;
  }

  get empty(): boolean {
    return (
      this.newMessages.length === 0 &&
      this.rewake.length === 0 &&
      this.completed.length === 0
    );
  }
}

interface Lane {
  outstanding: Request | null;
}

export interface InboxWatchOptions {
  pollIntervalMs: number;
  heartbeatIntervalMs: number;
  holdMs?: number | null;
  useWait?: boolean;
}

export class InboxWatch {
  readonly counters: Record<string, number> = {};

  private readonly pollIntervalMs: number;
  private readonly heartbeatIntervalMs: number;
  private readonly holdMs: number | null;
  /** Mutable, as in the Python core: a client turns the wait off while busy. */
  useWait: boolean;
  private readonly poll: Lane = { outstanding: null };
  private readonly heartbeat: Lane = { outstanding: null };
  private quietState = false;
  private registeredState = false;
  private registeredOnce = false;
  private registerFailures = 0;
  private registerAt = 0;
  private epoch: string | null = null;
  private refreshOwedState = false;
  private refreshInFlight = false;
  private refreshFailures = 0;
  private refreshAt = 0;
  private rewakeReason: string | null = null;
  private listFailures = 0;
  private listAt = 0;
  private waitNext = false;
  private waitSupported: boolean | null = null;
  private waitRefusedEpoch: string | null = null;
  private seen = new Map<string, InboxRow>();
  private listedIds: readonly string[] = [];
  private heartbeatAt = 0;
  private connectedAt: number | null = null;

  constructor(options: InboxWatchOptions) {
    if (options.pollIntervalMs <= 0 || options.heartbeatIntervalMs <= 0) {
      throw new Error("inbox watch intervals must be positive");
    }
    this.pollIntervalMs = options.pollIntervalMs;
    this.heartbeatIntervalMs = options.heartbeatIntervalMs;
    this.holdMs = options.holdMs ?? null;
    this.useWait = options.useWait ?? true;
  }

  get quiet(): boolean {
    return this.quietState;
  }

  get registered(): boolean {
    return this.registeredState;
  }

  get refreshOwed(): boolean {
    return this.refreshOwedState;
  }

  requestRewake(reason = "recovery", now = true): void {
    this.rewakeReason ||= reason;
    if (now) {
      this.listAt = 0;
      this.waitNext = false;
    }
  }

  listNow(): void {
    this.listAt = 0;
    this.waitNext = false;
  }

  observe(response: Record<string, unknown>): void {
    if (!this.quietState) this.observeEpoch(response);
  }

  observeRejection(code: string): void {
    this.terminal(new Rejected(code));
  }

  pollNext(nowMs: number): Action {
    if (this.poll.outstanding !== null) {
      throw new Error(
        `poll lane already waits on ${this.poll.outstanding.op}`,
      );
    }
    if (this.quietState) return new Quiet();
    if (!this.registeredState) {
      if (nowMs < this.registerAt) return new Sleep(this.registerAt - nowMs);
      return this.issue(this.poll, new Register());
    }
    if (this.refreshDue(nowMs)) {
      this.refreshInFlight = true;
      return this.issue(this.poll, new Refresh());
    }
    if (this.waitNext) {
      this.waitNext = false;
      if (!this.refreshOwedState && this.useWait) {
        return this.issue(
          this.poll,
          new Wait([...this.listedIds], this.holdMs),
        );
      }
      this.listAt = nowMs + this.pollIntervalMs;
    }
    if (nowMs >= this.listAt) {
      return this.issue(this.poll, new ListInbox());
    }
    let wakeAt = this.listAt;
    if (this.refreshOwedState && !this.refreshInFlight) {
      wakeAt = Math.min(wakeAt, this.refreshAt);
    }
    return new Sleep(wakeAt - nowMs);
  }

  pollDone(
    action: Request,
    outcome: Outcome,
    nowMs: number,
  ): InboxChange | null {
    this.settle(this.poll, action);
    if (this.terminal(outcome)) return null;
    if (action instanceof Register) {
      this.registerDone(outcome, nowMs);
    } else if (action instanceof Refresh) {
      this.refreshDone(outcome, nowMs);
    } else if (action instanceof ListInbox) {
      return this.listDone(outcome, nowMs);
    } else if (action instanceof Wait) {
      this.waitDone(outcome, nowMs);
    }
    return null;
  }

  heartbeatNext(nowMs: number): Action {
    if (this.heartbeat.outstanding !== null) {
      throw new Error(
        `heartbeat lane already waits on ${this.heartbeat.outstanding.op}`,
      );
    }
    if (this.quietState) return new Quiet();
    if (!this.registeredState) return new Sleep(this.heartbeatIntervalMs);
    if (this.refreshDue(nowMs)) {
      this.refreshInFlight = true;
      return this.issue(this.heartbeat, new Refresh());
    }
    if (nowMs < this.heartbeatAt) {
      return new Sleep(this.heartbeatAt - nowMs);
    }
    return this.issue(this.heartbeat, new Heartbeat());
  }

  heartbeatDone(action: Request, outcome: Outcome, nowMs: number): void {
    this.settle(this.heartbeat, action);
    if (this.terminal(outcome)) return;
    if (action instanceof Refresh) {
      this.refreshDone(outcome, nowMs);
      return;
    }
    this.heartbeatAt = nowMs + this.heartbeatIntervalMs;
    if (outcome instanceof Ok) {
      this.observeEpoch(outcome.response);
    } else if (
      outcome instanceof Rejected &&
      outcome.code === STALE_DAEMON_GENERATION
    ) {
      this.oweRefresh("epoch");
    }
  }

  private issue<T extends Request>(lane: Lane, action: T): T {
    lane.outstanding = action;
    return action;
  }

  private settle(lane: Lane, action: Request): void {
    if (lane.outstanding !== action) {
      throw new Error(`${action.op} completed but was not outstanding`);
    }
    lane.outstanding = null;
    if (action instanceof Refresh) this.refreshInFlight = false;
  }

  private terminal(outcome: Outcome): boolean {
    if (!(outcome instanceof Rejected)) return false;
    if (outcome.code === SESSION_SUPERSEDED) {
      this.quietState = true;
      return true;
    }
    if (outcome.code === STALE_SESSION) {
      this.registeredState = false;
      this.registerAt = 0;
      this.refreshOwedState = false;
      return true;
    }
    return false;
  }

  private refreshDue(nowMs: number): boolean {
    return (
      this.refreshOwedState &&
      !this.refreshInFlight &&
      nowMs >= this.refreshAt
    );
  }

  private oweRefresh(reason: string): void {
    this.refreshOwedState = true;
    this.rewakeReason ||= reason;
  }

  private observeEpoch(response: Record<string, unknown>): void {
    const epoch = response.daemonEpoch;
    if (typeof epoch !== "string" || !epoch) return;
    if (
      this.waitSupported === false &&
      epoch !== this.waitRefusedEpoch
    ) {
      this.waitSupported = null;
    }
    if (this.epoch === null) {
      this.epoch = epoch;
      return;
    }
    if (epoch !== this.epoch) this.oweRefresh("epoch");
  }

  private registerDone(outcome: Outcome, nowMs: number): void {
    if (!(outcome instanceof Ok)) {
      this.registerFailures += 1;
      this.registerAt =
        nowMs + backoffMs(this.pollIntervalMs, this.registerFailures);
      return;
    }
    const epoch = outcome.response.daemonEpoch;
    this.epoch = typeof epoch === "string" && epoch ? epoch : null;
    this.registeredState = true;
    this.registerFailures = 0;
    this.refreshOwedState = false;
    this.refreshFailures = 0;
    this.listAt = 0;
    this.waitNext = false;
    this.heartbeatAt = nowMs + this.heartbeatIntervalMs;
    this.connectedAt = nowMs;
    if (this.registeredOnce) this.rewakeReason ||= "start";
    this.registeredOnce = true;
  }

  private refreshDone(outcome: Outcome, nowMs: number): void {
    if (!(outcome instanceof Ok)) {
      this.refreshFailures += 1;
      this.refreshAt =
        nowMs + backoffMs(this.pollIntervalMs, this.refreshFailures);
      return;
    }
    const epoch = outcome.response.daemonEpoch;
    if (typeof epoch === "string" && epoch) this.epoch = epoch;
    this.refreshOwedState = false;
    this.refreshFailures = 0;
    this.refreshAt = 0;
    this.listAt = 0;
    this.waitNext = false;
  }

  private waitDone(outcome: Outcome, nowMs: number): void {
    if (outcome instanceof Ok) {
      this.waitSupported = true;
      this.observeEpoch(outcome.response);
      if (outcome.response.held === true) {
        this.count(
          outcome.response.changed === true
            ? "wait.held_changed"
            : "wait.held_timeout",
        );
        this.listAt = nowMs;
        return;
      }
      this.count("wait.unheld");
    } else if (
      outcome instanceof Rejected &&
      outcome.code === METHOD_NOT_FOUND
    ) {
      this.count("method_not_found");
      this.waitSupported = false;
      this.waitRefusedEpoch = this.epoch;
    }
    this.listAt = nowMs + this.pollIntervalMs;
  }

  private listDone(outcome: Outcome, nowMs: number): InboxChange | null {
    if (!(outcome instanceof Ok)) {
      this.listFailures += 1;
      this.listAt = nowMs + backoffMs(this.pollIntervalMs, this.listFailures);
      return null;
    }
    this.count("list");
    this.listFailures = 0;
    const response = outcome.response;
    this.observeEpoch(response);
    const rows = Array.isArray(response.messages) ? response.messages : [];
    const listed = new Map<string, InboxRow>();
    for (const value of rows) {
      if (typeof value !== "object" || value === null || Array.isArray(value)) {
        this.count("deliver.invalid");
        continue;
      }
      const row = value as InboxRow;
      const messageId = row.messageId;
      if (typeof messageId !== "string" || !messageId) {
        this.count("deliver.invalid");
        continue;
      }
      listed.set(messageId, row);
    }
    const newMessages = [...listed].flatMap(([messageId, row]) =>
      this.seen.has(messageId) ? [] : [row],
    );
    let rewake: InboxRow[] = [];
    let reason: string | null = null;
    if (this.rewakeReason !== null && !this.refreshOwedState) {
      reason = this.rewakeReason;
      this.rewakeReason = null;
      this.count(`rewake.${reason}`);
      rewake = [...listed].flatMap(([messageId, row]) =>
        this.seen.has(messageId) ? [row] : [],
      );
    }
    const completed = [...this.seen.keys()].filter(
      (messageId) => !listed.has(messageId),
    );
    this.count("deliver.new", newMessages.length);
    this.count("deliver.dedup", listed.size - newMessages.length);
    this.seen = listed;
    this.listedIds = [...listed.keys()];
    if (
      (newMessages.length > 0 || rewake.length > 0) &&
      this.connectedAt !== null
    ) {
      if (!("first_wake_ms_after_connect" in this.counters)) {
        this.counters.first_wake_ms_after_connect =
          nowMs - this.connectedAt;
      }
      this.connectedAt = null;
    }
    const waitable =
      this.useWait &&
      this.waitSupported !== false &&
      !this.refreshOwedState;
    if (waitable) {
      this.waitNext = true;
    } else {
      this.listAt = nowMs + this.pollIntervalMs;
    }
    return new InboxChange(newMessages, rewake, reason, completed);
  }

  private count(name: string, amount = 1): void {
    if (amount !== 0) this.counters[name] = (this.counters[name] ?? 0) + amount;
  }
}
