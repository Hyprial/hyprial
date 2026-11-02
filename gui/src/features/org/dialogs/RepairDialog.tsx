import { useEffect, useRef, useState } from 'react';
import { Notice } from '../../../components/primitives';
import { ORG_COPY } from '../domain/copy';
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
  const [steps, setSteps] = useState<readonly string[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const { busy, run } = useBusy();
  const headingRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let alive = true;
    void api.gateway.previewRepair(orgId).then((result) => {
      if (!alive) return;
      if (result.kind === 'ok') {
        setSteps(result.value);
      } else {
        setMessage(resultMessage(result));
      }
    });
    return () => {
      alive = false;
    };
  }, [api.gateway, orgId]);

  async function confirm(): Promise<void> {
    const result = await run(() => api.gateway.repairOrg(orgId));
    if (!result) return;
    if (result.kind === 'ok') {
      api.notify('修复已完成。');
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
      description="先核对修复内容，确认后才执行。"
      onClose={onClose}
      initialFocusRef={headingRef}
      devUi="DEV-UI-ORG-003"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '确认修复',
          variant: 'primary',
          loading: busy,
          disabled: steps === null,
          onClick: () => void confirm(),
        },
      ]}
    >
      <p ref={headingRef} tabIndex={-1} className="org-dialog__lead">
        修复预览
      </p>
      {steps === null && !message ? <p role="status">正在读取修复预览…</p> : null}
      {steps ? (
        <ol className="org-steps-list">
          {steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      ) : null}
      {message ? (
        <Notice tone="warning" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
