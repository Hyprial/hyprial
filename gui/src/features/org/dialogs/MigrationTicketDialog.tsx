import { useRef, useState } from 'react';
import { Notice } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { MigrationStatus, MigrationTicket, OrgResult } from '../domain/types';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { useBusy } from '../workspace/controls';
import { OrgDialog } from './OrgDialog';
import { MigrationPlanView, MigrationProgressView } from './MigrationPlanView';

type Step = 'review' | 'accept' | 'reject' | 'cancel';

/** 目标方已同意、迁移尚未结束：参与方在完成前都可以取消（P13“结束”）。 */
const CANCELLABLE: readonly MigrationStatus[] = ['verifying', 'plan-conflict', 'ready', 'running'];

/**
 * 目标端处理迁入请求（P13）：核对同一迁移单后“同意”或“拒绝”；同意后、完成前可以“取消迁移”。
 * 每个决定都在同一对话框内切换到确认步骤；成功后关闭，焦点回到该条迁入请求；
 * 失败时留在确认步骤显示原因，可以重试或返回。状态以后端返回为准。
 */
export function MigrationTicketDialog({
  api,
  ticket,
  onClose,
}: {
  readonly api: WorkspaceApi;
  readonly ticket: MigrationTicket;
  readonly onClose: () => void;
}) {
  const [step, setStep] = useState<Step>('review');
  const [message, setMessage] = useState<string | null>(null);
  const { busy, run } = useBusy();
  const leadRef = useRef<HTMLParagraphElement>(null);
  const confirmRef = useRef<HTMLParagraphElement>(null);
  const actionable = ticket.status === 'awaiting-target';
  const cancellable = CANCELLABLE.includes(ticket.status);

  function open(next: Step): void {
    setMessage(null);
    setStep(next);
  }

  async function submit(): Promise<void> {
    setMessage(null);
    const task: () => Promise<OrgResult> =
      step === 'cancel'
        ? () => api.gateway.cancelMigration(ticket.id)
        : () => api.gateway.decideMigration(ticket.id, step === 'accept' ? 'accept' : 'reject');
    const result = await run(task);
    if (!result) return;
    const action = step === 'cancel' ? '取消' : step === 'accept' ? '同意' : '拒绝';
    if (result.kind === 'ok') {
      api.notify(
        step === 'cancel'
          ? `迁移单 ${ticket.ticketNo} 已取消。`
          : `迁移单 ${ticket.ticketNo}：已提交${action}，等待源端处理。`,
      );
      onClose();
      return;
    }
    if (result.kind === 'submitted') {
      api.notify(`迁移单 ${ticket.ticketNo}：已提交${action}，等待处理。`);
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  if (step !== 'review') {
    const copy = {
      accept: {
        title: '确认同意迁入？',
        confirm: '确认同意',
        lead: zh`同意后，迁移单经过验证、状态变为“可以开始”时，源端才能开始迁移 ${ticket.agentName}。`,
      },
      reject: {
        title: '确认拒绝迁入？',
        confirm: '确认拒绝',
        lead: zh`拒绝后，${ticket.agentName} 不会迁入这台电脑；源端需要重新发起。`,
      },
      cancel: {
        title: '确认取消迁移？',
        confirm: '确认取消迁移',
        lead:
          ticket.status === 'running'
            ? zh`迁移已经开始。取消后迁移单结束，${ticket.agentName} 不会迁入这台电脑；已传输部分的处理以后端返回为准。`
            : zh`取消后迁移单结束，${ticket.agentName} 不会迁入这台电脑；要再迁移，源端需要重新发起，双方重新同意。`,
      },
    }[step];
    return (
      <OrgDialog
        title={copy.title}
        onClose={onClose}
        initialFocusRef={confirmRef}
        devUi="DEV-UI-ORG-014"
        actions={[
          // 取消迁移的确认框里不用“取消”作退出按钮，避免与“取消迁移”混淆。
          { label: step === 'cancel' ? '返回' : '返回核对', onClick: () => open('review') },
          {
            label: busy ? ORG_COPY.processing : copy.confirm,
            variant: step === 'accept' ? 'primary' : 'danger',
            loading: busy,
            onClick: () => void submit(),
          },
        ]}
      >
        <p ref={confirmRef} tabIndex={-1} className="org-dialog__lead">
          {copy.lead}
        </p>
        <p className="org-muted">
          迁移单 {ticket.ticketNo}，版本 {ticket.version}。迁移单不可变；要改条件只能取消后重新发起。
        </p>
        {message ? (
          <Notice tone="danger" role="alert">
            {message}
          </Notice>
        ) : null}
      </OrgDialog>
    );
  }

  return (
    <OrgDialog
      title={`迁移单 ${ticket.ticketNo}`}
      onClose={onClose}
      initialFocusRef={leadRef}
      wide
      devUi="DEV-UI-ORG-014"
      actions={
        actionable
          ? [
              { label: '拒绝', variant: 'danger', onClick: () => open('reject') },
              { label: '同意', variant: 'primary', onClick: () => open('accept') },
            ]
          : cancellable
            ? [
                { label: '关闭', onClick: onClose },
                { label: '取消迁移', variant: 'danger', onClick: () => open('cancel') },
              ]
            : [{ label: '关闭', variant: 'primary', onClick: onClose }]
      }
    >
      <p ref={leadRef} tabIndex={-1} className="org-dialog__lead">
        {actionable
          ? '核对迁移单的编号、版本、来源和 7 项后再决定。'
          : cancellable
            ? '迁移完成前，参与方都可以取消这张迁移单。'
            : '这张迁移单已经结束。'}
      </p>
      {/* 迁移开始后进度最重要，放在计划前面（与源端一致）。 */}
      {ticket.progress ? (
        <>
          <h4 className="org-subheading">迁移进度</h4>
          <MigrationProgressView progress={ticket.progress} />
        </>
      ) : null}
      <MigrationPlanView ticket={ticket} />
    </OrgDialog>
  );
}
