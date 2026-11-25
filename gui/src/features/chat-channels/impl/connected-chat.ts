/**
 * Decision record (ordinary connected chat controller, 2026-11-05)
- Abandoning the tab-local key is a tab-level action, so it must clear every
  target's state that the key produced, not only the selected target's (#2475
  cross-target review). A tab keeps one key, so `unknown` on any target and the
  `pending-operation` refusal on any other target both belong to that key; the
  owner can click the page-level row while another target is selected. Leaving
  them behind permanently disabled that target's composer after the key was
  gone, and there was no longer any control able to clear it.
- A recovery result belongs to the target that dispatched the key, never to the
  target that happens to be selected when a late answer arrives. `pendingOwner`
  records that target while the tab holds the key; `recover()` projects onto it
  (or onto the conversation the daemon names) and otherwise only at page level, so
  switching targets while a lookup is in flight cannot attribute A's result to B.
- The ordinary chat page is presentation-only: it renders the same
  AgentChatSection as the fixture and legacy live views. This controller projects
  the declared methods onto that view so the page no longer has a second chat
  layout (a target picker plus a conversation directory) beside it.
- Method wiring, in the order the page needs it:
  `targets` (authoritative chat-object directory; membership, kind and
  deliverable flag; the label is the row's displayName, otherwise the full
  targetUri), `conversations.list` (paged participant-scoped directory; a
  head names the document id and the send id), `message.query` inbox+outbox
  (additional discovery of send ids), `conversations.history` (the
  conversation document, only for a document this directory listed),
  `message.send` and `message.send.key_lookup` through the frozen ChatSendApi
  adapter.
- The chat reads the conversation document (slice G1, 2026-11-18). An entry
  keeps two ids: `conversationId`, the id `message.send` takes and the value
  of the session selector, and `documentId`, the `c.` id `conversations.history`
  takes. The document id comes only from a `conversations.list` head; it is
  never derived. It is looked up by peer and send id together: one send id
  can name a cell per peer (an older multi-target send), so the send id alone
  could open another peer's document. A send id with no listed head (a conversation `message.query`
  alone found, or one the directory has not listed yet) has no readable
  document: its history is `unread`, which claims neither an empty history nor
  a missing conversation, and sending into it still works.
- One peer can list two heads under one send id: a cell is (owner, thread,
  send id), and a row whose stored thread its two ends do not infer keeps its
  own cell (daemon conversations/read.py). The table keeps the head with the
  greatest `lastMessageAtMs` of each completed directory sweep, the first
  listed on a tie (the daemon lists newest first, then by `c.` id), so the
  result does not depend on page order; the session list keeps the same one
  head per (peer, send id), because the selector's value is the send id. The
  other cell is not reachable from this page (handoff 7.3).
- Opening a chat (select, a conversation switch, 刷新消息, a recovered send)
  is a user read. When its directory re-read fails, a refusal goes to
  `reportUnavailable` with the history-read subject (读取消息) and leaves the
  entry as a refused history read does; any other failure is the entry's
  `error` with the directory failure text, never `unread`: a failed read is
  not shown as a chat without messages (PR 2936 review 1). A head that is
  simply absent stays `unread`.
- A first message names no conversation. The daemon files it under the default
  conversation of the user and the target and returns that id, which the
  entry adopts; later sends reuse it. A send never promotes history: an entry
  whose document was not read stays `unread` (an `error` or `loading` entry
  keeps its status) with the local copy of the sent message, which the page
  shows under the 历史尚未读取，这里只显示你刚发送的消息。 line and without 暂无消息 or
  加载更多, so a client-made thread is not presented as the whole
  conversation (review 3). It becomes `ready` only when the document's tail
  is read; that read merges by message id, so the local copy is replaced, not
  duplicated, and the agent's messages in that conversation appear.
- Attaching that document is a background read with a budget (review 2): at
  most ATTACH_READS = 6 attempts, the first at acceptance and then on refresh
  ticks no earlier than 2, 4, 8, 16 and 32 s after the previous attempt
  (about one minute in all, at most 6 directory sweeps per accepted send
  instead of one every 2 s for as long as the page is open). The recorder
  normally lists a new conversation within one or two 2 s cycles, so six
  attempts over a minute cover a slow recorder; a head that is still absent
  after that will not appear by polling (for example a head whose peer is
  not the target URI). A refused directory or history method stops the
  attempts at once and silently, and a directory refused at start-up means
  none start; an older daemon whose catalog lacks the method is refused
  before dispatch, while a daemon that answers METHOD_NOT_FOUND after
  dispatch is an ordinary failure and is bounded the same way. Attempts are
  silent; after the last one the entry becomes `error` with the last failure
  text, or, when every sweep succeeded without the head, with
  会话目录里还没有这段会话，暂时读不到历史消息。可点“刷新消息”重试。 Opening the chat again is a user read
  and gives the attempts a new budget. An entry left in `error` stays until a
  user-caused read succeeds (刷新消息, reopening, selecting the conversation, a
  recovered send); no timer re-reads it. An accepted send in `error` is such a
  user-caused read: it runs the same loadHistory as reopening, exactly once
  per send, under the same budget and staleness fences. Sends in `ready`,
  `unread` and `loading` and sends that are not accepted read nothing new, and
  the sent row survives the re-read whatever its outcome.
- A background read whose result is discarded as stale gives its attempt back
  (directory lookup, tail read and tail failure alike). This covers the retry
  and initialize (directory restart) paths; leaving the chat needs no give-back
  because loadHistory resets the budget to { reads: 1 } on reopen.
- Reopening a conversation keeps the messages this tab sent (`delivery:
  'sent'`), including ones sent while the reopen was loading: every write after
  the await merges the current entry's sent rows with those taken before it, by
  id. A failed reopen therefore does not make a sent message vanish.
- A recovered acceptance names the stored send id. It selects the target that
  holds that id, else the directory head's peer, else the dispatching target
  when that target still has no conversation; otherwise it is only a notice.
  When several targets hold or list the stored id, the dispatching target wins.
- An open that still finds no document keeps the local sent messages of an
  entry that is waiting for its document, instead of clearing them.
- A history entry carrying `bodyOmitted` is shown as a message whose body was
  not recorded; the field, not an empty `message`, decides it. An entry with an
  empty `message` and no `bodyOmitted` renders as any other entry: the record
  answered no reason, so the page claims none.
  `recordAvailable: false` changes nothing on the page: the answer came from
  the local queue and is still the history (no indicator yet, KP-103).
- Nothing is selected automatically. With a trusted send capability, a target
  without a discovered conversation can start one; reads follow acceptance.
- The target directory is not the chat list: the view lists discovered conversations
  and explicitly opened targets. Opening an empty chat creates no daemon conversation.
- Conversation discovery selects the newest visible conversation. History pages
  consume daemon cursors only; a refresh or selection invalidates late pages.
- Reads are generation-fenced per target: selecting another target or starting a
  new directory read invalidates late responses. Failures carry the wire code and
  request id when the transport provides one; the page never invents a payload.
- Send results keep the #2441 invariants: one dispatch per intent, accepted means
  the daemon durably accepted the operation key (not peer delivery), unknown
  keeps the recovery key and never resends, and a refusal without a pending
  operation leaves the draft in place. A declared admission refusal
  (`refused`, e.g. AGENT_ADDRESS_RETIRED) is a definitive failure whose key the
  adapter already cleared, so it never marks the tab recovery-pending.
- Refusals before dispatch (PR 1f/F3, 2026-11-14): a `method-refused` send or a
  refused user-started history read (refresh, select, load-more) goes to
  `reportUnavailable` (the notification mechanism) with no inline text. A
  refused send leaves the draft and composer as they were; a refused history
  read leaves the previous history state and messages untouched, and when
  there was none the entry is `unread`, which renders no text and claims
  neither an empty history nor a missing conversation. A refused discovery
  read at start-up (`message.query`, `conversations.list`) writes no page
  notice and marks `conversationsUnknown`, so a target without a discovered
  conversation is shown without a conversation claim. A refused or failed
  target read is a visible load failure with a retry, never an empty list; the
  composition hands the target port down only when its method is callable. The
  target directory and message read ports are required here: the page does not
  mount the controller without them.
 */

