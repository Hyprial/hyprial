/**
 * Decision record (group members panel, slice GC5-3, 2026-11-21):
 * - Renders inside the workspace right-hand info sidebar. Member loading occurs
 *   only when the user clicks 「成员」 in the group header or an explicit load/
 *   refresh action inside the panel.
 * - Shows each member's `displayName` and `kind`. When `canManageMembers` is
 *   true, renders the add-member control and a 「移除」 button for non-manager
 *   members (never for the owner/manager row).
 * - Removing a member requires confirmation via the shared `Dialog` primitive;
 *   rejections and `unknown` outcomes («结果未知。» + «重试») stay visible
 *   in-place with the original `idempotencyKey`.
 */
import { useEffect, useId, useState } from 'react';
import {
  Button,
  Dialog,
  ErrorState,
  Field,
  LoadingState,
  Notice,
  Select,
} from '../../../../components/primitives';
import type {
  GroupAddMemberState,
  GroupMember,
  GroupMemberCandidate,
  GroupMembersEntryState,
  GroupRemoveMemberState,
} from './group-admin';
import './group-admin.css';

export interface GroupMembersPanelProps {
  readonly groupId: string;
  readonly entry?: GroupMembersEntryState;
  readonly candidates: readonly GroupMemberCandidate[];
  readonly addState: GroupAddMemberState;
  readonly removeState: GroupRemoveMemberState;
  readonly onLoadMembers: () => void | Promise<unknown>;
  readonly onAddMember: (memberId: string) => void | Promise<unknown>;
  readonly onRetryAddMember: () => void | Promise<unknown>;
  readonly onDismissAddState?: () => void;
  readonly onRemoveMember: (memberId: string) => Promise<unknown>;
  readonly onRetryRemoveMember: () => Promise<unknown>;
  readonly onDismissRemoveState?: () => void;
}

function memberKindLabel(member: GroupMember): string {
  const base = member.kind === 'user' ? '用户' : 'Agent';
  return member.manager ? `${base} · 所有者` : base;
}

