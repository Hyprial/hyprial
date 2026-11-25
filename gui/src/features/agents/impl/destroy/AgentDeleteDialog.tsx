import { useEffect, useEffectEvent, useRef, useState } from 'react';
import type { AgentDestroyPreviewResult } from '../../../../client/generated/types';
import { Dialog } from '../../../../components/primitives';
import { methodRefusal } from '../../../../client';
import { useNotifyUnavailable } from '../../../../components/notifications';
import { agentFailureMessage } from '../api/agent-writes';

interface Props {
  readonly name: string;
  readonly load: () => Promise<AgentDestroyPreviewResult>;
  readonly confirm: (operationId: string) => Promise<void>;
  readonly onClose: () => void;
}

export function AgentDeleteDialog({ name, load, confirm, onClose }: Props) {
  const notifyUnavailable = useNotifyUnavailable();
  const [preview, setPreview] = useState<AgentDestroyPreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now);
  const [pending, setPending] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const busy = useRef(false);
  const previewFailed = useEffectEvent((failure: unknown) => {
    const refusal = methodRefusal(failure);
    if (refusal) {
      notifyUnavailable('删除 Agent', refusal);
      onClose();
    } else setError(agentFailureMessage(failure).text);
  });

  useEffect(() => {
    let active = true;
    setPreview(null);
    setError(null);
    void load().then(result => {
      if (active) { setPreview(result); setNow(Date.now()); }
    }, failure => {
      if (active) previewFailed(failure);
    });
    return () => { active = false; };
  }, [load, revision]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  async function execute() {
    if (busy.current || submitted || !preview || Date.now() >= preview.expiresAt) return;
    busy.current = true;
    setPending(true);
    setSubmitted(true);
    try { await confirm(preview.operationId); }
    catch { setError('删除结果未确认，请关闭窗口并核对 Agent 列表。'); }
    finally { busy.current = false; setPending(false); }
  }

  const expired = preview !== null && now >= preview.expiresAt;
  return <Dialog open title="删除 Agent？" description={`删除 ${name} 后无法恢复。`}
    onClose={() => { if (!busy.current) onClose(); }} closeOnEscape={!pending}
    actions={[
      { label: '取消', onClick: onClose, variant: 'secondary', disabled: pending },
      ...((expired || error) && !submitted ? [{ label: '重新预览', onClick: () => setRevision(value => value + 1), variant: 'secondary' as const }] : []),
      { label: '确认删除', onClick: () => void execute(), variant: 'danger', loading: pending,
        disabled: !preview || expired || pending || submitted },
    ]}>
    {!preview && !error && <p role="status">正在读取删除影响…</p>}
    {error && <p role="alert">{error}</p>}
    {preview && <>
      <p>将停止 Agent，删除其 home（工作区、配置和凭据），解除平台绑定，移除例行任务，并丢弃待处理消息。</p>
      <dl className="agent-detail-facts">
        <div><dt>工作区</dt><dd style={{ overflowWrap: 'anywhere' }}>{preview.workspace.path}</dd></div>
        <div><dt>工作区内容</dt><dd>{preview.workspace.files} 个文件 · {preview.workspace.bytes} 字节</dd></div>
        <div><dt>平台绑定</dt><dd>{preview.adapterBindings.join('、') || '无'}</dd></div>
        <div><dt>例行任务</dt><dd>{preview.routines.join('、') || '无'}</dd></div>
        <div><dt>待处理消息</dt><dd>{preview.pendingMessages}</dd></div>
      </dl>
      {preview.workspace.unreadableEntries > 0 && <p role="alert">有 {preview.workspace.unreadableEntries} 项无法读取，统计不完整。</p>}
      <p>以上为当前统计，工作区内容和待处理消息可能继续变化。</p>
      {expired && <p role="alert">预览已过期，请重新预览后确认。</p>}
    </>}
  </Dialog>;
}