import { useMemo } from 'react';
import { useEffect } from 'react';
import { ClientError, methodRefusal } from '../../../client';
import type { UnavailableDetail } from '../../../components/notifications';
import type { ConversationMessage, SessionTarget } from '../../../state/gui-view-model';
import type { OperationStatus } from '../../../state/operation-lifecycle';
import type { GuiCapabilities } from '../../../app/capabilities';
import {
  CHAT_SEND_PENDING_NOTICE,
  CHAT_SEND_UNKNOWN_NOTICE,
  isCanonicalAgentUri,
  isChatSendMethodRefused,
  type ChatSendApi,
} from './daemon-api';
import type { ConversationHead, ConversationKind, ConversationPage, GroupHead, HistoryPage, HistoryMode, QueryEntry } from './message-read-api';
import { sendSessionsFromMessages, type SendDirectoryTarget } from './send-session-selection';
import { agentTargetDisplayName } from '../../agent-chat';

export const CONNECTED_CHAT_RECOVERY_PENDING_NOTICE =
  '本标签页还有一个未结算的发送操作键；只能按键查询，不会自动重发。';
export const CONNECTED_CHAT_ABANDONED_NOTICE =
  '已放弃本地恢复键；本次没有重发，也不代表那次发送失败。';


export interface ConnectedChatTargetRow extends SendDirectoryTarget {
  readonly actor: string | null;
  /** Local agent rows only (UUID addressing): stable id and registry display name. */
  readonly agentId?: string | null;
  readonly displayName?: string | null;
  readonly status?: string;
}

export interface ConnectedChatPort {
  readonly targets: (kind: 'agent') => Promise<readonly ConnectedChatTargetRow[]>;
  readonly query: (view: 'inbox' | 'outbox') => Promise<readonly QueryEntry[]>;
  readonly conversations?: (cursor?: string, kinds?: readonly ConversationKind[]) => Promise<ConversationPage>;
  readonly includeGroups?: boolean;
  readonly history?: (documentId: string, cursor?: string, mode?: HistoryMode) => Promise<HistoryPage>;
  readonly send?: ChatSendApi;
  /** Raised when the user operates a read or send that the composition refused before dispatch. */
  readonly reportUnavailable?: (subject: string, detail: UnavailableDetail) => void;
  /** Trusted host identity; used only to label the reader's own messages. */
  readonly identity?: string;
}

export interface ConnectedChatOperation {
  readonly targetId: string;
  readonly requestId: string;
  readonly status: OperationStatus;
  readonly canRetry: false;
  readonly error?: string;
}

