import { useRef, useState } from 'react';
import { Field, Notice } from '../../../components/primitives';
import { ORG_COPY } from '../domain/copy';
import type { OrgGateway, OrgSummary } from '../domain/types';
import { validateOrgName } from '../domain/validation';
import { resultMessage } from '../workspace/api';
import { focusSoon, useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';

/**
 * 新建组织（P03）：就地校验；成功后在同一对话框询问是否设为当前组织，不自动切换；
 * 失败保留输入；未接通只在本地显示“暂不可用”。
 */
export function CreateOrgDialog({
  gateway,
  onClose,
  onCreated,
  onSetCurrent,
}: {
  readonly gateway: OrgGateway;
  readonly onClose: () => void;
  readonly onCreated: (org: OrgSummary) => void;
  readonly onSetCurrent: (org: OrgSummary) => void;
}) {
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [created, setCreated] = useState<OrgSummary | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const successRef = useRef<HTMLParagraphElement>(null);
  const { busy, run } = useBusy();

  async function submit(): Promise<void> {
    const invalid = validateOrgName(name);
    setError(invalid);
    setFailure(null);
    if (invalid) {
      inputRef.current?.focus();
      return;
    }
    const result = await run(() => gateway.createOrg(name.trim()));
    if (!result) {
      return;
    }
    if (result.kind === 'ok') {
      setCreated(result.value);
      onCreated(result.value);
      return;
    }
    focusSoon(inputRef);
    setFailure(
      result.kind === 'failed'
        ? `组织创建未完成：${result.message}`
        : (resultMessage(result) ?? ORG_COPY.unavailable),
    );
  }

  if (created) {
    return (
      <OrgDialog
        title="组织已创建"
        onClose={onClose}
        initialFocusRef={successRef}
        devUi="DEV-UI-ORG-002"
        actions={[
          { label: '暂不', onClick: onClose },
          {
            label: ORG_COPY.setCurrent,
            variant: 'primary',
            onClick: () => {
              onSetCurrent(created);
              onClose();
            },
          },
        ]}
      >
        <p role="status" ref={successRef} tabIndex={-1}>
          {ORG_COPY.askSetCurrent(created.name)}
        </p>
        <p className="org-muted">设为当前组织后，之后新建的 Agent 默认归入它。</p>
      </OrgDialog>
    );
  }

  return (
    <OrgDialog
      title={ORG_COPY.createOrg}
      description="组织名只用小写字母、数字和连字符，最长 57 个字符，不能以 -acl 结尾。"
      onClose={onClose}
      initialFocusRef={inputRef}
      devUi="DEV-UI-ORG-002"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : "创建",
          variant: "primary",
          loading: busy,
          onClick: () => void submit(),
        },
      ]}
    >
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Field label="组织名称" htmlFor="org-new-name" error={error ?? undefined} required>
          <input
            id="org-new-name"
            ref={inputRef}
            className="org-input"
            autoComplete="off"
            spellCheck={false}
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
        </Field>
      </form>
      {failure ? (
        <Notice tone="danger" role="alert">
          {failure}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
