/** First-run partial creation recovers the same Agent instead of repeating create. */
import { useEffect, useRef, useState } from 'react';
import { Button } from '../../../../components/primitives';
import type { SharedCredentialsApi } from '../../../connections';
import type { ProviderLoginFile } from '../../../agent-provider';
import type { AgentDraft } from '../AgentMutationGateway';
import type { AgentsApi } from '../api';
import { startFailureMessage, toAgentRequestError } from '../api/agent-writes';
import { ModelPanel } from './Panel';
import { ModelStartDialog } from './StartDialog';
import { modelReady, type ModelStatus } from './api';
export function ModelRecovery({ actor, api, credentials, nativeLoginFiles, draft, onConfigured }: {
  actor: string; api: AgentsApi; credentials?: SharedCredentialsApi;
  nativeLoginFiles?: (actor: string, harness: string) => Promise<readonly ProviderLoginFile[]>;
  draft: AgentDraft | null; onConfigured: () => void;
}) {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [unknownStart, setUnknownStart] = useState(false);
  const [checking, setChecking] = useState(false);
  const epoch = useRef(0);
  useEffect(() => { epoch.current += 1; return () => { epoch.current += 1; }; }, [actor, api]);
  async function checkRuntime(): Promise<void> {
    if (!api.list || checking) return;
    const current = epoch.current; setChecking(true);
    try {
      const rows = await api.list();
      if (current !== epoch.current) return;
      const matched = rows.filter(row => row.uri === actor);
      if (matched.length === 1 && matched[0].runState.runtime === 'online') onConfigured();
      else setOutcome('尚未观察到此 Agent 运行，继续保留创建记录与启动锁定。');
    } catch { if (current === epoch.current) setOutcome('运行状态读取失败，未重复启动。'); }
    finally { if (current === epoch.current) setChecking(false); }
  }
  return <>
    <p>Agent 身份已创建。以下操作继续使用此身份，不会重新创建。</p>
    <ModelPanel actor={actor} api={api} credentials={credentials} nativeLoginFiles={nativeLoginFiles}
      onStatus={setStatus} initialHarness={draft?.preferredHarness ?? ''} initialModel={draft?.model ?? ''} />
    <Button variant="primary" disabled={!modelReady(status) || !api.start || unknownStart} onClick={() => setOpen(true)}>启动已配置 Agent</Button>
    {unknownStart && api.list && <Button variant="secondary" disabled={checking} onClick={() => void checkRuntime()}>检查运行状态</Button>}
    {outcome && <p role="status">{outcome}</p>}
    {open && status?.binding && <ModelStartDialog actor={actor} status={status} api={api} cwd={draft?.cwd ?? ''}
      onClose={() => setOpen(false)} onOutcome={result => setOutcome(result.text)} onStart={async options => {
        const current = epoch.current;
        try {
          const result = await api.start!(actor, options);
          if (current !== epoch.current) return { tone: 'error', text: '', dispatched: true };
          if (result.accepted && result.running) onConfigured();
          else setUnknownStart(true);
          return { tone: result.running ? 'status' : 'error', text: result.running ? '已运行，尚未验证模型响应。' : '启动尚未确认，请到 Agent 详情核对。', dispatched: true };
        } catch (error) {
          const failure = startFailureMessage(toAgentRequestError(error));
          if (current === epoch.current && failure.dispatch === 'unknown') setUnknownStart(true);
          return { tone: 'error', text: failure.text, dispatched: failure.dispatch !== 'not-dispatched' };
        }
      }} />}
  </>;
}