export interface ConnectedChatEntry {
  /** The id `message.send` takes; also the session selector's value. */
  readonly conversationId: string | null;
  /** The conversation document (`c.` id) history is read from; null until a directory head names it. */
  readonly documentId?: string | null;
  readonly historyStatus: 'loading' | 'ready' | 'error' | 'unconfirmed' | 'new' | 'unread';
  readonly historyError?: string;
  readonly messages: readonly ConversationMessage[];
  readonly nextCursor?: string | null;
  readonly headCursor?: string | null;
  readonly loadingMore?: boolean;
  readonly operation?: ConnectedChatOperation;
  /** Exact wire copy for the active dispatch; never a claim of delivery. */
  readonly notice?: string;
}

export interface ConnectedChatSnapshot {
  readonly status: 'loading' | 'ready' | 'error';
  readonly error?: string;
  /** Page-level notice outside the selected object (directory/recovery). */
  readonly notice?: string;
  readonly targets: readonly SessionTarget[];
  readonly sessions: readonly ConversationHead[];
  readonly groups: readonly GroupHead[];
  readonly groupsUnknown: boolean;
  readonly conversationTargetIds: readonly string[];
  readonly activeTargetId: string | null;
  /** A tab-local operation key is still unsettled; only a key lookup may close it. */
  readonly recoveryPending: boolean;
  /** A start-up discovery read was refused: a target without a conversation is not known to have none. */
  readonly conversationsUnknown: boolean;
  readonly drafts: Readonly<Record<string, string>>;
  readonly entries: Readonly<Record<string, ConnectedChatEntry>>;
}

const EMPTY_ENTRY: ConnectedChatEntry = {
  conversationId: null, historyStatus: 'unconfirmed', messages: [],
};

const ATTACH_READS = 6;
const ATTACH_FIRST_DELAY_MS = 2_000;

type DocumentLookup =
  | { readonly status: 'found'; readonly documentId: string }
  | { readonly status: 'absent' }
  | { readonly status: 'failed'; readonly error: unknown }
  | { readonly status: 'stale' };

interface AttachBudget { reads: number; dueAtMs: number }

function documentKey(peer: string, conversationId: string): string {
  return JSON.stringify([peer, conversationId]);
}

function newestByKey<T>(items: readonly T[], keyOf: (item: T) => string, timeOf: (item: T) => number): Map<string, T> {
  const newest = new Map<string, T>();
  for (const item of items) {
    const key = keyOf(item);
    const known = newest.get(key);
    if (!known || timeOf(item) > timeOf(known)) newest.set(key, item);
  }
  return newest;
}

function failureText(error: unknown): string {
  if (error instanceof ClientError) {
    const requestId = error.diagnostics.requestId;
    return requestId ? `${error.code}（请求 ${requestId}）` : error.code;
  }
  return error instanceof Error && error.message ? error.message : '读取失败';
}

/** Local state that exists only because the tab still held the operation key. */
function heldByAbandonedKey(entry: ConnectedChatEntry): boolean {
  if (entry.operation?.status === 'unknown') return true;
  return entry.operation?.status === 'not-sent'
    && entry.operation.error === CHAT_SEND_PENDING_NOTICE;
}

function targetOf(row: ConnectedChatTargetRow): SessionTarget {
  return {
    id: row.targetUri,
    agentId: row.agentId,
    kind: 'agent',
    displayName: agentTargetDisplayName(row),
    role: row.targetUri,
    availability: row.status === 'online' ? 'available' : 'needs-connection',
    avatarTone: 'green',
    capabilities: [],
  };
}

function authorOf(from: string, identity?: string): ConversationMessage['author'] {
  if (identity) return from === identity ? 'user' : 'assistant';
  return from.startsWith('user:') ? 'user' : 'assistant';
}

function messageOf(
  entry: HistoryPage['entries'][number],
  targetId: string,
  identity?: string,
): ConversationMessage {
  const system = entry.messageClass === 'system';
  return {
    id: entry.messageId, targetId, author: system ? 'system' : authorOf(entry.from, identity), text: entry.message,
    createdAt: new Date(entry.createdAtMs).toISOString(),
    ...(entry.bodyOmitted === undefined ? {} : { bodyNotRecorded: true }),
  };
}

function mergeMessages(
  current: readonly ConversationMessage[],
  entries: HistoryPage['entries'],
  targetId: string,
  identity?: string,
): ConversationMessage[] {
  const messages = new Map(current.map(message => [message.id, message]));
  for (const item of entries) messages.set(item.messageId, messageOf(item, targetId, identity));
  return [...messages.values()].sort((a, b) => (a.createdAt ?? '').localeCompare(b.createdAt ?? '') || a.id.localeCompare(b.id));
}

/** Owns chat read/send projection; components stay controlled and stateless. */
export class ConnectedChatController {
  private snapshot: ConnectedChatSnapshot = {
    status: 'loading', targets: [], sessions: [], groups: [], groupsUnknown: false, conversationTargetIds: [], activeTargetId: null, drafts: {}, entries: {}, recoveryPending: false,
    conversationsUnknown: false,
  };
  private listeners = new Set<() => void>();
  private started?: Promise<void>;
  private startEpoch = 0;
  private historyEpoch = 0;
  private recoveryEpoch = 0;
  /** Target whose dispatch left the tab holding the operation key, if this tab sent it. */
  private pendingOwner: string | null = null;
  /** The page notice currently shown was produced by key recovery, so settling clears it. */
  private recoveryOwnsNotice = false;
  private conversations = new Map<string, string>();
  /** (peer, send id) → document id, filled only from `conversations.list` heads. */
  private documents = new Map<string, string>();
  private eligible: readonly ConnectedChatTargetRow[] = [];
  private refreshing = false;
  /** (peer, send id) → attach budget of an entry waiting for its document. */
  private attaching = new Map<string, AttachBudget>();
  private directoryRefused = false;

