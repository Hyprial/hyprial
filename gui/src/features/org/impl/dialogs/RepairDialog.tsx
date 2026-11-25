import { useEffect, useRef, useState } from 'react';
import { Notice } from '../../../../components/primitives';
import { ORG_COPY } from '../domain/copy';
import type { OrgRepairCandidate } from '../domain/types';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';

/** 修复创建未完成的组织（P03）：先预览，再确认执行；不自动重试。 */
export function RepairDialog({
  api,
  orgId,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly orgId: string;
  readonly onClose: () => void;
}) {
  const [candidates, setCandidates] = useState<readonly OrgRepairCandidate[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const { busy, run } = useBusy();
  const headingRef = useRef<HTMLParagraphElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    let alive = true;
    void api.gateway.previewRepair(orgId).then((result) => {
      if (!alive) return;
      if (result.kind === 'unavailable') {
        closeRef.current();
      } else if (result.kind === 'ok') {
        setCandidates(result.value);
      } else {
        setMessage(resultMessage(result));
      }
    });
    return () => {
      alive = false;
    };
  }, [api.gateway, orgId]);

  const repairable = candidates?.filter((candidate) => candidate.repairable) ?? [];
  const manual = (candidates?.length ?? 0) - repairable.length;
  const canConfirm = candidates !== null && repairable.length > 0;

  async function confirm(): Promise<void> {
    const result = await run(() => api.gateway.repairOrg(orgId));
    if (!result || result.kind === 'unavailable') return;
    if (result.kind === 'ok') {
      const repaired = result.value.length;
      const incomplete = result.value.filter(
        (item) => !item.published || !item.devicePublished,
      ).length;
      // Repairable candidates the daemon did not actually execute still need
      // attention; a repaired space whose device publish failed is also not
      // done.  Only a complete apply may claim the repair is finished.
      const remaining = manual + Math.max(0, repairable.length - repaired);
      if (repaired === 0) {
        api.notify(
          remaining > 0
            ? `没有执行自动修复；${remaining} 项仍需人工处理。`
            : '没有执行自动修复；仍需人工处理。',
        );
      } else if (incomplete > 0) {
        api.notify(`已修复 ${repaired} 项，但 ${incomplete} 项发布未完成；仍需处理。`);
      } else if (remaining > 0) {
        api.notify(`已修复 ${repaired} 项；另有 ${remaining} 项仍需人工处理。`);
      } else {
        api.notify('修复已完成。');
      }
      onClose();
      return;
    }
    if (result.kind === 'submitted') {
      api.notify(ORG_COPY.submitted);
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title="修复组织"
      description="确认后才执行修复。"
      onClose={onClose}
      initialFocusRef={headingRef}
      devUi="DEV-UI-ORG-003"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '确认修复',
          variant: 'primary',
          loading: busy,
          disabled: !canConfirm,
          onClick: () => void confirm(),
        },
      ]}
    >
      <p ref={headingRef} tabIndex={-1} className="org-dialog__lead">
        修复预览
      </p>
      {candidates === null && !message ? <p role="status">正在读取修复预览…</p> : null}
      {candidates ? (
        candidates.length === 0 ? (
          <p className="org-muted">没有需要修复的项。</p>
        ) : (
          <ol className="org-steps-list">
            {candidates.map((candidate, index) => (
              <li key={`${candidate.org}:${candidate.reason}:${index}`}>
                {candidate.org}：{candidate.reason}
                {candidate.repairable ? '（可自动修复）' : '（需人工）'}
              </li>
            ))}
          </ol>
        )
      ) : null}
      {candidates !== null && candidates.length > 0 && repairable.length === 0 ? (
        <Notice tone="warning" role="status">
          没有可自动修复的项，需要人工处理；本次不会执行自动修复。
        </Notice>
      ) : null}
      {manual > 0 && repairable.length > 0 ? (
        <p className="org-muted">另有 {manual} 项需要人工处理，本次不会自动执行。</p>
      ) : null}
      {message ? (
        <Notice tone="warning" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
