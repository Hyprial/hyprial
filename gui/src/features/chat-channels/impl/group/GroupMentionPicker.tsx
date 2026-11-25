/**
 * Decision record (group mention picker, slice GC5-4, 2026-11-21):
 * - Renders an `@` trigger button beside the composer actions and a multi-select
 *   popover listing current group agent members excluding self (`kind === 'agent'`
 *   and `memberId !== self`).
 * - Member loading delegates to `GroupAdminController.ensureMembersLoaded` so
 *   opening `@` issues at most one `conversations.get` when members have not yet
 *   been read (neither via 「成员」 nor group creation), and zero thereafter.
 * - Supports keyboard operation: Tab reaches the `@` button, Enter/Space opens
 *   the popover, and Escape (or the close button) closes it and restores focus
 *   to the `@` trigger button. Inline `@` typing autocomplete is intentionally
 *   out of scope.
 */
import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { Button, ErrorState, Icon, LoadingState } from '../../../../components/primitives';
import type { GroupMembersEntryState } from './group-admin';
import './group-admin.css';

export interface GroupMentionPickerProps {
  readonly groupId: string;
  readonly identity?: string;
  readonly entry?: GroupMembersEntryState;
  readonly selectedMemberIds: readonly string[];
  readonly onEnsureMembers: () => void | Promise<unknown>;
  readonly onToggleMember: (memberId: string) => void;
}

export function GroupMentionPicker({
  groupId,
  identity,
  entry,
  selectedMemberIds,
  onEnsureMembers,
  onToggleMember,
}: GroupMentionPickerProps) {
  const popoverId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  const detail = entry?.detail;
  const selfId = detail?.self ?? identity;
  const agentMembers = detail
    ? detail.members.filter((member) => member.kind === 'agent' && member.memberId !== selfId)
    : [];
  const selectedSet = new Set(selectedMemberIds);

  useEffect(() => {
    setOpen(false);
  }, [groupId]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !rootRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const checkbox = popoverRef.current?.querySelector<HTMLElement>('input:not([disabled])');
    (checkbox ?? popoverRef.current)?.focus({ preventScroll: true });
  }, [open, entry?.status]);

  function handleToggle(): void {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    void onEnsureMembers();
  }

  function closeAndRestoreFocus(): void {
    setOpen(false);
    triggerRef.current?.focus();
  }

  function handlePopoverKeyDown(event: ReactKeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeAndRestoreFocus();
    }
  }

  return (
    <div ref={rootRef} className="group-mention-picker">
      <Button
        ref={triggerRef}
        type="button"
        variant="secondary"
        size="sm"
        aria-label="@ 成员"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? popoverId : undefined}
        onClick={handleToggle}
      >
        {selectedMemberIds.length > 0 ? `@ (${selectedMemberIds.length})` : '@'}
      </Button>
      {open && (
        <div
          id={popoverId}
          ref={popoverRef}
          className="group-mention-picker__popover"
          role="dialog"
          aria-label="选择 @ 成员"
          tabIndex={-1}
          onKeyDown={handlePopoverKeyDown}
        >
          <div className="group-mention-picker__header">
            <span className="group-mention-picker__title">选择 @ 成员</span>
            <Button
              type="button"
              variant="ghost"
              size="compact"
              aria-label="关闭 @ 选择器"
              onClick={closeAndRestoreFocus}
            >
              <Icon name="x" size="sm" />
            </Button>
          </div>
          {entry?.status === 'loading' && !detail ? (
            <LoadingState title="正在读取成员…" />
          ) : entry?.status === 'error' && !detail ? (
            <ErrorState
              message={entry.message}
              retryable
              onRetry={() => { void onEnsureMembers(); }}
            />
          ) : agentMembers.length === 0 ? (
            <p className="group-mention-picker__empty">暂无可 @ 的 Agent 成员。</p>
          ) : (
            <div className="group-mention-picker__list" role="group" aria-label="可 @ 成员">
              {agentMembers.map((member) => {
                const checked = selectedSet.has(member.memberId);
                return (
                  <label key={member.memberId} className="group-mention-picker__option">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggleMember(member.memberId)}
                    />
                    <span className="group-mention-picker__name">{member.displayName}</span>
                    <span className="group-mention-picker__id">{member.memberId}</span>
                  </label>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
