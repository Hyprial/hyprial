import type { SessionTarget, ConversationMessage } from '../../../state/gui-view-model';
import type { OperationStatus } from '../../../state/operation-lifecycle';
import { array, object, string, LiveError, type LivePort } from '../../../adapters/live/client';

export interface ChatIntent {
  readonly targetId: string;
  readonly requestId: string;
  readonly text: string;
  readonly status: OperationStatus;
  readonly canRetry: boolean;
  readonly error?: string;
  readonly confirmationPending?: boolean;
  readonly acceptedMessageId?: string;
}
export interface ChatEntry {
  readonly sessionId?: string;
  readonly conversationId?: string;
  readonly messages: readonly ConversationMessage[];
  readonly historyStatus: 'loading' | 'ready' | 'error';
  readonly historyError?: string;
  readonly partial?: boolean;
  readonly operation?: ChatIntent;
}
export interface ChatSnapshot {
  readonly status: 'loading' | 'ready' | 'error';
  readonly error?: string;
  readonly targets: readonly SessionTarget[];
  readonly activeTargetId: string | null;
  readonly drafts: Readonly<Record<string, string>>;
  readonly entries: Readonly<Record<string, ChatEntry>>;
}
const emptyEntry = (): ChatEntry => ({ messages: [], historyStatus: 'loading' });
const errorText = (error: unknown) => error instanceof LiveError ? error.message : '服务状态尚未确认。';

