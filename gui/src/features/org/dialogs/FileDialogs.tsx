import { useEffect, useRef, useState } from 'react';
import { Field, Notice } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { FileSaveConflict, OrgFileEntry, TrashEntry } from '../domain/types';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';
import { useDiscardGuard } from './useDiscardGuard';

interface CommonProps {
  readonly api: WorkspaceApi;
  readonly orgId: string;
  readonly spaceId: string;
  readonly onClose: () => void;
}

/**
 * 编辑器（P12）：保存后以后端返回的新版本为准，显示“已保存（版本 n）”。
 * 版本冲突时先给出两个选择并显示双方内容，编辑框收起；选择后回到编辑框重新保存，不静默覆盖。
 * 有未保存的修改时，退出前在同一对话框内确认。
 */
export function FileEditorDialog({
  api,
  orgId,
  spaceId,
  onClose,
  entry,
}: CommonProps & { readonly entry: OrgFileEntry }) {
  const [content, setContent] = useState(entry.content ?? '');
  // 后端当前的内容：打开时的内容，保存成功或出现冲突时随之更新；与它不同即有未保存的修改。
  const [savedContent, setSavedContent] = useState(entry.content ?? '');
  const [baseVersion, setBaseVersion] = useState(entry.version);
  const [conflict, setConflict] = useState<FileSaveConflict | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const backToEditor = useRef(false);
  const { busy, run } = useBusy();
  const guard = useDiscardGuard({ dirty: content !== savedContent, onClose, initialFocusRef: textRef });

  // 冲突出现时“保存”被禁用，焦点移到第一个选择；选择后焦点回到编辑框。
  useEffect(() => {
    if (conflict) {
      keepRef.current?.focus();
    } else if (backToEditor.current) {
      backToEditor.current = false;
      textRef.current?.focus();
    }
  }, [conflict]);

  function resolve(theirs: string | null): void {
    if (!conflict) return;
    if (theirs !== null) setContent(theirs);
    setSavedContent(conflict.theirs);
    setBaseVersion(conflict.theirVersion);
    backToEditor.current = true;
    setConflict(null);
  }

  async function save(): Promise<void> {
    setMessage(null);
    const result = await run(() =>
      api.gateway.saveFile(orgId, spaceId, entry.path, content, baseVersion),
    );
    if (!result) return;
    if (result.kind === 'ok') {
      setSaved(result.value);
      setSavedContent(content);
      setBaseVersion(result.value);
      setConflict(null);
      api.notify(`已保存（版本 ${result.value}）`);
      return;
    }
    if (result.kind === 'failed' && result.conflict) {
      setSavedContent(result.conflict.theirs);
      setConflict(result.conflict);
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={guard.confirming ? '放弃未保存的修改？' : zh`编辑 ${entry.name}`}
      description={guard.confirming ? undefined : `${entry.path} · 基于版本 ${baseVersion}`}
      onClose={guard.requestClose}
      initialFocusRef={guard.focusRef}
      wide
      devUi="DEV-UI-ORG-012"
      actions={
        guard.confirming
          ? [
              { label: '继续编辑', onClick: guard.resume },
              { label: '放弃修改', variant: 'danger', onClick: onClose },
            ]
          : [
              { label: '关闭', onClick: guard.requestClose },
              {
                label: busy ? ORG_COPY.processing : '保存',
                variant: 'primary',
                loading: busy,
                disabled: conflict !== null,
                onClick: () => void save(),
              },
            ]
      }
    >
      {guard.confirming ? (
        <p ref={guard.confirmRef} tabIndex={-1} className="org-dialog__lead">
          {zh`关闭后，对 ${entry.name} 的修改不会保留。要保留，先保存。`}
        </p>
      ) : null}
      <div className="org-editor" hidden={guard.confirming}>
        {conflict ? (
          <section className="org-conflict" aria-label="版本冲突">
            <Notice tone="warning" role="alert" title="版本冲突">
              文件已被别人改到版本 {conflict.theirVersion}。先选择保留哪一份，再保存；不会静默覆盖。
            </Notice>
            <div className="org-actions org-conflict__choices">
              <button ref={keepRef} type="button" className="button button-secondary" onClick={() => resolve(null)}>
                保留我的修改
              </button>
              <button type="button" className="button button-secondary" onClick={() => resolve(conflict.theirs)}>
                使用对方版本
              </button>
            </div>
            <div className="org-conflict__grid">
              <div>
                <h4 className="org-subheading">我的修改</h4>
                <pre className="org-preview__content">{content}</pre>
              </div>
              <div>
                <h4 className="org-subheading">对方版本 {conflict.theirVersion}</h4>
                <pre className="org-preview__content">{conflict.theirs}</pre>
              </div>
            </div>
          </section>
        ) : (
          <Field label="文件内容" htmlFor="org-file-editor">
            <textarea
              id="org-file-editor"
              ref={textRef}
              className="org-textarea"
              rows={10}
              value={content}
              disabled={busy}
              onChange={(event) => {
                setContent(event.target.value);
                setSaved(null);
              }}
            />
          </Field>
        )}
        <p role="status" className="org-copy-result">
          {saved !== null ? `已保存（版本 ${saved}）` : null}
        </p>
        {message ? (
          <Notice tone="danger" role="alert">
            {message}
          </Notice>
        ) : null}
      </div>
    </OrgDialog>
  );
}

function PathDialog({
  title,
  label,
  initial,
  confirmLabel,
  onClose,
  onSubmit,
  extra,
  devUi,
}: {
  readonly title: string;
  readonly label: string;
  readonly initial: string;
  readonly confirmLabel: string;
  readonly onClose: () => void;
  readonly onSubmit: (path: string) => Promise<string | null>;
  readonly extra?: React.ReactNode;
  readonly devUi: string;
}) {
  const [path, setPath] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { busy, run } = useBusy();

  async function submit(): Promise<void> {
    if (!path.trim()) {
      setError('请填写路径');
      inputRef.current?.focus();
      return;
    }
    const failure = await run(() => onSubmit(path.trim()));
    if (failure) {
      setError(failure);
    }
  }

  return (
    <OrgDialog
      title={title}
      onClose={onClose}
      initialFocusRef={inputRef}
      devUi={devUi}
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : confirmLabel,
          variant: 'primary',
          loading: busy,
          onClick: () => void submit(),
        },
      ]}
    >
      {extra}
      <Field label={label} htmlFor="org-file-path" error={error ?? undefined} required>
        <input
          id="org-file-path"
          ref={inputRef}
          className="org-input"
          autoComplete="off"
          spellCheck={false}
          value={path}
          disabled={busy}
          onChange={(event) => setPath(event.target.value)}
        />
      </Field>
    </OrgDialog>
  );
}

