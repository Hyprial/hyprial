import type { OperationStatus } from '../../state/operation-lifecycle';
import { array, object, string, LiveError, type LivePort } from './client';
export interface Authorization {
  readonly adapter: string;
  readonly appId: string | null;
  readonly authorization: 'authorized' | 'unauthorized' | 'unconfigured' | 'unconfirmed';
  readonly capabilities: readonly string[];
  readonly checkedAt: number;
  readonly reason?: string;
}
export interface LarkAdapter {
  readonly bindingAvailable: boolean;
  readonly operation?: AuthorizationOperation;
  readonly adapter: string;
  readonly authorization: Authorization;
  readonly binding: { readonly actor: string | null };
  readonly runtime: { readonly online: boolean };
}
export interface AuthorizationOperation {
  readonly operationId?: string;
  readonly adapter: string;
  readonly status: 'starting' | 'awaiting_user' | 'checking' | 'succeeded' | 'denied' | 'expired' | 'cancelled' | 'failed' | 'unknown';
  readonly authorization?: Authorization;
  readonly verificationUrl?: string;
  readonly expiresAt?: number;
  readonly errorCode?: string;
}
export interface SwitchIntent {
  readonly adapter: string;
  readonly agentId: string;
  readonly expectedActor: string | null;
  readonly status: OperationStatus;
  readonly error?: string;
}
export interface LarkSnapshot {
  readonly status: 'loading' | 'ready' | 'error';
  readonly error?: string;
  readonly adapters: readonly LarkAdapter[];
  readonly selectedAdapter: string | null;
  readonly selectedAgent: string | null;
  readonly authorizationOperations: Readonly<Record<string, AuthorizationOperation>>;
  readonly switchOperations: Readonly<Record<string, SwitchIntent>>;
  readonly confirmation?: Omit<SwitchIntent, 'status'>;
}
function authorization(raw: unknown): Authorization {
  const value = object(raw), adapter = string(value.adapter);
  if (!['authorized', 'unauthorized', 'unconfigured', 'unconfirmed'].includes(string(value.authorization)) ||
      (value.appId !== null && typeof value.appId !== 'string') || typeof value.checkedAt !== 'number' || !Number.isFinite(value.checkedAt)) {
    throw new LiveError('invalid-response', '飞书授权状态尚未确认。');
  }
  return { adapter, appId: value.appId as string | null, authorization: value.authorization as Authorization['authorization'],
    checkedAt: value.checkedAt, capabilities: array(value.capabilities).map(string), reason: typeof value.reason === 'string' ? value.reason : undefined };
}
function adapter(raw: unknown): LarkAdapter {
  const value = object(raw), name = string(value.adapter), auth = authorization(value.authorization);
  const binding = object(value.binding), runtime = object(value.runtime);
  if (auth.adapter !== name || (binding.actor !== null && typeof binding.actor !== 'string') || typeof runtime.online !== 'boolean') {
    throw new LiveError('invalid-response', '飞书绑定状态尚未确认。');
  }
  return { adapter: name, bindingAvailable: value.bindingAvailable === true,
    operation: value.operation == null ? undefined : authOperation(value.operation, name), authorization: auth, binding: { actor: binding.actor as string | null }, runtime: { online: runtime.online } };
}
const active = (op?: AuthorizationOperation) => !!op && ['starting', 'awaiting_user', 'checking', 'unknown'].includes(op.status);
function authOperation(raw: unknown, expectedAdapter: string, expectedId?: string): AuthorizationOperation {
  const value = object(raw); const operationId = string(value.operationId), name = string(value.adapter);
  const status = string(value.status);
  if (name !== expectedAdapter || (expectedId && operationId !== expectedId) ||
    !['starting', 'awaiting_user', 'checking', 'succeeded', 'denied', 'expired', 'cancelled', 'failed', 'unknown'].includes(status)) {
    throw new LiveError('invalid-response', '授权操作响应不匹配。');
  }
  const auth = authorization(value.authorization);
  if (auth.adapter !== name || value.appId !== auth.appId || (status === 'succeeded' && auth.authorization !== 'authorized')) throw new LiveError('invalid-response', '尚未确认授权。');
  let verificationUrl: string | undefined;
  if (value.verificationUrl !== undefined && ['awaiting_user', 'checking'].includes(status)) {
    const url = new URL(string(value.verificationUrl));
    if (url.protocol !== 'https:' || url.username || url.password) throw new LiveError('invalid-response', '授权链接无法确认。');
    verificationUrl = url.href;
  }
  if (verificationUrl && (typeof value.expiresAt !== 'number' || !Number.isFinite(value.expiresAt))) throw new LiveError('invalid-response', '授权期限无法确认。');
  return { operationId, adapter: name, status: status as AuthorizationOperation['status'], authorization: auth,
    verificationUrl, expiresAt: verificationUrl ? value.expiresAt as number : undefined,
    errorCode: typeof value.errorCode === 'string' ? value.errorCode : undefined };
}

