import { useCallback, useEffect, useRef, useState } from 'react';
import { ClientError } from '../../../../client';
import { Card, Notice } from '../../../../components/primitives';
import type { DiagnosticApi, DiagnosticSnapshot } from './api';

import { Readout, RefreshButton, readoutTime } from '../views/Readout';

type State = { readonly kind: 'loading' | 'failed' | 'unknown' }
  | { readonly kind: 'ready'; readonly result: DiagnosticSnapshot };

export function DiagnosticSection({ api }: { readonly api: DiagnosticApi }) {
  const [snapshot, setSnapshot] = useState<{ api: DiagnosticApi; state: State }>({
    api, state: { kind: 'loading' },
  });
  const pending = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setSnapshot({ api, state: { kind: 'loading' } });
    try {
      const result = await api.read(controller.signal);
      if (current === generation.current && !controller.signal.aborted) {
        setSnapshot({ api, state: { kind: 'ready', result } });
      }
    } catch (error) {
      if (current === generation.current && !controller.signal.aborted) {
        setSnapshot({ api, state: { kind: error instanceof ClientError && error.dispatch === 'unknown' ? 'unknown' : 'failed' } });
      }
    }
  }, [api]);
  useEffect(() => {
    void refresh();
    return () => { ++generation.current; pending.current?.abort(); };
  }, [api, refresh]);
  const state: State = snapshot.api === api ? snapshot.state : { kind: 'loading' };
  return <Card as="section" padding="lg" className="settings-card" aria-label="运行诊断">
    <div className="settings-card__header">
      <h2>运行诊断</h2>
      <RefreshButton label="运行诊断" disabled={state.kind === 'loading'} onClick={() => void refresh()} />
    </div>
    <div className="settings-live" aria-live="polite" aria-busy={state.kind === 'loading'}>
      {state.kind === 'loading' && <p role="status">正在读取运行诊断…</p>}
      {(state.kind === 'failed' || state.kind === 'unknown') && <Notice tone="warning"
        title={state.kind === 'unknown' ? '运行诊断结果未知' : '运行诊断查询失败'}>
        未取得快照，请检查服务与访问权限后重试。
      </Notice>}
      {state.kind === 'ready' && <Readout rows={[
        ['观测时间', readoutTime(state.result.observedAtMs)],
        ['Agent 观测条目', state.result.actorCount], ['滞留收件人条目', state.result.strandedCount],
      ]} />}
    </div>
  </Card>;
}