/** 新建文件或文件夹；选中文件夹时路径预填该文件夹，新建的条目随即选中。 */
export function NewEntryDialog({
  api,
  orgId,
  spaceId,
  onClose,
  initialPath,
  onCreated,
}: CommonProps & { readonly initialPath: string; readonly onCreated: (path: string | null) => void }) {
  const [kind, setKind] = useState<'file' | 'folder'>('file');
  return (
    <PathDialog
      title="新建"
      label="路径"
      initial={initialPath}
      confirmLabel="新建"
      devUi="DEV-UI-ORG-012"
      onClose={onClose}
      extra={
        <fieldset className="org-radio-group">
          <legend>类型</legend>
          <label>
            <input type="radio" checked={kind === 'file'} onChange={() => setKind('file')} />
            文件
          </label>
          <label>
            <input type="radio" checked={kind === 'folder'} onChange={() => setKind('folder')} />
            文件夹
          </label>
        </fieldset>
      }
      onSubmit={async (path) => {
        const result = await api.gateway.createEntry(orgId, spaceId, path, kind);
        if (result.kind === 'ok') {
          api.notify(zh`已新建 ${path}`);
          onCreated(path);
          onClose();
          return null;
        }
        return resultMessage(result);
      }}
    />
  );
}

export function MoveEntryDialog({
  api,
  orgId,
  spaceId,
  onClose,
  entry,
  onMoved,
}: CommonProps & { readonly entry: OrgFileEntry; readonly onMoved: (path: string | null) => void }) {
  return (
    <PathDialog
      title={zh`移动或重命名 ${entry.name}`}
      label="新路径"
      initial={entry.path}
      confirmLabel="移动"
      devUi="DEV-UI-ORG-012"
      onClose={onClose}
      onSubmit={async (path) => {
        const result = await api.gateway.moveEntry(orgId, spaceId, entry.path, path);
        if (result.kind === 'ok') {
          api.notify(zh`已移动到 ${path}`);
          onMoved(path);
          onClose();
          return null;
        }
        return resultMessage(result);
      }}
    />
  );
}

