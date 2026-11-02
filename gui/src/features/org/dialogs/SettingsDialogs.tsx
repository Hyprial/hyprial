import { useEffect, useRef, useState } from 'react';
import { Field, Notice } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { OrgSummary, RemovalPreview } from '../domain/types';
import { validateOrgName } from '../domain/validation';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { focusSoon, useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';
import { RemovalPreviewView, RemovalStages } from './RemovalPreviewView';

/**
 * 改名（P17）：规则与新建组织相同；确认后显示“已提交，等待处理”，刷新后显示新名称。
 * 改的是显示名还是组织标识，在后端对齐时确定。
 */
export function RenameDialog({
  api,
  org,
  onClose,
  onSubmitted,
}: {
  readonly api: WorkspaceApi;
  readonly org: OrgSummary;
  readonly onClose: () => void;
  readonly onSubmitted?: (name: string) => void;
}) {
  const [name, setName] = useState(org.name);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { busy, run } = useBusy();

  async function submit(): Promise<void> {
    const invalid = validateOrgName(name) ?? (name.trim() === org.name ? '新名称与当前名称相同' : null);
    setError(invalid);
    setFailure(null);
    if (invalid) {
      inputRef.current?.focus();
      return;
    }
    const result = await run(() => api.gateway.renameOrg(org.id, name.trim()));
    if (!result) return;
    if (result.kind === 'submitted' || result.kind === 'ok') {
      api.notify(zh`改名为 ${name.trim()}：${ORG_COPY.submitted}`);
      onSubmitted?.(name.trim());
      onClose();
      return;
    }
    focusSoon(inputRef);
    setFailure(resultMessage(result));
  }

  return (
    <OrgDialog
      title={zh`改名 ${org.name}`}
      description={ORG_COPY.renameImpact}
      onClose={onClose}
      initialFocusRef={inputRef}
      devUi="DEV-UI-ORG-013"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '确认改名',
          variant: 'primary',
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
        <Field
          label="新名称"
          htmlFor="org-rename-input"
          hint="只用小写字母、数字和连字符，最长 57 个字符，不能以 -acl 结尾。"
          error={error ?? undefined}
          required
        >
          <input
            id="org-rename-input"
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

/** 采信候选（P10 第 5 步）：本节点 owner 确认；新版本号必须大于当前版本。 */
export function AdoptDialog({
  api,
  orgId,
  candidateVersion,
  adoptedVersion,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly orgId: string;
  readonly candidateVersion: number;
  readonly adoptedVersion: number | null;
  readonly onClose: () => void;
}) {
  const [message, setMessage] = useState<string | null>(null);
  const leadRef = useRef<HTMLParagraphElement>(null);
  const { busy, run } = useBusy();
  const invalid = adoptedVersion !== null && candidateVersion <= adoptedVersion;

  async function adopt(): Promise<void> {
    const result = await run(() => api.gateway.adoptCandidate(orgId, candidateVersion));
    if (!result) return;
    if (result.kind === 'ok') {
      api.notify(`本节点已采信版本 ${candidateVersion}。`);
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={`采信版本 ${candidateVersion}？`}
      onClose={onClose}
      initialFocusRef={leadRef}
      devUi="DEV-UI-ORG-013"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '确认采信',
          variant: 'primary',
          loading: busy,
          disabled: invalid,
          onClick: () => void adopt(),
        },
      ]}
    >
      <p ref={leadRef} tabIndex={-1} className="org-dialog__lead">
        采信只作用于本节点。当前版本 {adoptedVersion ?? '无'}，候选版本 {candidateVersion}。
      </p>
      {invalid ? (
        <Notice tone="warning" role="alert">
          新版本号必须大于当前版本，不能采信。
        </Notice>
      ) : null}
      {message ? (
        <Notice tone="danger" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}

/**
 * 离开组织（P16）：预览后输入组织名确认；预览变化时重新确认；
 * 提交后显示“已提交，等待处理”，不立即从切换器移除。
 */
export function LeaveOrgDialog({
  api,
  org,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly org: OrgSummary;
  readonly onClose: () => void;
}) {
  const [preview, setPreview] = useState<RemovalPreview | null>(null);
  const [typed, setTyped] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const [previous, setPrevious] = useState<RemovalPreview | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const leadRef = useRef<HTMLParagraphElement>(null);
  const { busy, run } = useBusy();

  async function loadPreview(): Promise<void> {
    const result = await api.gateway.previewLeave(org.id);
    if (result.kind === 'ok') {
      setPreview(result.value);
    } else {
      setMessage(resultMessage(result));
    }
  }

  useEffect(() => {
    void loadPreview();
  }, []); // 只在打开时读取一次预览。

  async function confirm(): Promise<void> {
    if (!preview || typed.trim() !== org.name) {
      inputRef.current?.focus();
      return;
    }
    const result = await run(() => api.gateway.leaveOrg(org.id, preview.revision));
    if (!result) return;
    if (result.kind === 'failed' && result.code === 'stale') {
      setChanged(true);
      setTyped('');
      setPrevious(preview);
      setPreview(null);
      await loadPreview();
      return;
    }
    if (result.kind === 'submitted' || result.kind === 'ok') {
      setSubmitted(true);
      api.notify(`${ORG_COPY.leaveOrg}：${ORG_COPY.submitted}`);
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={submitted ? zh`已提交：离开 ${org.name}` : zh`离开 ${org.name}？`}
      onClose={onClose}
      initialFocusRef={submitted ? leadRef : inputRef}
      devUi="DEV-UI-ORG-013"
      actions={
        submitted
          ? [{ label: '知道了', variant: 'primary', onClick: onClose }]
          : [
              { label: '取消', onClick: onClose },
              {
                label: busy ? ORG_COPY.processing : changed ? '再次确认离开' : '确认离开',
                variant: 'danger',
                loading: busy,
                disabled: preview === null || typed.trim() !== org.name,
                onClick: () => void confirm(),
              },
            ]
      }
    >
      {submitted ? (
        <>
          <p ref={leadRef} tabIndex={-1} className="org-dialog__lead org-dialog__lead--submitted">
            {ORG_COPY.submitted}
          </p>
          <RemovalStages />
          <p className="org-muted">收到事件或刷新后，该组织才会从切换器中消失。</p>
        </>
      ) : (
        <>
          {changed ? (
            <Notice tone="warning" role="alert">
              {ORG_COPY.previewChanged}
            </Notice>
          ) : null}
          {preview ? (
            <RemovalPreviewView preview={preview} previous={previous} />
          ) : !message ? (
            <p role="status">正在读取预览…</p>
          ) : null}
          <Field label={ORG_COPY.typeNameToConfirm(org.name)} htmlFor="org-leave-confirm" required>
            <input
              id="org-leave-confirm"
              ref={inputRef}
              className="org-input"
              autoComplete="off"
              spellCheck={false}
              value={typed}
              disabled={busy}
              onChange={(event) => setTyped(event.target.value)}
            />
          </Field>
        </>
      )}
      {message ? (
        <Notice tone="danger" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
