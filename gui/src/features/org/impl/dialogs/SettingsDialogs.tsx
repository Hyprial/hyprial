import { useEffect, useRef, useState } from 'react';
import { Field, Notice } from '../../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { OrgDeletePreview, OrgResult, OrgSummary, RemovalPreview } from '../domain/types';
import { validateOrgName } from '../domain/validation';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { focusSoon, useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';
import { RemovalPreviewView, RemovalStages } from './RemovalPreviewView';

function leaveMessage(result: OrgResult<unknown>): string | null {
  if (result.kind === 'failed' && result.code === 'LAST_AUTHORITY_REFUSED') {
    return ORG_COPY.authorityCannotLeave;
  }
  return resultMessage(result);
}

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
    const renameOrg = api.gateway.renameOrg;
    if (!renameOrg) return;
    const result = await run(() => renameOrg(org.id, name.trim()));
    if (!result || result.kind === 'unavailable') return;
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

/**
 * 删除组织（OC7 两步删除）：打开时读取一次 org.delete 预览并展示其他受影响设备、管理设备与
 * 不可恢复；owner 输入组织名后点确认，带预览的 operationId 确认。只有阶段为 complete 才提示
 * 已删除；预览已是 complete（别处已删完）同样按已删除关闭。确认失败后显示网关文案，并自动
 * 重新读取预览（恢复读取，不是第二个用户动作），下一次确认绑定新的 operationId。
 * 打开时的预览用 ref 只发一次：开发模式下 React 会对同一实例重复执行挂载 effect。
 * 输入框在预览读取期间保持可用、作为初始焦点（确认按钮仍要等预览），失败后焦点回到输入框；
 * 对话框关闭后到达的应答不再调用 onClose 或通知。请求进行中不能取消对话框。
 */
export function DeleteOrgDialog({
  api,
  org,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly org: OrgSummary;
  readonly onClose: () => void;
}) {
  const [preview, setPreview] = useState<OrgDeletePreview | null>(null);
  const previewRequested = useRef(false);
  const mounted = useRef(true);
  const [typed, setTyped] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [reread, setReread] = useState<'idle' | 'loading' | 'done' | 'failed'>('idle');
  const inputRef = useRef<HTMLInputElement>(null);
  const { busy, run } = useBusy();
  const matches = typed.trim() === org.name;

  function deleted(): void {
    api.notify(`已删除组织：${org.name}`);
    onClose();
  }

  async function loadPreview(afterFailure: boolean): Promise<void> {
    const result = await api.labelled(ORG_COPY.deleteOrg).previewDelete(org.id);
    if (!mounted.current) return;
    if (result.kind === 'unavailable') {
      onClose();
    } else if (result.kind === 'ok') {
      if (result.value.phase === 'complete') {
        deleted();
        return;
      }
      setPreview(result.value);
      if (afterFailure) setReread('done');
    } else if (afterFailure) {
      setReread('failed');
    } else {
      setMessage(resultMessage(result));
    }
  }

  useEffect(() => {
    mounted.current = true;
    if (!previewRequested.current) {
      previewRequested.current = true;
      void loadPreview(false);
    }
    return () => {
      mounted.current = false;
    };
  }, []);

  async function confirm(): Promise<void> {
    if (!preview || !matches) {
      inputRef.current?.focus();
      return;
    }
    const result = await run(() => api.labelled('确认删除').deleteOrg(org.id, preview.operationId));
    if (!result || result.kind === 'unavailable' || !mounted.current) return;
    if (result.kind === 'ok' || result.kind === 'submitted') {
      deleted();
      return;
    }
    setMessage(resultMessage(result));
    setPreview(null);
    setTyped('');
    setReread('loading');
    focusSoon(inputRef);
    await loadPreview(true);
  }

  function dismiss(): void {
    if (!busy) onClose();
  }

  const loading = preview === null && (reread === 'loading' || (reread === 'idle' && !message));

  return (
    <OrgDialog
      title={zh`删除 ${org.name}`}
      description="删除后不能恢复；不安全的删除会被拒绝。"
      onClose={dismiss}
      initialFocusRef={inputRef}
      devUi="DEV-UI-ORG-013"
      actions={[
        { label: '取消', onClick: dismiss, disabled: busy },
        {
          label: busy ? ORG_COPY.processing : '确认删除',
          variant: 'danger',
          loading: busy,
          disabled: preview === null || !matches,
          onClick: () => void confirm(),
        },
      ]}
    >
      {preview ? (
        <>
          {preview.phase !== 'pending-confirmation' ? (
            <Notice tone="warning">{ORG_COPY.deleteResume(preview.phase)}</Notice>
          ) : reread === 'done' ? (
            <Notice tone="warning">{ORG_COPY.deletePreviewReread}</Notice>
          ) : null}
          <dl className="org-facts">
            <div>
              <dt>其他受影响的设备</dt>
              <dd>
                {preview.devices.length > 0 ? (
                  <ul className="org-device-list">
                    {preview.devices.map((device) => (
                      <li key={device}>{device}</li>
                    ))}
                  </ul>
                ) : (
                  '没有其他设备'
                )}
              </dd>
            </div>
            <div>
              <dt>管理设备</dt>
              <dd>{preview.authority ?? '未知'}</dd>
            </div>
            {preview.irreversible ? (
              <div>
                <dt>能否恢复</dt>
                <dd>删除后不能恢复</dd>
              </div>
            ) : null}
          </dl>
        </>
      ) : loading ? (
        <p role="status">正在读取预览…</p>
      ) : null}
      <Field
        label="输入组织名确认"
        htmlFor="org-delete-input"
        hint={ORG_COPY.typeNameToConfirm(org.name)}
        required
      >
        <input
          id="org-delete-input"
          ref={inputRef}
          className="org-input"
          autoComplete="off"
          spellCheck={false}
          value={typed}
          disabled={busy}
          onChange={(event) => setTyped(event.target.value)}
        />
      </Field>
      {message ? (
        <Notice tone="danger" role="alert">
          {message}
          {reread === 'failed' ? ` ${ORG_COPY.deletePreviewRereadFailed}` : ''}
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
    const adoptCandidate = api.gateway.adoptCandidate;
    if (!adoptCandidate) return;
    const result = await run(() => adoptCandidate(orgId, candidateVersion));
    if (!result || result.kind === 'unavailable') return;
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

  const confirmLabel = changed ? '再次确认离开' : '确认离开';

  async function loadPreview(subject: string = ORG_COPY.leaveOrg): Promise<void> {
    const result = await api.labelled(subject).previewLeave(org.id);
    if (result.kind === 'unavailable') {
      onClose();
    } else if (result.kind === 'ok') {
      setPreview(result.value);
    } else {
      setMessage(leaveMessage(result));
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
    const result = await run(() => api.labelled(confirmLabel).leaveOrg(org.id, preview.revision));
    if (!result || result.kind === 'unavailable') return;
    if (result.kind === 'failed' && result.code === 'stale') {
      setChanged(true);
      setTyped('');
      setPrevious(preview);
      setPreview(null);
      await loadPreview(confirmLabel);
      return;
    }
    if (result.kind === 'submitted' || result.kind === 'ok') {
      setSubmitted(true);
      api.notify(`${ORG_COPY.leaveOrg}：${ORG_COPY.submitted}`);
      return;
    }
    setMessage(leaveMessage(result));
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
                label: busy ? ORG_COPY.processing : confirmLabel,
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