export class LarkController {
  private snapshot: LarkSnapshot = { status: 'loading', adapters: [], selectedAdapter: null, selectedAgent: null, authorizationOperations: {}, switchOperations: {} };
  private listeners = new Set<() => void>();
  private reading = false;
  private revision = 0;
  private authReads = new Set<string>();
  constructor(private readonly port: LivePort) {}
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(change: Partial<LarkSnapshot>) { this.snapshot = { ...this.snapshot, ...change }; this.listeners.forEach((listener) => listener()); }
  private operation(name: string, op: AuthorizationOperation, expectedId?: string) {
    const existing = this.snapshot.authorizationOperations[name];
    if (expectedId && existing?.operationId !== expectedId) return;
    // Worker custody can outlive an App configuration change. Keep its ID so
    // cancellation can finish cleanup; only matching App grants may update UI.
    this.revision++;
    this.update({ authorizationOperations: { ...this.snapshot.authorizationOperations, [name]: op },
      adapters: op.authorization ? this.snapshot.adapters.map((row) => row.adapter === name && op.authorization!.appId === row.authorization.appId &&
        op.authorization!.checkedAt >= row.authorization.checkedAt ? { ...row, authorization: op.authorization! } : row) : this.snapshot.adapters });
  }
  chooseAdapter(name: string) {
    if (this.snapshot.adapters.some((row) => row.adapter === name)) this.update({ selectedAdapter: name, confirmation: undefined });
  }
  chooseAgent(agentId: string) { this.update({ selectedAgent: agentId, confirmation: undefined }); }
  dismissConfirmation() { this.update({ confirmation: undefined }); }
  async refresh() {
    if (this.reading) return;
    this.reading = true;
    const revision = this.revision;
    try {
      const result = object(await this.port.call('lark.adapters'));
      if (revision !== this.revision) return;
      const adapters = array(result.adapters).map(adapter).map((row) => {
        const previous = this.snapshot.adapters.find((value) => value.adapter === row.adapter);
        return previous && previous.authorization.appId === row.authorization.appId && previous.authorization.checkedAt > row.authorization.checkedAt
          ? { ...row, authorization: previous.authorization } : row;
      });
      if (new Set(adapters.map((row) => row.adapter)).size !== adapters.length) throw new LiveError('invalid-response', '飞书列表无法确认。');
      const switches = { ...this.snapshot.switchOperations };
      const operations = { ...this.snapshot.authorizationOperations };
      for (const row of adapters) {
        if (row.operation) {
          if (row.operation.authorization?.appId === row.authorization.appId || active(row.operation)) operations[row.adapter] = row.operation;
          else if (operations[row.adapter]?.operationId === row.operation.operationId) delete operations[row.adapter];
        }
        const retained = operations[row.adapter];
        if (retained?.authorization && retained.authorization.appId !== row.authorization.appId && !active(retained)) delete operations[row.adapter];
        const op = switches[row.adapter];
        if (op?.status === 'unknown' && row.binding.actor === op.agentId) switches[row.adapter] = { ...op, status: 'success', error: undefined };
      }
      this.update({ status: 'ready', error: undefined, adapters, switchOperations: switches, authorizationOperations: operations,
        selectedAdapter: adapters.some((row) => row.adapter === this.snapshot.selectedAdapter) ? this.snapshot.selectedAdapter : adapters[0]?.adapter ?? null });
    } catch (error) { if (revision === this.revision) this.update({ status: 'error', error: error instanceof LiveError ? error.message : '飞书连接状态尚未确认。' }); }
    finally { this.reading = false; }
  }
  async authorize() {
    const name = this.snapshot.selectedAdapter;
    const row = this.snapshot.adapters.find((value) => value.adapter === name);
    if (!name || !row || this.snapshot.status !== 'ready' || row.authorization.authorization === 'unconfigured' || active(this.snapshot.authorizationOperations[name])) return;
    this.operation(name, { adapter: name, status: 'starting' });
    try { this.operation(name, authOperation(await this.port.call('lark.authorization.start', { adapter: name }), name)); }
    catch (error) { this.operation(name, { adapter: name, status: error instanceof LiveError && error.definitive ? 'failed' : 'unknown', errorCode: error instanceof LiveError ? error.code : 'unconfirmed' }); }
  }
  async pollAuthorization(name = this.snapshot.selectedAdapter) {
    if (!name || this.authReads.has(name)) return;
    const op = this.snapshot.authorizationOperations[name];
    if (!active(op) || !op.operationId) return;
    this.authReads.add(name);
    try { this.operation(name, authOperation(await this.port.call('lark.authorization.operation', { operationId: op.operationId }), name, op.operationId), op.operationId); }
    catch (error) { this.operation(name, { ...op, status: 'unknown', verificationUrl: undefined, expiresAt: undefined,
      errorCode: error instanceof LiveError ? error.code : 'unconfirmed' }, op.operationId); }
    finally { this.authReads.delete(name); }
  }
  async cancelAuthorization() {
    const name = this.snapshot.selectedAdapter;
    if (!name || this.authReads.has(name)) return;
    const op = this.snapshot.authorizationOperations[name];
    if (!active(op) || !op.operationId) return;
    this.authReads.add(name);
    try { this.operation(name, authOperation(await this.port.call('lark.authorization.cancel', { operationId: op.operationId }), name, op.operationId), op.operationId); }
    catch (error) { this.operation(name, { ...op, status: 'unknown', verificationUrl: undefined, expiresAt: undefined,
      errorCode: error instanceof LiveError ? error.code : 'unconfirmed' }, op.operationId); }
    finally { this.authReads.delete(name); }
  }
  prepareSwitch() {
    const name = this.snapshot.selectedAdapter, agentId = this.snapshot.selectedAgent;
    const row = this.snapshot.adapters.find((value) => value.adapter === name);
    if (!name || !agentId || !row || !row.bindingAvailable || this.snapshot.status !== 'ready' || row.authorization.authorization !== 'authorized' ||
      ['unknown', 'in-flight'].includes(this.snapshot.switchOperations[name]?.status)) return;
    this.update({ confirmation: { adapter: name, agentId, expectedActor: row.binding.actor } });
  }
  async confirmSwitch() {
    const intent = this.snapshot.confirmation;
    if (!intent) return;
    this.revision++;
    this.update({ confirmation: undefined, switchOperations: { ...this.snapshot.switchOperations, [intent.adapter]: { ...intent, status: 'in-flight' } } });
    try {
      const result = adapter(await this.port.call('lark.binding.set', { adapter: intent.adapter, agentId: intent.agentId, expectedActor: intent.expectedActor }));
      if (result.adapter !== intent.adapter || result.binding.actor !== intent.agentId) throw new LiveError('invalid-response', '切换结果尚未确认。');
      this.revision++;
      this.update({ adapters: this.snapshot.adapters.map((row) => row.adapter === result.adapter ? result : row),
        switchOperations: { ...this.snapshot.switchOperations, [intent.adapter]: { ...intent, status: 'success' } } });
    } catch (error) {
      this.revision++;
      this.update({ switchOperations: { ...this.snapshot.switchOperations, [intent.adapter]: { ...intent,
        status: error instanceof LiveError && error.definitive ? 'definitive-failure' : 'unknown',
        error: error instanceof LiveError ? error.message : '切换结果尚未确认，请刷新状态。' } } });
    }
  }
  async poll() {
    await Promise.all(Object.keys(this.snapshot.authorizationOperations).map((name) => this.pollAuthorization(name)));
    await this.refresh();
  }
}