  constructor(private readonly port: ConnectedChatPort) {}

  getSnapshot = (): ConnectedChatSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  start(): Promise<void> {
    return (this.started ??= this.initialize());
  }

  /** Re-runs the directory read after a visible failure; never a silent retry. */
  retry(): Promise<void> {
    this.started = this.initialize();
    return this.started;
  }

  private update(change: Partial<ConnectedChatSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...change };
    this.listeners.forEach(listener => listener());
  }

  private entry(targetId: string, change: Partial<ConnectedChatEntry>): void {
    this.update({
      entries: {
        ...this.snapshot.entries,
        [targetId]: { ...(this.snapshot.entries[targetId] ?? EMPTY_ENTRY), ...change },
      },
    });
  }

  private async initialize(): Promise<void> {
    const epoch = ++this.startEpoch;
    this.historyEpoch += 1;
    this.update({ status: 'loading', error: undefined, conversationsUnknown: false });
    this.conversations.clear();
    this.documents.clear();
    this.directoryRefused = false;
    this.eligible = [];
    const { targets, query, send } = this.port;
    let rows: readonly ConnectedChatTargetRow[];
    try {
      rows = await targets('agent');
    } catch (error) {
      if (epoch !== this.startEpoch) return;
      this.update({
        status: 'error', targets: [],
        error: `聊天对象读取失败：${failureText(error)}`,
      });
      return;
    }
    if (epoch !== this.startEpoch) return;
    const eligible = rows.filter(row => row.targetKind === 'agent' && row.deliverable
      && isCanonicalAgentUri(row.targetUri));
    this.eligible = eligible;
    this.update({ targets: eligible.map(targetOf), error: undefined, sessions: [], groups: [], groupsUnknown: false, conversationTargetIds: [] });

    let notice: string | undefined;
    let unknown = false;
    try {
      const [inbox, outbox] = await Promise.all([query('inbox'), query('outbox')]);
      if (epoch !== this.startEpoch) return;
      for (const session of sendSessionsFromMessages([...inbox, ...outbox], eligible)) {
        if (!this.conversations.has(session.targetUri)) {
          this.conversations.set(session.targetUri, session.conversationId);
        }
      }
    } catch (error) {
      if (epoch !== this.startEpoch) return;
      if (methodRefusal(error) === null) notice = `最近会话读取失败：${failureText(error)}`;
      else unknown = true;
    }
    if (this.port.conversations) {
      try {
        const dir = await this.readDirectory(() => epoch === this.startEpoch);
        if (dir === null || epoch !== this.startEpoch) return;
        this.applyDirectory(dir);
      } catch (error) {
        if (epoch !== this.startEpoch) return;
        if (methodRefusal(error) === null) {
          notice = [notice, `会话目录读取失败：${failureText(error)}`].filter(Boolean).join(' ');
        } else {
          unknown = true;
          this.directoryRefused = true;
        }
        if (this.port.includeGroups) this.update({ groupsUnknown: true });
      }
    }
    if (epoch !== this.startEpoch) return;
    this.update({ status: 'ready', notice, conversationsUnknown: unknown, conversationTargetIds: [...this.conversations.keys()] });
    this.update({ recoveryPending: send !== undefined && send.pendingOperationKey() !== null });
    if (send) await this.recover(epoch);
  }

  private applyDirectory(dir: { readonly sessions: ConversationHead[]; readonly groups: GroupHead[]; readonly groupsUnknown: boolean }, withTargets = false): void {
    const selected = new Set<string>();
    for (const session of dir.sessions) {
      if (!selected.has(session.peer)) this.conversations.set(session.peer, session.conversationId);
      selected.add(session.peer);
    }
    this.update({ sessions: dir.sessions, groups: dir.groups, groupsUnknown: dir.groupsUnknown,
      ...(withTargets ? { conversationTargetIds: [...this.conversations.keys()] } : {}) });
  }

  /** Reads every `conversations.list` page; null when `fresh` turned false meanwhile. */
  private async readDirectory(fresh: () => boolean): Promise<{ readonly sessions: ConversationHead[]; readonly groups: GroupHead[]; readonly groupsUnknown: boolean } | null> {
    const list = this.port.conversations;
    if (!list) return { sessions: [], groups: [], groupsUnknown: false };
    const entries: ConversationPage['entries'][number][] = [];
    const groupEntries: GroupHead[] = [];
    let recordUnavailable = false;
    const cursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = this.port.includeGroups ? await list(cursor, ['direct', 'group']) : await list(cursor);
      if (!fresh()) return null;
      entries.push(...page.entries);
      if (page.groups) groupEntries.push(...page.groups);
      if (page.recordAvailable === false) recordUnavailable = true;
      cursor = page.nextCursor ?? undefined;
      if (cursor && (cursors.has(cursor) || cursors.size >= 100)) throw new Error('会话目录分页未完成，请刷新');
      if (cursor) cursors.add(cursor);
    } while (cursor);
    const newest = newestByKey(entries, head => documentKey(head.peer, head.conversationId), head => head.lastMessageAtMs);
    for (const [key, head] of newest) this.documents.set(key, head.documentId);
    const groupMap = newestByKey(groupEntries, group => group.conversationId, group => group.lastMessageAtMs);
    return {
      sessions: [...newest.values()].filter(row => this.eligible.some(target => target.targetUri === row.peer)).sort((a, b) => b.lastMessageAtMs - a.lastMessageAtMs),
      groups: [...groupMap.values()].sort((a, b) => b.lastMessageAtMs - a.lastMessageAtMs),
      groupsUnknown: Boolean(this.port.includeGroups) && recordUnavailable && groupMap.size === 0,
    };
  }

  private knownDocument(targetId: string, conversationId: string): string | null {
    return this.documents.get(documentKey(targetId, conversationId)) ?? null;
  }

  /** The document of a target's send id; re-reads the directory once when it is not known yet. */
  private async documentOf(targetId: string, conversationId: string): Promise<DocumentLookup> {
    const known = this.knownDocument(targetId, conversationId);
    if (known) return { status: 'found', documentId: known };
    if (!this.port.conversations) return { status: 'absent' };
    const epoch = this.startEpoch;
    try {
      const dir = await this.readDirectory(() => epoch === this.startEpoch);
      if (dir === null) return { status: 'stale' };
      this.update({ sessions: dir.sessions, groups: dir.groups, groupsUnknown: dir.groupsUnknown });
    } catch (error) {
      if (methodRefusal(error) !== null) this.directoryRefused = true;
      return { status: 'failed', error };
    }
    const found = this.knownDocument(targetId, conversationId);
    return found ? { status: 'found', documentId: found } : { status: 'absent' };
  }

  /** A recovery result may only be projected onto a target the directory still declares. */
  private knownTarget(targetId: string): boolean {
    return this.snapshot.targets.some(target => target.id === targetId);
  }

  /** Re-reads only the stored operation key; it never dispatches a send. */
  async recover(epoch = this.startEpoch): Promise<void> {
    const send = this.port.send;
    if (!send) return;
    const generation = ++this.recoveryEpoch;
    const result = await send.recover().catch(() => undefined);
    if (!result || epoch !== this.startEpoch || generation !== this.recoveryEpoch) return;
    if (result.status === 'idle') {
      this.pendingOwner = null;
      this.update({
        recoveryPending: false,
        ...(this.recoveryOwnsNotice ? { notice: undefined } : {}),
      });
      this.recoveryOwnsNotice = false;
      return;
    }
    if (result.status === 'accepted') {
      const owner = this.pendingOwner;
      this.pendingOwner = null;
      this.update({
        recoveryPending: false,
        ...(this.recoveryOwnsNotice ? { notice: undefined } : {}),
      });
      this.recoveryOwnsNotice = false;
      const holders = [...this.conversations.entries()]
        .filter(([, conversationId]) => conversationId === result.conversationId).map(([targetId]) => targetId);
      const listed = this.snapshot.sessions
        .filter(row => row.conversationId === result.conversationId).map(row => row.peer);
      const unstarted = owner !== null && (this.snapshot.entries[owner]?.conversationId ?? null) === null
        ? [owner] : [];
      const candidates = [...holders, ...listed, ...unstarted].filter(id => this.knownTarget(id));
      const target = candidates.find(id => id === owner) ?? candidates[0];
      if (target) {
        this.conversations.set(target, result.conversationId);
        this.attaching.set(documentKey(target, result.conversationId), { reads: 0, dueAtMs: 0 });
        this.select(target);
        this.entry(target, {
          operation: {
            targetId: target, requestId: result.operationKey,
            status: 'success', canRetry: false,
          },
          notice: result.notice,
        });
        return;
      }
      if (owner && this.knownTarget(owner)) this.entry(owner, { notice: result.notice });
      else this.update({ notice: result.notice });
      return;
    }
    if (result.status === 'unknown') {
      this.recoveryOwnsNotice = true;
      this.update({ recoveryPending: true, notice: result.notice });
      if (this.pendingOwner && this.knownTarget(this.pendingOwner)) {
        this.entry(this.pendingOwner, { notice: result.notice });
      }
    }
  }

  /** Explicit owner cancel of the tab-local key; it never claims the send failed. */
  abandon(): void {
    this.recoveryEpoch += 1;
    this.pendingOwner = null;
    this.recoveryOwnsNotice = false;
    this.port.send?.clear('cancelled');
    const released: Record<string, ConnectedChatEntry> = {};
    for (const [targetId, entry] of Object.entries(this.snapshot.entries)) {
      if (!heldByAbandonedKey(entry)) continue;
      released[targetId] = {
        ...entry, operation: undefined, notice: CONNECTED_CHAT_ABANDONED_NOTICE,
      };
    }
    this.update({
      recoveryPending: false,
      notice: CONNECTED_CHAT_ABANDONED_NOTICE,
      ...(Object.keys(released).length > 0
        ? { entries: { ...this.snapshot.entries, ...released } } : {}),
    });
  }

  private canStartConversation(): boolean {
    return this.port.send !== undefined
      && typeof this.port.identity === 'string'
      && this.port.identity.length > 0;
  }

  private unstartedStatus(): 'new' | 'unconfirmed' | 'unread' {
    if (this.snapshot.conversationsUnknown) return 'unread';
    return this.canStartConversation() ? 'new' : 'unconfirmed';
  }

  deselect(): void {
    this.historyEpoch += 1;
    if (this.snapshot.activeTargetId !== null) this.update({ activeTargetId: null });
  }

  async refreshDirectory(): Promise<void> {
    if (!this.port.conversations) return;
    const epoch = this.startEpoch;
    try {
      const dir = await this.readDirectory(() => epoch === this.startEpoch);
      if (dir !== null && epoch === this.startEpoch) this.applyDirectory(dir, true);
    } catch (error) {
      if (epoch !== this.startEpoch) return;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        this.directoryRefused = true;
        this.port.reportUnavailable?.('读取消息', refusal);
      }
      if (this.port.includeGroups) this.update({ groupsUnknown: true });
    }
  }

  select(targetId: string): void {
    if (!this.snapshot.targets.some(target => target.id === targetId)) return;
    this.update({ activeTargetId: targetId });
    const conversationId = this.conversations.get(targetId) ?? null;
    if (conversationId) {
      void this.loadHistory(targetId, conversationId);
      return;
    }
    this.historyEpoch += 1;
    this.entry(targetId, {
      conversationId: null, documentId: null,
      historyStatus: this.unstartedStatus(),
      historyError: undefined, messages: [], nextCursor: null, loadingMore: false,
    });
  }

  selectConversation(targetId: string, conversationId: string): void {
    if (!this.snapshot.sessions.some(row => row.peer === targetId && row.conversationId === conversationId)) return;
    this.conversations.set(targetId, conversationId);
    this.select(targetId);
  }

  setDraft = (targetId: string, value: string): void => {
    this.update({ drafts: { ...this.snapshot.drafts, [targetId]: value } });
  };

  async send(targetId: string, text: string): Promise<void> {
    const send = this.port.send;
    const entry = this.snapshot.entries[targetId] ?? EMPTY_ENTRY;
    if (!send || !this.port.identity) return;
    if (!this.snapshot.targets.some(target => target.id === targetId)) return;
    if (entry.operation?.status === 'in-flight') return;
    if (text.trim().length === 0) return;
    const conversationId = entry.conversationId;
    const previousStatus = entry.operation?.status;
    this.entry(targetId, {
      operation: { targetId, requestId: '', status: 'in-flight', canRetry: false },
      notice: undefined,
    });
    const outcome = await send.send({
      to: [targetId], message: text, ...(conversationId === null ? {} : { conversationId }),
    });
    if (!this.snapshot.targets.some(target => target.id === targetId)) return;
    if ((this.snapshot.entries[targetId]?.conversationId ?? null) !== conversationId) return;
    if (outcome.status === 'accepted') {
      const adopted = outcome.conversationId;
      const current = this.snapshot.entries[targetId] ?? EMPTY_ENTRY;
      const read = current.conversationId === adopted && typeof current.documentId === 'string';
      const kept = current.conversationId === adopted
        && (current.historyStatus === 'error' || current.historyStatus === 'loading');
      const historyStatus = read && current.historyStatus === 'ready' ? 'ready' : kept ? current.historyStatus : 'unread';
      const waiting = historyStatus === 'unread';
      const rereads = current.conversationId === adopted && current.historyStatus === 'error';
      this.pendingOwner = null;
      this.conversations.set(targetId, adopted);
      if (waiting) this.attaching.set(documentKey(targetId, adopted), { reads: 0, dueAtMs: 0 });
      this.update({ recoveryPending: false });
      this.entry(targetId, {
        conversationId: adopted,
        documentId: read ? current.documentId : this.knownDocument(targetId, adopted),
        historyStatus,
        operation: {
          targetId, requestId: outcome.operationKey, status: 'success', canRetry: false,
        },
        notice: undefined,
        messages: [...(this.snapshot.entries[targetId]?.messages ?? []), {
          id: outcome.messageId, targetId, author: 'user', createdAt: new Date().toISOString(),
          text, delivery: 'sent',
        }],
      });
      this.update({ drafts: { ...this.snapshot.drafts, [targetId]: '' } });
      if (waiting && this.snapshot.activeTargetId === targetId) void this.refresh();
      if (rereads && this.snapshot.activeTargetId === targetId) void this.loadHistory(targetId, adopted);
      return;
    }
    if (isChatSendMethodRefused(outcome)) {
      this.entry(targetId, {
        operation: { targetId, requestId: '', status: 'not-sent', canRetry: false },
        notice: undefined,
      });
      this.port.reportUnavailable?.('发送消息', { code: outcome.code, source: outcome.source });
      return;
    }
    if (outcome.reason === 'pending-operation' && previousStatus === 'unknown') {
      this.entry(targetId, { operation: entry.operation, notice: outcome.notice });
      return;
    }
    if (outcome.reason === 'pending-operation'
      && this.snapshot.entries[targetId]?.operation?.status === 'unknown') {
      this.entry(targetId, { notice: outcome.notice });
      return;
    }
    if (outcome.status === 'refused') {
      this.entry(targetId, {
        operation: {
          targetId, requestId: outcome.operationKey, status: 'definitive-failure',
          canRetry: false, error: outcome.notice,
        },
        notice: outcome.notice,
      });
      return;
    }
    if (outcome.status === 'unknown') {
      this.pendingOwner = targetId;
      this.update({ recoveryPending: true });
      this.entry(targetId, {
        operation: {
          targetId, requestId: outcome.operationKey, status: 'unknown',
          canRetry: false, error: outcome.notice,
        },
        notice: CHAT_SEND_UNKNOWN_NOTICE,
      });
      return;
    }
    this.entry(targetId, {
      operation: {
        targetId, requestId: '', status: outcome.reason === 'store-failed'
          || outcome.reason === 'capability-unavailable'
          ? 'definitive-failure' : 'not-sent',
        canRetry: false, error: outcome.notice,
      },
      notice: outcome.notice,
    });
  }

  private async loadHistory(targetId: string, conversationId: string): Promise<void> {
    const history = this.port.history;
    const epoch = ++this.historyEpoch;
    const prior = this.snapshot.entries[targetId];
    if (!history) {
      this.entry(targetId, { conversationId, documentId: null, historyStatus: 'unconfirmed', messages: [], nextCursor: null, loadingMore: false });
      return;
    }
    this.entry(targetId, {
      conversationId, documentId: null, historyStatus: 'loading', historyError: undefined, messages: [],
      nextCursor: null, headCursor: null, loadingMore: false,
    });
    const key = documentKey(targetId, conversationId);
    const budget = this.attaching.get(key);
    const local = prior?.conversationId === conversationId
      ? prior.messages.filter(row => row.delivery === 'sent') : [];
    const sentRows = (): ConversationMessage[] => {
      const ids = new Set(local.map(row => row.id));
      const now = this.snapshot.entries[targetId];
      const added = now?.conversationId === conversationId
        ? now.messages.filter(row => row.delivery === 'sent' && !ids.has(row.id)) : [];
      return [...local, ...added];
    };
    const restored = (kept: ConnectedChatEntry): ConnectedChatEntry => {
      const ids = new Set(kept.messages.map(row => row.id));
      return {
        ...kept, loadingMore: false,
        messages: [...kept.messages, ...sentRows().filter(row => !ids.has(row.id))],
      };
    };
    const known = this.knownDocument(targetId, conversationId);
    const lookup: DocumentLookup = known ? { status: 'found', documentId: known } : await this.documentOf(targetId, conversationId);
    if (epoch !== this.historyEpoch || lookup.status === 'stale') return;
    if (lookup.status !== 'found') {
      if (budget) this.attaching.set(key, { reads: 1, dueAtMs: Date.now() + ATTACH_FIRST_DELAY_MS });
      const refusal = lookup.status === 'failed' ? methodRefusal(lookup.error) : null;
      if (lookup.status === 'failed' && refusal === null) {
        this.entry(targetId, {
          conversationId, historyStatus: 'error', messages: sentRows(),
          historyError: `会话目录读取失败：${failureText(lookup.error)}`,
        });
        return;
      }
      this.entry(targetId, refusal !== null && budget === undefined
        && prior?.conversationId === conversationId && prior.historyStatus !== 'loading'
        ? restored(prior)
        : { conversationId, historyStatus: 'unread', messages: sentRows() });
      if (refusal !== null) this.port.reportUnavailable?.('读取消息', refusal);
      return;
    }
    const documentId = lookup.documentId;
    this.entry(targetId, { documentId });
    try {
      const page = await history(documentId, undefined, 'tail');
      if (epoch !== this.historyEpoch) return;
      this.attaching.delete(key);
      this.entry(targetId, {
        conversationId, historyStatus: 'ready',
        nextCursor: page.nextOlderCursor ?? page.nextCursor, headCursor: page.headCursor,
        messages: mergeMessages(sentRows(), page.entries, targetId, this.port.identity),
      });
    } catch (error) {
      if (epoch !== this.historyEpoch) return;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        this.entry(targetId, prior?.conversationId === conversationId && prior.historyStatus !== 'loading'
          ? restored(prior)
          : { conversationId, historyStatus: 'unread', historyError: undefined, messages: sentRows(), nextCursor: null, loadingMore: false });
        this.port.reportUnavailable?.('读取消息', refusal);
        return;
      }
      this.entry(targetId, {
        conversationId, historyStatus: 'error', messages: sentRows(),
        historyError: `历史消息读取失败：${failureText(error)}`,
      });
    }
  }

  async refresh(): Promise<void> {
    const targetId = this.snapshot.activeTargetId;
    const entry = targetId ? this.snapshot.entries[targetId] : undefined;
    const history = this.port.history;
    if (this.refreshing || !targetId || !history || !entry?.conversationId || entry.loadingMore) return;
    const conversationId = entry.conversationId;
    const budget = entry.historyStatus === 'unread' ? this.attaching.get(documentKey(targetId, conversationId)) : undefined;
    if (budget) {
      this.refreshing = true;
      try { await this.attach(targetId, conversationId, budget, history); } finally { this.refreshing = false; }
      return;
    }
    if (entry.historyStatus !== 'ready' || !entry.documentId) return;
    const documentId = entry.documentId;
    const epoch = this.historyEpoch;
    this.refreshing = true;
    try {
      const headCursor = entry.headCursor ?? null;
      const page = await history(documentId, headCursor ?? undefined, headCursor ? 'after' : 'tail');
      if (epoch !== this.historyEpoch || this.snapshot.activeTargetId !== targetId) return;
      const current = this.snapshot.entries[targetId];
      if (current?.conversationId !== conversationId) return;
      this.entry(targetId, {
        messages: mergeMessages(current.messages, page.entries, targetId, this.port.identity),
        headCursor: headCursor ? page.nextCursor ?? page.headCursor ?? headCursor : page.headCursor ?? null,
        historyError: undefined,
      });
    } catch (error) {
      if (epoch === this.historyEpoch && this.snapshot.activeTargetId === targetId) {
        this.entry(targetId, { historyError: `消息更新失败：${failureText(error)}` });
      }
    } finally { this.refreshing = false; }
  }

  /** One budgeted background attempt to find and read the document of an entry waiting for it. */
  private async attach(
    targetId: string,
    conversationId: string,
    budget: AttachBudget,
    history: NonNullable<ConnectedChatPort['history']>,
  ): Promise<void> {
    const key = documentKey(targetId, conversationId);
    if (budget.reads >= ATTACH_READS || Date.now() < budget.dueAtMs) return;
    if (this.directoryRefused && this.knownDocument(targetId, conversationId) === null) {
      this.attaching.delete(key);
      return;
    }
    const epoch = this.historyEpoch;
    const fresh = () => epoch === this.historyEpoch && this.snapshot.activeTargetId === targetId
      && this.snapshot.entries[targetId]?.conversationId === conversationId;
    budget.reads += 1;
    const lookup = await this.documentOf(targetId, conversationId);
    if (lookup.status === 'stale' || !fresh()) { budget.reads -= 1; return; }
    let failure: string | null = null;
    if (lookup.status === 'failed') {
      if (methodRefusal(lookup.error) !== null) { this.attaching.delete(key); return; }
      failure = `会话目录读取失败：${failureText(lookup.error)}`;
    }
    if (lookup.status === 'found') {
      try {
        const page = await history(lookup.documentId, undefined, 'tail');
        if (!fresh()) { budget.reads -= 1; return; }
        this.attaching.delete(key);
        this.entry(targetId, {
          documentId: lookup.documentId, historyStatus: 'ready', historyError: undefined,
          messages: mergeMessages(this.snapshot.entries[targetId].messages, page.entries, targetId, this.port.identity),
          headCursor: page.headCursor ?? null, nextCursor: page.nextOlderCursor ?? page.nextCursor,
        });
        return;
      } catch (error) {
        if (!fresh()) { budget.reads -= 1; return; }
        if (methodRefusal(error) !== null) { this.attaching.delete(key); return; }
        failure = `历史消息读取失败：${failureText(error)}`;
      }
    }
    if (budget.reads < ATTACH_READS) {
      budget.dueAtMs = Date.now() + ATTACH_FIRST_DELAY_MS * 2 ** (budget.reads - 1);
      return;
    }
    this.entry(targetId, {
      historyStatus: 'error',
      historyError: failure ?? `会话目录里还没有这段会话，暂时读不到历史消息。可点“刷新消息”重试。`,
    });
  }

  async loadMore(targetId: string): Promise<void> {
    const history = this.port.history;
    const entry = this.snapshot.entries[targetId];
    if (!history || this.snapshot.activeTargetId !== targetId || !entry?.documentId
      || !entry.nextCursor || entry.loadingMore || entry.historyStatus !== 'ready') return;
    const epoch = this.historyEpoch;
    const cursor = entry.nextCursor;
    this.entry(targetId, { loadingMore: true, historyError: undefined });
    try {
      const page = await history(entry.documentId, cursor, 'older');
      if (epoch !== this.historyEpoch) return;
      const messages = this.snapshot.entries[targetId].messages;
      const ids = new Set(messages.map(message => message.id));
      this.entry(targetId, {
        loadingMore: false,
        nextCursor: (page.nextOlderCursor ?? page.nextCursor) === cursor ? null : (page.nextOlderCursor ?? page.nextCursor),
        messages: [...page.entries.filter(message => !ids.has(message.messageId))
          .map(message => messageOf(message, targetId, this.port.identity)), ...messages],
      });
    } catch (error) {
      if (epoch !== this.historyEpoch) return;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        this.entry(targetId, { loadingMore: false });
        this.port.reportUnavailable?.('读取消息', refusal);
        return;
      }
      this.entry(targetId, { loadingMore: false, historyError: `历史消息读取失败：${failureText(error)}` });
    }
  }
}