export class ChatController {
  private snapshot: ChatSnapshot = { status: 'loading', targets: [], activeTargetId: null, drafts: {}, entries: {} };
  private listeners = new Set<() => void>();
  private startPromise?: Promise<void>;
  private reads = new Set<string>();
  private historyStarts = new Map<string, string | undefined>();
  private lookups = new Set<string>();
  private confirmations = new Set<string>();
  private confirmationCounts = new Map<string, number>();
  private rescanRequested = new Set<string>();
  private sessions = new Map<string, Promise<ChatEntry>>();
  private lookupCounts = new Map<string, number>();
  constructor(private readonly port: LivePort, private readonly key: () => string = () => crypto.randomUUID()) {}
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(change: Partial<ChatSnapshot>) {
    this.snapshot = { ...this.snapshot, ...change };
    this.listeners.forEach((listener) => listener());
  }
  private entry(targetId: string, change: Partial<ChatEntry>) {
    this.update({ entries: { ...this.snapshot.entries, [targetId]: { ...(this.snapshot.entries[targetId] ?? emptyEntry()), ...change } } });
  }
  start(): Promise<void> { return this.startPromise ??= this.initialize(); }
  private async initialize() {
    try {
      const directory = object(await this.port.call('targets.list'));
      const seen = new Set<string>();
      const targets = array(directory.targets).map((raw): SessionTarget => {
        const row = object(raw); const id = string(row.targetUri); string(row.status);
        if (!id.startsWith('agent:') || seen.has(id)) throw new LiveError('invalid-response', '聊天对象列表无法确认。');
        seen.add(id);
        return { id, kind: 'agent', displayName: typeof row.displayName === 'string' ? row.displayName : id.split(':').at(-1)!,
          role: id, availability: row.status === 'online' ? 'available' : 'needs-connection',
          avatarTone: 'green', capabilities: [] };
      });
      // Recovery must finish before the first fresh mutation is enabled.
      const pending = object(await this.port.call('sessions.pending'));
      for (const raw of array(pending.requests)) {
        const row = object(raw); const targetId = string(row.targetUri); const request = object(row.request);
        const requestId = string(request.idempotencyKey); const text = string(request.text);
        if (row.status !== 'unknown' || this.snapshot.entries[targetId]?.operation) throw new LiveError('invalid-response', '待确认操作无法恢复。');
        if (!seen.has(targetId)) {
          seen.add(targetId); targets.push({ id: targetId, kind: 'agent', displayName: targetId.split(':').at(-1)!, role: '待确认操作',
            availability: 'needs-connection', avatarTone: 'grey', capabilities: [] });
        }
        this.entry(targetId, { sessionId: string(row.sessionId), operation: { targetId, requestId, text, status: 'unknown', canRetry: false } });
        this.setDraft(targetId, text);
      }
      const preferred = directory.preferredTargetUri;
      if (preferred !== null && preferred !== undefined && (typeof preferred !== 'string' ||
        !targets.some((target) => target.id === preferred))) {
        throw new LiveError('invalid-response', '个人 Squire 地址无法确认。');
      }
      const preferredTargetId = typeof preferred === 'string' ? preferred : null;
      this.update({ targets, status: 'ready', activeTargetId: preferredTargetId });
      if (preferredTargetId) await this.select(preferredTargetId);
    } catch (error) { this.update({ status: 'error', error: errorText(error) }); }
  }
  setDraft(targetId: string, value: string) { this.update({ drafts: { ...this.snapshot.drafts, [targetId]: value } }); }
  async select(targetId: string) {
    if (!this.snapshot.targets.some((target) => target.id === targetId)) return;
    this.update({ activeTargetId: targetId });
    await this.refresh(targetId);
  }
  private async ensure(targetId: string): Promise<ChatEntry> {
    const existing = this.snapshot.entries[targetId];
    if (existing?.sessionId && existing.conversationId) return existing;
    const inFlight = this.sessions.get(targetId);
    if (inFlight) return inFlight;
    const promise = (async () => {
      const row = object(await this.port.call('sessions.create', { targetUri: targetId }));
      const sessionId = string(row.sessionId), conversationId = string(row.conversationId);
      if (row.targetUri !== targetId || (existing?.sessionId && sessionId !== existing.sessionId)) {
        throw new LiveError('invalid-response', '聊天会话与原操作不匹配。');
      }
      this.entry(targetId, { sessionId, conversationId });
      return this.snapshot.entries[targetId];
    })();
    this.sessions.set(targetId, promise);
    try { return await promise; } finally { this.sessions.delete(targetId); }
  }
  async refresh(targetId = this.snapshot.activeTargetId) {
    if (!targetId || this.reads.has(targetId)) return;
    this.reads.add(targetId);
    try {
      const entry = await this.ensure(targetId);
      const rows = new Map(entry.messages.map((message) => [message.id, message]));
      let cursor = this.historyStarts.get(targetId);
      const cursors = new Set<string>(cursor ? [cursor] : []);
      let nextStart = cursor; let partial = false;
      for (let page = 0; page < 20; page++) {
        const history = object(await this.port.call('sessions.history', { sessionId: entry.sessionId, args: { limit: 20, ...(cursor ? { cursor } : {}) } }));
        for (const raw of array(history.events)) {
          const event = object(raw); const id = string(event.eventId);
          if (!['user', 'assistant', 'system'].includes(string(event.kind))) throw new LiveError('invalid-response', '消息类型无法确认。');
          if (typeof event.text !== 'string') throw new LiveError('invalid-response', '消息内容无法确认。');
          rows.set(id, { id, targetId, author: event.kind as ConversationMessage['author'], text: event.text, createdAt: string(event.at) });
        }
        if (history.nextCursor === null || history.nextCursor === undefined) { nextStart = cursor; partial = false; break; }
        cursor = string(history.nextCursor);
        if (cursors.has(cursor)) throw new LiveError('invalid-response', '历史分页游标重复。');
        cursors.add(cursor); nextStart = cursor; partial = true;
      }
      this.historyStarts.set(targetId, nextStart);
      this.entry(targetId, { messages: [...rows.values()].sort((a, b) => (a.createdAt ?? '').localeCompare(b.createdAt ?? '') || a.id.localeCompare(b.id)),
        historyStatus: 'ready', historyError: undefined, partial });
    } catch (error) { this.entry(targetId, { historyStatus: 'error', historyError: errorText(error) }); }
    finally {
      this.reads.delete(targetId);
      if (this.rescanRequested.delete(targetId)) { this.historyStarts.delete(targetId); await this.refresh(targetId); }
    }
  }
  async reloadHistory(targetId = this.snapshot.activeTargetId) {
    if (!targetId) return;
    if (this.reads.has(targetId)) { this.rescanRequested.add(targetId); return; }
    this.historyStarts.delete(targetId);
    await this.refresh(targetId);
  }
  async send(targetId: string, text: string) {
    const entry = this.snapshot.entries[targetId]; const old = entry?.operation;
    if (this.snapshot.status !== 'ready' || !this.snapshot.targets.some((target) => target.id === targetId && target.availability === 'available') || !entry?.sessionId || !entry.conversationId || !text.trim() ||
      old?.confirmationPending || (old && old.status !== 'success' && old.status !== 'not-sent')) return;
    const operation: ChatIntent = { targetId, text, requestId: this.key(), status: 'in-flight', canRetry: false };
    this.entry(targetId, { operation });
    await this.submit(targetId, operation);
  }
  private async submit(targetId: string, operation: ChatIntent) {
    const entry = this.snapshot.entries[targetId];
    try {
      const result = await this.port.call('sessions.send', { sessionId: entry.sessionId, request: { text: operation.text, idempotencyKey: operation.requestId } });
      if (this.accept(targetId, operation, result)) { await this.confirmAcceptance(targetId); await this.refresh(targetId); }
    } catch (error) {
      if (this.snapshot.entries[targetId]?.operation?.requestId !== operation.requestId) return;
      const definitive = error instanceof LiveError && error.definitive;
      this.entry(targetId, { operation: { ...operation, status: definitive ? 'definitive-failure' : 'unknown',
        canRetry: definitive && error.canRetry, error: errorText(error) } });
    }
  }
  private accept(targetId: string, operation: ChatIntent, raw: unknown) {
    const result = object(raw); const entry = this.snapshot.entries[targetId];
    if (entry.operation?.requestId !== operation.requestId) return false;
    if (result.status !== 'accepted' || result.commandId !== operation.requestId || result.conversationId !== entry.conversationId) {
      throw new LiveError('invalid-response', '原消息的接收结果尚未确认。');
    }
    const messageId = string(result.messageId);
    this.entry(targetId, { operation: { ...operation, status: 'success', canRetry: false, error: undefined,
      confirmationPending: true, acceptedMessageId: messageId } });
    if (this.snapshot.drafts[targetId] === operation.text) this.setDraft(targetId, '');
    return true;
  }
  async confirmAcceptance(targetId: string) {
    const entry = this.snapshot.entries[targetId], operation = entry?.operation;
    if (!operation?.confirmationPending || operation.status !== 'success' || this.confirmations.has(targetId)) return;
    this.confirmations.add(targetId);
    try {
      const result = object(await this.port.call('sessions.send.confirm', { sessionId: entry.sessionId,
        request: { text: operation.text, idempotencyKey: operation.requestId } }));
      if (result.status !== 'accepted' || result.commandId !== operation.requestId || result.conversationId !== entry.conversationId ||
        result.messageId !== operation.acceptedMessageId) throw new LiveError('invalid-response', '接收确认响应不匹配。');
      if (this.snapshot.entries[targetId]?.operation?.requestId === operation.requestId) {
        this.entry(targetId, { operation: { ...operation, confirmationPending: false, error: undefined } });
      }
    } catch {
      if (this.snapshot.entries[targetId]?.operation?.requestId === operation.requestId) {
        this.entry(targetId, { operation: { ...operation, confirmationPending: true, error: '消息已被服务接收，正在确认本机恢复记录，暂不能发送下一条。' } });
      }
    } finally { this.confirmations.delete(targetId); }
  }
  resetFailure(targetId: string) {
    if (this.snapshot.entries[targetId]?.operation?.status === 'definitive-failure') this.entry(targetId, { operation: undefined });
  }
  async retry(requestId: string) {
    const entry = Object.values(this.snapshot.entries).find((value) => value.operation?.requestId === requestId);
    const operation = entry?.operation;
    if (!operation || operation.status !== 'definitive-failure' || !operation.canRetry) return;
    const pending = { ...operation, status: 'in-flight' as const, canRetry: false };
    this.entry(operation.targetId, { operation: pending });
    await this.submit(operation.targetId, pending);
  }
  async reconcile(targetId: string) {
    const operation = this.snapshot.entries[targetId]?.operation;
    if (!operation || operation.status !== 'unknown' || this.lookups.has(targetId)) return;
    this.lookups.add(targetId);
    try {
      const entry = await this.ensure(targetId);
      const result = object(await this.port.call('sessions.send.lookup', { sessionId: entry.sessionId,
        request: { text: operation.text, idempotencyKey: operation.requestId } }));
      if (result.status === 'accepted') {
        if (this.accept(targetId, operation, result)) { await this.confirmAcceptance(targetId); await this.refresh(targetId); }
      }
      else if (result.status !== 'unknown') throw new LiveError('invalid-response', '原消息的接收结果尚未确认。');
    } catch (error) {
      if (this.snapshot.entries[targetId]?.operation?.requestId === operation.requestId) {
        this.entry(targetId, { operation: { ...operation, status: 'unknown', canRetry: false, error: errorText(error) } });
      }
    }
    finally { this.lookups.delete(targetId); }
  }
  async poll() {
    await Promise.all(Object.entries(this.snapshot.entries).map(async ([id, entry]) => {
      const op = entry.operation;
      if (op?.confirmationPending && (this.confirmationCounts.get(op.requestId) ?? 0) < 20) {
        this.confirmationCounts.set(op.requestId, (this.confirmationCounts.get(op.requestId) ?? 0) + 1);
        await this.confirmAcceptance(id);
      }
      if (op?.status === 'unknown' && (this.lookupCounts.get(op.requestId) ?? 0) < 20) {
        this.lookupCounts.set(op.requestId, (this.lookupCounts.get(op.requestId) ?? 0) + 1);
        await this.reconcile(id);
      }
    }));
    await this.refresh();
  }
}
