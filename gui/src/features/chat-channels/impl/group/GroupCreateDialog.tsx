/**
 * Decision record (create group dialog, slice GC5-3, 2026-11-21):
 * - Controlled modal built on shared `Dialog`, `Field`, `Notice`, and `Button`
 *   primitives. Opening the dialog triggers zero backend requests.
 * - Initial focus lands on the title field; Escape closes when not pending,
 *   Tab stays trapped inside the dialog, and focus returns to the trigger button
 *   on close via `Dialog`.
 * - Rejections render in-place via `Notice`; lost/ambiguous responses render
 *   「结果未知。」with a 「重试」 button that invokes `onRetry` (preserving the
 *   in-memory `idempotencyKey`).
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { Button, Dialog, Field, Notice } from '../../../../components/primitives';
import type { GroupCreateMutationState, GroupMemberCandidate } from './group-admin';
import './group-admin.css';

export interface GroupCreateDialogProps {
  readonly open: boolean;
  readonly candidates: readonly GroupMemberCandidate[];
  readonly state: GroupCreateMutationState;
  readonly onSubmit: (title: string, members: readonly string[]) => void | Promise<unknown>;
  readonly onRetry: () => void | Promise<unknown>;
  readonly onDismissState?: () => void;
  readonly onClose: () => void;
}

export function GroupCreateDialog({
  open,
  candidates,
  state,
  onSubmit,
  onRetry,
  onDismissState,
  onClose,
}: GroupCreateDialogProps) {
  const titleInputId = useId();
  const titleInputRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState('');
  const [selectedMembers, setSelectedMembers] = useState<readonly string[]>([]);
  const [titleError, setTitleError] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!open) {
      setTitle('');
      setSelectedMembers([]);
      setTitleError(undefined);
    }
  }, [open]);

  const pending = state.status === 'pending';

  function toggleMember(memberId: string): void {
    if (pending) return;
    if (state.status !== 'idle') onDismissState?.();
    setSelectedMembers((current) =>
      current.includes(memberId)
        ? current.filter((item) => item !== memberId)
        : [...current, memberId],
    );
  }

  function handleClose(): void {
    if (pending) return;
    onDismissState?.();
    onClose();
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (pending) return;
    const trimmed = title.trim();
    if (trimmed.length === 0) {
      setTitleError('请输入群聊名称。');
      titleInputRef.current?.focus();
      return;
    }
    if (trimmed.length > 200) {
      setTitleError('群聊名称最多 200 个字符。');
      titleInputRef.current?.focus();
      return;
    }
    setTitleError(undefined);
    void onSubmit(trimmed, selectedMembers);
  }

  return (
    <Dialog
      open={open}
      title="新建群聊"
      onClose={handleClose}
      closeOnEscape={!pending}
      initialFocusRef={titleInputRef}
    >
      <form className="group-create-dialog__form" onSubmit={handleSubmit} noValidate>
        {state.status === 'rejected' && (
          <Notice tone="danger" onDismiss={onDismissState}>
            {state.message}
          </Notice>
        )}
        {state.status === 'unknown' && (
          <Notice
            tone="warning"
            action={{ label: '重试', onClick: () => { void onRetry(); }, disabled: pending }}
            onDismiss={onDismissState}
          >
            {state.message}
          </Notice>
        )}
        <Field label="群聊名称" htmlFor={titleInputId} required error={titleError}>
          <input
            ref={titleInputRef}
            id={titleInputId}
            type="text"
            value={title}
            maxLength={240}
            disabled={pending}
            placeholder="例如 release"
            onChange={(event) => {
              setTitle(event.target.value);
              if (titleError) setTitleError(undefined);
              if (state.status !== 'idle') onDismissState?.();
            }}
          />
        </Field>
        <fieldset className="group-create-dialog__members">
          <legend className="group-create-dialog__legend">选择本机 Agent 成员</legend>
          {candidates.length === 0 ? (
            <p className="group-create-dialog__empty">暂无可加入的本机 Agent。</p>
          ) : (
            <div className="group-create-dialog__member-list">
              {candidates.map((candidate) => {
                const checked = selectedMembers.includes(candidate.memberId);
                return (
                  <label key={candidate.memberId} className="group-create-dialog__member-option">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={pending}
                      onChange={() => toggleMember(candidate.memberId)}
                    />
                    <span className="group-create-dialog__member-name">{candidate.displayName}</span>
                    <span className="group-create-dialog__member-id">{candidate.memberId}</span>
                  </label>
                );
              })}
            </div>
          )}
        </fieldset>
        <div className="ui-dialog__actions">
          <Button type="button" variant="secondary" disabled={pending} onClick={handleClose}>
            取消
          </Button>
          <Button type="submit" variant="primary" loading={pending} disabled={pending}>
            创建
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
