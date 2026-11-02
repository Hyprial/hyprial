import { useEffect, useRef, useState } from 'react';
import { Notice } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { OrgPerson, RemovalPreview } from '../domain/types';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';
import { RemovalPreviewView, RemovalStages } from './RemovalPreviewView';

/**
 * 移除成员（P16）：预览 → 确认。后端报告预览已变化时重新展示最新预览并要求再次确认，
 * 不重用旧确认；提交后显示“已提交，等待处理”，不立即从列表删除。
 */
export function RemoveMemberDialog({
  api,
  orgId,
  person,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly orgId: string;
  readonly person: OrgPerson;
  readonly onClose: () => void;
}) {
  const [preview, setPreview] = useState<RemovalPreview | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  // 预览变化时保留上一次的预览，用来标出新出现的设备。
  const [previous, setPrevious] = useState<RemovalPreview | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const { busy, run } = useBusy();
  const leadRef = useRef<HTMLParagraphElement>(null);

  async function loadPreview(): Promise<void> {
    const result = await api.gateway.previewRemoveMember(orgId, person.id);
    if (result.kind === 'ok') {
      setPreview(result.value);
    } else {
      setMessage(resultMessage(result));
    }
  }

  useEffect(() => {
    void loadPreview();
    // 只在打开时读取一次预览。
  }, []);

  async function confirm(): Promise<void> {
    if (!preview) return;
    const result = await run(() => api.gateway.removeMember(orgId, person.id, preview.revision));
    if (!result) return;
    if (result.kind === 'failed' && result.code === 'stale') {
      setChanged(true);
      setPrevious(preview);
      setPreview(null);
      await loadPreview();
      leadRef.current?.focus();
      return;
    }
    if (result.kind === 'submitted' || result.kind === 'ok') {
      setSubmitted(true);
      api.notify(`${ORG_COPY.removeMember}：${ORG_COPY.submitted}`);
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={submitted ? zh`已提交：移除成员 ${person.displayName}` : zh`移除成员 ${person.displayName}？`}
      description={submitted ? undefined : '先核对影响，确认后才移除。网络超时不会自动重试。'}
      onClose={onClose}
      initialFocusRef={leadRef}
      devUi="DEV-UI-ORG-004"
      actions={
        submitted
          ? [{ label: '知道了', variant: 'primary', onClick: onClose }]
          : [
              { label: '取消', onClick: onClose },
              {
                label: busy ? ORG_COPY.processing : changed ? '再次确认移除' : '确认移除',
                variant: 'danger',
                loading: busy,
                disabled: preview === null,
                onClick: () => void confirm(),
              },
            ]
      }
    >
      <p
        ref={leadRef}
        tabIndex={-1}
        className={submitted ? 'org-dialog__lead org-dialog__lead--submitted' : 'org-dialog__lead'}
      >
        {submitted ? ORG_COPY.submitted : '移除预览'}
      </p>
      {changed && !submitted ? (
        <Notice tone="warning" role="alert">
          {ORG_COPY.previewChanged}
        </Notice>
      ) : null}
      {submitted ? (
        <>
          <RemovalStages />
          <p className="org-muted">列表收到事件或刷新后才更新，不会立即删除这一行。</p>
        </>
      ) : preview ? (
        <RemovalPreviewView preview={preview} previous={previous} />
      ) : !message ? (
        <p role="status">正在读取移除预览…</p>
      ) : null}
      {message ? (
        <Notice tone="warning" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
