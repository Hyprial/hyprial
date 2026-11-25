/**
 * Platform operations share one progress view. Only pending receipts expose
 * their authorization handoff; simulated receipts never open a vendor URL.
 * Pending reads update every two seconds for at most ten minutes or until expiry.
 * Background tabs, unmount and terminal receipts stop reads; writes never replay.
 */
import { useEffect, useRef, useState } from 'react';
import { Button } from '../../../../components/primitives';
import type { AdapterOperation } from '../../platform-integrations-api';
import { active, operationLabel } from '../../platform-integrations-view';

interface OperationProgressProps {
  readonly operation: AdapterOperation;
  readonly busy: boolean;
  readonly onRefresh?: () => void;
  readonly onCancel?: () => void;
}

export function OperationProgress({ operation, busy, onRefresh, onCancel }: OperationProgressProps) {
  const pending = active(operation);
  const expiresAt = operation.expiresAt;
  const [paused, setPaused] = useState<string | null>(null);
  const poll = useRef(onRefresh);
  poll.current = onRefresh;
  const pollWindow = useRef({ key: operation.operationId, until: Date.now() + 600000 });
  if (pollWindow.current.key !== operation.operationId) {
    pollWindow.current = { key: operation.operationId, until: Date.now() + 600000 };
  }
  useEffect(() => {
    if (!pending || busy || !onRefresh) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      clearTimeout(timer);
      if (document.visibilityState === 'hidden') return;
      timer = setTimeout(() => {
        if (Date.now() < Math.min(expiresAt ?? Infinity, pollWindow.current.until)) poll.current?.();
        else setPaused(operation.operationId);
      }, 2000);
    };
    schedule();
    document.addEventListener('visibilitychange', schedule);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', schedule);
    };
  }, [operation.operationId, operation.status, busy, expiresAt, pending, onRefresh]);
  if (operation.status === 'succeeded') return null;
  const handoff = pending && !operation.simulated
    && (expiresAt === undefined || expiresAt > Date.now());
  const action = operation.kind === 'add' ? '添加' : '授权';
  return (
    <div className="platform-integrations__operation">
      <p role="status" className="platform-integrations__operation-title">
        {operationLabel(operation)}{operation.simulated ? '（模拟）' : ''}
      </p>
      {handoff && operation.verificationUrl && (
        <a className="platform-integrations__authorization-link" href={operation.verificationUrl} target="_blank" rel="noopener noreferrer">
          打开平台授权页面
        </a>
      )}
      {handoff && expiresAt && (
        <p className="platform-integrations__hint">有效期至 {new Date(expiresAt).toLocaleString('zh-CN', { hour12: false })}</p>
      )}
      {pending && (
        <div className="platform-integrations__actions">
          <Button size="sm" disabled={busy || !onRefresh} onClick={onRefresh}>刷新{action}状态</Button>
          <Button variant="text" size="sm" disabled={busy || !onCancel} onClick={onCancel}>取消{action}</Button>
        </div>
      )}
      {pending && paused === operation.operationId && <p role="status">自动更新已暂停，请刷新确认最终状态。</p>}
    </div>
  );
}
