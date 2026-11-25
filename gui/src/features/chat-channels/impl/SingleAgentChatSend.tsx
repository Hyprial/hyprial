/**
 * Decision record (chat C1 single-target agent send panel, 2026-11-05)
- This panel is the shared presentation for the ordinary connected chat page and
  for frozen-contract tests. It reuses the shared ChatComposer instead of a second
  send widget, and it never sends when no adapter is injected: ordinary product
  chat therefore stays unavailable until the public method declaration lands.
- The panel owns only draft and presentation state. Operation keys, dispatch and
  recovery stay in the injected adapter; the page passes the canonical target and
  conversation it already froze for this intent.
- Recovery runs on mount and on identity change. An accepted record opens the
  conversation reported by the lookup; an unknown record keeps the key for manual
  queries. Nothing here resends, retries or claims delivery.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Button } from '../../../components/primitives';
import { useNotifyUnavailable } from '../../../components/notifications';
import type { OperationStatus } from '../../../state/operation-lifecycle';
import { ChatComposer } from '../../agent-chat';
import {
  CHAT_SEND_NO_RECOVERY_NOTICE,
  isChatSendMethodRefused,
  type ChatSendApi,
  type ChatSendMethodRefused,
  type ChatSendOutcome,
  type ChatSendRecovery,
} from './daemon-api';

export interface SingleAgentChatSendTarget {
  readonly uri: string;
  readonly displayName: string;
}

export interface SingleAgentChatSendProps {
  readonly send?: ChatSendApi;
  readonly identity?: string;
  readonly target?: SingleAgentChatSendTarget | null;
  readonly conversationId?: string | null;
  readonly onConversationRecovered?: (conversationId: string) => void;
}

interface SendOperation {
  readonly requestId: string;
  readonly status: OperationStatus;
  readonly canRetry: false;
  readonly error?: string;
}

function operationOf(value: Exclude<ChatSendOutcome, ChatSendMethodRefused>, key: string): SendOperation {
  if (value.status === 'accepted') return { requestId: key, status: 'success', canRetry: false };
  if (value.status === 'unknown') return { requestId: key, status: 'unknown', canRetry: false, error: value.notice };
  if (value.status === 'refused') return { requestId: key, status: 'definitive-failure', canRetry: false, error: value.notice };
  return { requestId: key, status: value.reason === 'store-failed' || value.reason === 'capability-unavailable'
    ? 'definitive-failure' : 'not-sent', canRetry: false, error: value.notice };
}

export function SingleAgentChatSend({
  send, identity, target, conversationId, onConversationRecovered,
}: SingleAgentChatSendProps) {
  const notifyUnavailable = useNotifyUnavailable();
  const [draft, setDraft] = useState('');
  const [operation, setOperation] = useState<SendOperation | undefined>();
  const [recovery, setRecovery] = useState<ChatSendRecovery | undefined>();
  const [recovering, setRecovering] = useState(false);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const identityRef = useRef(identity);
  const sendRef = useRef(send);
  const scopeEpoch = useRef(0);
  const aliveRef = useRef(true);
  sendRef.current = send;
  const targetId = target?.uri ?? null;

  function refreshPending() {
    setPendingKey(send ? send.pendingOperationKey() : null);
  }

  async function runRecovery(current: ChatSendApi) {
    const epoch = ++scopeEpoch.current;
    const currentIdentity = identityRef.current;
    setRecovering(true);
    const result = await current.recover();
    if (!aliveRef.current || scopeEpoch.current !== epoch || identityRef.current !== currentIdentity || sendRef.current !== current) return;
    setRecovery(result);
    setRecovering(false);
    refreshPending();
    if (result.status === 'accepted') onConversationRecovered?.(result.conversationId);
  }

  useLayoutEffect(() => {
    if (identityRef.current === identity) return;
    identityRef.current = identity;
    scopeEpoch.current += 1;
    if (!send) return;
    send.clear('identity-change');
    setOperation(undefined);
    setRecovery(undefined);
    setRecovering(false);
    refreshPending();
  }, [identity, send]);

  useEffect(() => {
    aliveRef.current = true;
    if (!send) return () => { aliveRef.current = false; scopeEpoch.current += 1; };
    refreshPending();
    void runRecovery(send);
    return () => { aliveRef.current = false; scopeEpoch.current += 1; };
  }, [send]);

  async function dispatch(text: string) {
    if (!send || !target || !conversationId) return;
    const epoch = ++scopeEpoch.current;
    const currentIdentity = identityRef.current;
    const requestId = send.pendingOperationKey() ?? 'pending';
    setOperation({ requestId, status: 'in-flight', canRetry: false });
    const outcome = await send.send({ to: [target.uri], message: text, conversationId });
    if (!aliveRef.current || scopeEpoch.current !== epoch || identityRef.current !== currentIdentity || sendRef.current !== send) return;
    if (isChatSendMethodRefused(outcome)) {
      setOperation(undefined);
      refreshPending();
      notifyUnavailable('发送消息', { code: outcome.code, source: outcome.source });
      return;
    }
    const key = outcome.status === 'not-started' ? send.pendingOperationKey() : outcome.operationKey;
    setOperation(operationOf(outcome, key ?? requestId));
    refreshPending();
    if (outcome.status === 'accepted' && draft === text) setDraft('');
    if (outcome.status === 'accepted') setRecovery(undefined);
  }

  if (!send) {
    return null;
  }

  const ready = target !== null && target !== undefined && typeof conversationId === 'string' && conversationId.length > 0;
  const composerOperation = operation && target ? { ...operation, targetId: target.uri } : undefined;

  return <section aria-label="向单个 Agent 发送">
    <h2>向单个 Agent 发送</h2>
    {recovering && <p role="status">正在查询上次发送结果</p>}
    {!recovering && recovery?.status === 'accepted' && <p role="status">{recovery.notice}</p>}
    {!recovering && recovery?.status === 'unknown' && <p role="status">{recovery.notice}</p>}
    {!recovering && recovery?.status === 'idle' && <p role="status">{CHAT_SEND_NO_RECOVERY_NOTICE}</p>}
    {pendingKey !== null && <p role="note">恢复键：<code>{pendingKey}</code></p>}
    {operation?.status === 'not-sent' && operation.error && <p role="alert">{operation.error}</p>}
    {operation?.status === 'definitive-failure' && operation.error && <p role="alert">{operation.error}</p>}
    {!ready && <p role="status">请先选择一个 Agent 会话。</p>}
    {(pendingKey !== null || operation?.status === 'unknown') && <div>
      <Button onClick={() => { void runRecovery(send); }}>查询本操作键</Button>
      <Button variant="ghost" onClick={() => {
        send.clear('cancelled');
        setOperation(undefined);
        setRecovery(undefined);
        refreshPending();
      }}>放弃操作键，重新编辑</Button>
    </div>}
    <ChatComposer targetId={targetId} targetName={target?.displayName ?? '未选择对象'} draft={draft}
      capability={ready ? 'ready' : 'unconfirmed'} operation={composerOperation}
      successHint={operation?.status === 'success' ? 'daemon 已持久接受该操作键；这不表示对方已收到。' : undefined}
      onDraftChange={(_targetId, value) => setDraft(value)}
      onSend={(_targetId, text) => { void dispatch(text); }}
      onRetry={() => undefined} />
  </section>;
}
