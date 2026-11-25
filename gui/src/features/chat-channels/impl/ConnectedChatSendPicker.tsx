/**
 * C1 ordinary-page selection uses authenticated message reads and the daemon's
 * deliverable-target directory. It offers only already-known conversations
 * whose message rows identify exactly one canonical agent. Host composition
 * supplies the trusted identity scope; changing that scope clears the pending
 * key before the new panel can recover it.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ChatSendApi } from './daemon-api';
import type { MessageReadApi } from './message-read-api';
import { SingleAgentChatSend } from './SingleAgentChatSend';
import { sendSessionsFromMessages, type SendDirectoryTarget, type SendSession } from './send-session-selection';

interface Props {
  readonly send: ChatSendApi;
  readonly read: MessageReadApi;
  readonly targets: readonly SendDirectoryTarget[];
  /** Supplied by trusted host composition, never inferred from message rows. */
  readonly identity: string;
  /** Invalidation scope only; never used as an authenticated principal. */
  readonly generation: string;
}

type Directory = { status: 'loading' | 'error'; scope: string } |
  { status: 'ready'; scope: string; sessions: readonly SendSession[] };

export function ConnectedChatSendPicker({ send, read, targets, identity, generation }: Props) {
  const scope = `${identity.length}:${identity}:${generation}`;
  const [directory, setDirectory] = useState<Directory>({ status: 'loading', scope });
  const [selected, setSelected] = useState<string | null>(null);
  const previousScope = useRef({ identity, generation });

  useLayoutEffect(() => {
    if (previousScope.current.identity === identity && previousScope.current.generation === generation) return;
    previousScope.current = { identity, generation };
    send.clear('identity-change');
    setSelected(null);
  }, [send, identity, generation]);

  useEffect(() => {
    let active = true;
    setDirectory({ status: 'loading', scope });
    void Promise.all([read.query('inbox'), read.query('outbox')])
      .then(([inbox, outbox]) => {
        if (active) setDirectory({ status: 'ready', scope, sessions: sendSessionsFromMessages([...inbox, ...outbox], targets) });
      })
      .catch(() => { if (active) setDirectory({ status: 'error', scope }); });
    return () => { active = false; };
  }, [read, targets, scope]);

  const currentDirectory = directory.scope === scope ? directory : { status: 'loading' as const, scope };
  const sessions = currentDirectory.status === 'ready' ? currentDirectory.sessions : [];
  const choice = sessions.find(row => row.conversationId === selected) ?? null;

  return <>
    <section aria-label="发送会话选择">
      <h2>选择发送会话</h2>
      {currentDirectory.status === 'loading' && <p role="status">正在确认可发送会话</p>}
      {currentDirectory.status === 'error' && <p role="alert">发送会话目录读取失败</p>}
      {currentDirectory.status === 'ready' && sessions.length === 0 && <p role="status">暂无可发送的已存在 Agent 会话。</p>}
      {sessions.length > 0 && <label>发送会话
        <select value={choice?.conversationId ?? ''} onChange={event => setSelected(event.target.value || null)}>
          <option value="">请选择 Agent 会话</option>
          {sessions.map(row => <option key={row.conversationId} value={row.conversationId}>
            {row.displayName} · {row.conversationId}
          </option>)}
        </select>
      </label>}
    </section>
    <SingleAgentChatSend key={`${identity}:${generation}`} send={send} identity={identity}
      target={choice ? { uri: choice.targetUri, displayName: choice.displayName } : null}
      conversationId={choice?.conversationId ?? null}
      onConversationRecovered={conversationId => {
        if (sessions.some(row => row.conversationId === conversationId)) setSelected(conversationId);
      }} />
  </>;
}