function ConfirmDialog({
  title,
  lead,
  confirmLabel,
  danger,
  onClose,
  onConfirm,
  devUi,
}: {
  readonly title: string;
  readonly lead: string;
  readonly confirmLabel: string;
  readonly danger?: boolean;
  readonly onClose: () => void;
  readonly onConfirm: () => Promise<string | null>;
  readonly devUi: string;
}) {
  const [message, setMessage] = useState<string | null>(null);
  const leadRef = useRef<HTMLParagraphElement>(null);
  const { busy, run } = useBusy();
  return (
    <OrgDialog
      title={title}
      onClose={onClose}
      initialFocusRef={leadRef}
      devUi={devUi}
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : confirmLabel,
          variant: danger ? 'danger' : 'primary',
          loading: busy,
          onClick: () =>
            void run(onConfirm).then((failure) => {
              if (failure) setMessage(failure);
            }),
        },
      ]}
    >
      <p ref={leadRef} tabIndex={-1} className="org-dialog__lead">
        {lead}
      </p>
      {message ? (
        <Notice tone="danger" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}

/** 删除进回收站；文件夹连同其中的内容一起，恢复时一起回到原路径。 */
export function DeleteEntryDialog({
  api,
  orgId,
  spaceId,
  onClose,
  entry,
  childCount,
  onDeleted,
}: CommonProps & { readonly entry: OrgFileEntry; readonly childCount: number; readonly onDeleted: () => void }) {
  return (
    <ConfirmDialog
      title={zh`删除 ${entry.name}？`}
      lead={
        entry.kind === 'file'
          ? '删除后文件进入回收站，可以从回收站恢复到原路径。'
          : childCount > 0
            ? `删除后，文件夹和其中的 ${childCount} 项进入回收站，可以从回收站恢复到原路径。`
            : '删除后文件夹进入回收站，可以从回收站恢复到原路径。'
      }
      confirmLabel="删除"
      danger
      devUi="DEV-UI-ORG-012"
      onClose={onClose}
      onConfirm={async () => {
        const result = await api.gateway.deleteEntry(orgId, spaceId, entry.path);
        if (result.kind === 'ok') {
          api.notify(`已移到回收站：${entry.path}`);
          onDeleted();
          onClose();
          return null;
        }
        return resultMessage(result);
      }}
    />
  );
}

export function RestoreDialog({
  api,
  orgId,
  spaceId,
  onClose,
  entry,
}: CommonProps & { readonly entry: TrashEntry }) {
  return (
    <ConfirmDialog
      title={zh`恢复 ${entry.path}？`}
      lead={entry.kind === 'folder' ? '恢复到原路径；文件夹中的内容一起恢复。' : '恢复到原路径。'}
      confirmLabel="恢复"
      devUi="DEV-UI-ORG-012"
      onClose={onClose}
      onConfirm={async () => {
        const result = await api.gateway.restoreEntry(orgId, spaceId, entry.id);
        if (result.kind === 'ok') {
          api.notify(`已恢复：${entry.path}`);
          onClose();
          return null;
        }
        return resultMessage(result);
      }}
    />
  );
}

/** 彻底清除：两步确认，在同一个对话框内完成。 */
export function PurgeDialog({
  api,
  orgId,
  spaceId,
  onClose,
  entry,
}: CommonProps & { readonly entry: TrashEntry }) {
  const [step, setStep] = useState<1 | 2>(1);
  const [message, setMessage] = useState<string | null>(null);
  const firstRef = useRef<HTMLParagraphElement>(null);
  const secondRef = useRef<HTMLParagraphElement>(null);
  const { busy, run } = useBusy();

  async function purge(): Promise<void> {
    const result = await run(() => api.gateway.purgeEntry(orgId, spaceId, entry.id));
    if (!result) return;
    if (result.kind === 'ok') {
      api.notify(`已彻底清除：${entry.path}`);
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={zh`彻底清除 ${entry.path}？`}
      onClose={onClose}
      initialFocusRef={step === 1 ? firstRef : secondRef}
      devUi="DEV-UI-ORG-012"
      actions={
        step === 1
          ? [
              { label: '取消', onClick: onClose },
              { label: '继续', variant: 'danger', onClick: () => setStep(2) },
            ]
          : [
              { label: '返回', onClick: () => setStep(1) },
              {
                label: busy ? ORG_COPY.processing : '确认彻底清除',
                variant: 'danger',
                loading: busy,
                onClick: () => void purge(),
              },
            ]
      }
    >
      {step === 1 ? (
        <p ref={firstRef} tabIndex={-1} className="org-dialog__lead">
          第 1 步，共 2 步：彻底清除后不能恢复{entry.kind === 'folder' ? '，文件夹中的内容一起清除' : ''}。
        </p>
      ) : (
        <p ref={secondRef} tabIndex={-1} className="org-dialog__lead">
          第 2 步，共 2 步：再次确认彻底清除 {entry.path}。
        </p>
      )}
      {message ? (
        <Notice tone="danger" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