export function GroupMembersPanel({
  groupId,
  entry,
  candidates,
  addState,
  removeState,
  onLoadMembers,
  onAddMember,
  onRetryAddMember,
  onDismissAddState,
  onRemoveMember,
  onRetryRemoveMember,
  onDismissRemoveState,
}: GroupMembersPanelProps) {
  const addSelectId = useId();
  const [selectedCandidate, setSelectedCandidate] = useState('');
  const [addError, setAddError] = useState<string | undefined>(undefined);
  const [confirmingMember, setConfirmingMember] = useState<GroupMember | null>(null);

  const detail = entry?.detail;
  const existingIds = new Set(detail?.members.map((member) => member.memberId) ?? []);
  const addableCandidates = candidates.filter((candidate) => !existingIds.has(candidate.memberId));

  useEffect(() => {
    setSelectedCandidate('');
    setAddError(undefined);
    setConfirmingMember(null);
  }, [groupId]);

  useEffect(() => {
    if (selectedCandidate && !addableCandidates.some((item) => item.memberId === selectedCandidate)) {
      setSelectedCandidate('');
    }
  }, [addableCandidates, selectedCandidate]);

  const addPending = addState.status === 'pending' && addState.groupId === groupId;
  const addRejected = addState.status === 'rejected' && addState.groupId === groupId ? addState : undefined;
  const addUnknown = addState.status === 'unknown' && addState.groupId === groupId ? addState : undefined;

  const removePending = removeState.status === 'pending' && removeState.groupId === groupId;
  const removeRejected = removeState.status === 'rejected' && removeState.groupId === groupId ? removeState : undefined;
  const removeUnknown = removeState.status === 'unknown' && removeState.groupId === groupId ? removeState : undefined;

  function handleAddClick(): void {
    if (addPending) return;
    if (!selectedCandidate) {
      setAddError('请选择要添加的 Agent。');
      return;
    }
    setAddError(undefined);
    void onAddMember(selectedCandidate);
  }

  async function handleConfirmRemove(): Promise<void> {
    if (!confirmingMember || removePending) return;
    const outcome = await onRemoveMember(confirmingMember.memberId);
    if (outcome !== null && outcome !== undefined) {
      setConfirmingMember(null);
    }
  }

  async function handleRetryRemove(): Promise<void> {
    if (removePending) return;
    const outcome = await onRetryRemoveMember();
    if (outcome !== null && outcome !== undefined) {
      setConfirmingMember(null);
    }
  }

  function handleCloseConfirm(): void {
    if (removePending) return;
    onDismissRemoveState?.();
    setConfirmingMember(null);
  }

  if (!entry) {
    return (
      <section className="group-members-panel" aria-label="群成员管理">
        <div className="group-members-panel__header">
          <h3 className="group-members-panel__title">成员名单</h3>
          <Button variant="secondary" size="sm" onClick={() => { void onLoadMembers(); }}>
            查看成员
          </Button>
        </div>
      </section>
    );
  }

  if (entry.status === 'loading' && !detail) {
    return (
      <section className="group-members-panel" aria-label="群成员管理">
        <LoadingState title="正在读取成员名单" />
      </section>
    );
  }

  if (entry.status === 'error' && !detail) {
    return (
      <section className="group-members-panel" aria-label="群成员管理">
        <ErrorState message={entry.message} retryable onRetry={() => { void onLoadMembers(); }} />
      </section>
    );
  }

  if (!detail) return null;

  return (
    <section className="group-members-panel" aria-label="群成员管理">
      <div className="group-members-panel__header">
        <h3 className="group-members-panel__title">成员名单（{detail.members.length}）</h3>
        <Button
          variant="ghost"
          size="sm"
          loading={entry.status === 'loading'}
          disabled={entry.status === 'loading'}
          onClick={() => { void onLoadMembers(); }}
        >
          刷新成员
        </Button>
      </div>
      {entry.status === 'error' && (
        <Notice tone="danger" action={{ label: '重试', onClick: () => { void onLoadMembers(); } }}>
          {entry.message}
        </Notice>
      )}
      <ul className="group-members-panel__list">
        {detail.members.map((member) => {
          const isSelfOrManager = member.manager || (detail.self !== undefined && member.memberId === detail.self);
          const canRemove = detail.canManageMembers === true && !isSelfOrManager;
          return (
            <li key={member.memberId} className="group-members-panel__row" data-member-id={member.memberId}>
              <div className="group-members-panel__identity">
                <span className="group-members-panel__name">{member.displayName}</span>
                <span className="group-members-panel__kind">{memberKindLabel(member)}</span>
              </div>
              {canRemove && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`移除 ${member.displayName}`}
                  disabled={removePending}
                  onClick={() => {
                    onDismissRemoveState?.();
                    setConfirmingMember(member);
                  }}
                >
                  移除
                </Button>
              )}
            </li>
          );
        })}
      </ul>
      {detail.canManageMembers && (
        <div className="group-members-panel__add">
          {addRejected && (
            <Notice tone="danger" onDismiss={onDismissAddState}>
              {addRejected.message}
            </Notice>
          )}
          {addUnknown && (
            <Notice
              tone="warning"
              action={{ label: '重试', onClick: () => { void onRetryAddMember(); }, disabled: addPending }}
              onDismiss={onDismissAddState}
            >
              {addUnknown.message}
            </Notice>
          )}
          {addableCandidates.length === 0 ? (
            <p className="group-members-panel__empty">暂无可添加的本机 Agent。</p>
          ) : (
            <div className="group-members-panel__add-row">
              <Field label="添加成员" htmlFor={addSelectId} error={addError}>
                <Select
                  id={addSelectId}
                  value={selectedCandidate}
                  disabled={addPending}
                  onValueChange={(value) => {
                    setSelectedCandidate(value);
                    if (addError) setAddError(undefined);
                    if (addState.status !== 'idle') onDismissAddState?.();
                  }}
                  options={[
                    { value: '', label: '选择要添加的 Agent' },
                    ...addableCandidates.map((candidate) => ({
                      value: candidate.memberId,
                      label: candidate.displayName,
                    })),
                  ]}
                />
              </Field>
              <Button
                variant="secondary"
                size="sm"
                loading={addPending}
                disabled={addPending}
                onClick={handleAddClick}
              >
                添加
              </Button>
            </div>
          )}
        </div>
      )}
      <Dialog
        open={confirmingMember !== null}
        title="移除成员？"
        description={confirmingMember
          ? `将 ${confirmingMember.displayName} 移出当前群聊。移出后离开期间的历史消息不会再投递。`
          : undefined}
        onClose={handleCloseConfirm}
        closeOnEscape={!removePending}
        actions={[
          { label: '取消', onClick: handleCloseConfirm, variant: 'secondary', disabled: removePending },
          {
            label: '确认移除',
            onClick: () => { void handleConfirmRemove(); },
            variant: 'danger',
            loading: removePending,
            disabled: removePending,
          },
        ]}
      >
        {removeRejected && (
          <Notice tone="danger" onDismiss={onDismissRemoveState}>
            {removeRejected.message}
          </Notice>
        )}
        {removeUnknown && (
          <Notice
            tone="warning"
            action={{ label: '重试', onClick: () => { void handleRetryRemove(); }, disabled: removePending }}
            onDismiss={onDismissRemoveState}
          >
            {removeUnknown.message}
          </Notice>
        )}
      </Dialog>
    </section>
  );
}
