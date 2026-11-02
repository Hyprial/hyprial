import { useEffect, useRef, useState } from 'react';
import { Notice, Stepper, type StepDefinition } from '../../../components/primitives';
import { MACHINE_STATUS_LABELS, ORG_COPY, zh } from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import type { MigrationCheck, MigrationTicket, OrgMachineRecord } from '../domain/types';
import { OrgDialog } from '../dialogs/OrgDialog';
import { MigrationPlanView } from '../dialogs/MigrationPlanView';
import { resultMessage } from '../workspace/api';
import { useBusy } from '../workspace/controls';

type Step = 'target' | 'check' | 'plan';

const STEPS: readonly { id: Step; label: string }[] = [
  { id: 'target', label: '选择目标机器' },
  { id: 'check', label: '检查' },
  { id: 'plan', label: '核对计划并发起' },
];

/**
 * 迁移向导（P13 源端）：只列同组织的机器，他人的机器禁用；显示可迁移性检查；
 * 核对后端生成的迁移单（编号、版本、7 项）后“发起”。发起不等于开始，
 * 开始要等目标方同意且后端返回“可以开始”。
 */
export function MigrationWizardDialog({
  agentId,
  agentName,
  onClose,
  onSubmitted,
}: {
  readonly agentId: string;
  readonly agentName: string;
  readonly onClose: () => void;
  readonly onSubmitted: (ticket: MigrationTicket) => void;
}) {
  const { snapshot, gateway } = useOrgDomain();
  const [step, setStep] = useState<Step>('target');
  const [machineId, setMachineId] = useState<string | null>(null);
  const [checks, setChecks] = useState<readonly MigrationCheck[] | null>(null);
  const [ticket, setTicket] = useState<MigrationTicket | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const { busy, run } = useBusy();

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  // 同组织的机器：Agent 所属组织内的机器，去掉 Agent 当前所在的机器。
  const orgIds = snapshot.agentOrgs?.[agentId] ?? [];
  const seen = new Set<string>();
  const candidates: OrgMachineRecord[] = [];
  let currentHost: string | null = null;
  for (const orgId of orgIds) {
    const detail = snapshot.details[orgId];
    const record = detail?.agents?.find((item) => item.id === agentId);
    currentHost = currentHost ?? record?.machineLabel ?? null;
    for (const machine of detail?.machines ?? []) {
      if (!seen.has(machine.id)) {
        seen.add(machine.id);
        candidates.push(machine);
      }
    }
  }
  const targets = candidates.filter((machine) => machine.hostname !== currentHost);

  async function runChecks(): Promise<void> {
    if (!machineId) {
      setMessage('请选择目标机器');
      return;
    }
    setMessage(null);
    const result = await run(() => gateway.checkMigration(agentId, machineId));
    if (!result) return;
    if (result.kind === 'ok') {
      setChecks(result.value);
      setStep('check');
      return;
    }
    setMessage(resultMessage(result));
  }

  async function plan(): Promise<void> {
    if (!machineId) return;
    setMessage(null);
    const result = await run(() => gateway.planMigration(agentId, machineId));
    if (!result) return;
    if (result.kind === 'ok') {
      setTicket(result.value);
      setStep('plan');
      return;
    }
    setMessage(resultMessage(result));
  }

  async function submit(): Promise<void> {
    if (!ticket) return;
    const result = await run(() => gateway.submitMigration(ticket.id));
    if (!result) return;
    if (result.kind === 'ok' || result.kind === 'submitted') {
      onSubmitted({ ...ticket, status: 'awaiting-target' });
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  const index = STEPS.findIndex((item) => item.id === step);
  const steps: StepDefinition[] = STEPS.map((item, i) => ({
    id: item.id,
    label: item.label,
    status: i < index ? 'completed' : i === index ? 'active' : 'pending',
  }));
  const checksPassed = checks !== null && checks.every((check) => check.ok);

  return (
    <OrgDialog
      title={zh`迁移 ${agentName}`}
      onClose={onClose}
      wide
      devUi="DEV-UI-ORG-016"
      actions={
        step === 'target'
          ? [
              { label: '取消', onClick: onClose },
              {
                label: busy ? ORG_COPY.processing : '下一步：检查',
                variant: 'primary',
                loading: busy,
                disabled: !machineId,
                onClick: () => void runChecks(),
              },
            ]
          : step === 'check'
            ? [
                { label: '上一步', onClick: () => setStep('target') },
                {
                  label: busy ? ORG_COPY.processing : '下一步：核对计划',
                  variant: 'primary',
                  loading: busy,
                  disabled: !checksPassed,
                  onClick: () => void plan(),
                },
              ]
            : [
                { label: '上一步', onClick: () => setStep('check') },
                {
                  label: busy ? ORG_COPY.processing : '发起',
                  variant: 'primary',
                  loading: busy,
                  onClick: () => void submit(),
                },
              ]
      }
    >
      <Stepper steps={steps} currentId={step} ariaLabel="迁移步骤" className="org-stepper" />
      <h3 ref={headingRef} tabIndex={-1} className="org-subheading">
        {STEPS[index].label}
      </h3>

      {step === 'target' ? (
        targets.length === 0 ? (
          <p className="org-empty-inline">没有可选的同组织机器。</p>
        ) : (
          <fieldset className="org-radio-group">
            <legend>同组织的机器</legend>
            {targets.map((machine) => (
              <label key={machine.id}>
                <input
                  type="radio"
                  name="org-migration-target"
                  disabled={!machine.mine}
                  checked={machineId === machine.id}
                  onChange={() => setMachineId(machine.id)}
                />
                <span>
                  {machine.hostname} · {MACHINE_STATUS_LABELS[machine.status]} · 主人 {machine.ownerLabel}
                  {!machine.mine ? (
                    <span className="org-row__meta">暂不支持迁移到他人的机器</span>
                  ) : null}
                </span>
              </label>
            ))}
          </fieldset>
        )
      ) : null}

      {step === 'check' && checks ? (
        <ul className="org-rows" aria-label="可迁移性检查">
          {checks.map((check) => (
            <li key={check.label} className="org-row">
              <span className="org-row__title">
                {check.ok ? '通过' : '未通过'}：{check.label}
              </span>
              <span className="org-row__meta">{check.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {step === 'check' && checks && !checksPassed ? (
        <Notice tone="warning">检查未通过，按原因处理后再继续。</Notice>
      ) : null}

      {step === 'plan' && ticket ? (
        <>
          <p className="org-muted">源端与目标端核对的是同一份迁移单；发起后等待目标方同意。</p>
          <MigrationPlanView ticket={ticket} draft />
        </>
      ) : null}

      {message ? (
        <Notice tone="danger" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