/** Builds one controller per injected capability generation. */
export function useConnectedChat(
  capabilities: GuiCapabilities,
  ports: { readonly chatTargets: NonNullable<GuiCapabilities['chatTargets']>; readonly messageRead: NonNullable<GuiCapabilities['messageRead']> },
  reportUnavailable?: ConnectedChatPort['reportUnavailable'],
): ConnectedChatController {
  const { chatTargets, messageRead } = ports;
  const controller = useMemo(() => new ConnectedChatController({
    targets: chatTargets,
    query: view => messageRead.query(view),
    conversations: (cursor, kinds) => messageRead.conversations(cursor, kinds),
    includeGroups: capabilities.groupChatRead !== undefined,
    history: (documentId, cursor, mode) => messageRead.history(documentId, cursor, mode),
    send: capabilities.chatSend,
    identity: capabilities.chatSession?.actor,
    reportUnavailable,
  }), [chatTargets, messageRead, capabilities.groupChatRead, capabilities.chatSend,
    capabilities.chatSession?.actor, reportUnavailable]);
  useEffect(() => {
    void controller.start();
    let cancelled = false;
    let reading = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      clearTimeout(timer);
      if (cancelled || reading || document.visibilityState === 'hidden') return;
      timer = setTimeout(async () => {
        reading = true;
        try { await controller.refresh(); }
        finally { reading = false; schedule(); }
      }, 2000);
    };
    schedule();
    document.addEventListener('visibilitychange', schedule);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', schedule);
    };
  }, [controller]);
  return controller;
}
