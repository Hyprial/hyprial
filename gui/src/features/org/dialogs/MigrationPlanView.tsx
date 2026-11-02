import { MIGRATION_STATUS_LABELS } from '../domain/copy';
import type { MigrationProgress, MigrationTicket } from '../domain/types';

/**
 * 迁移单 7 项：源端与目标端核对同一份计划；缺项显示“未知”、原因与责任方。
 * `draft` 用于发起前核对：还没有后端状态，显示“未发起”。
 */
export function MigrationPlanView({
  ticket,
  draft = false,
}: {
  readonly ticket: MigrationTicket;
  readonly draft?: boolean;
}) {
  return (
    <div className="org-migration-plan">
      <dl className="org-facts">
        <div>
          <dt>迁移单编号</dt>
          <dd>{ticket.ticketNo}</dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>{ticket.version}</dd>
        </div>
        <div>
          <dt>来源 Agent</dt>
          <dd>{ticket.agentName}</dd>
        </div>
        <div>
          <dt>源机器 → 目标机器</dt>
          <dd>
            {ticket.sourceMachine} → {ticket.targetMachine}
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{draft ? '未发起' : MIGRATION_STATUS_LABELS[ticket.status]}</dd>
        </div>
      </dl>
      <h4 className="org-subheading">迁移计划 7 项</h4>
      <dl className="org-facts org-plan-items">
        {ticket.items.map((item) => (
          <div key={item.key}>
            <dt>{item.label}</dt>
            <dd>
              {item.value ?? (
                <span className="org-unknown-item">
                  未知：{item.reason ?? '原因未提供'}
                  {item.owner ? `（责任方：${item.owner}）` : ''}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** 进度：阶段、字节、文件数、速率、最近更新、重试原因；不估算剩余时间。 */
export function MigrationProgressView({ progress }: { readonly progress: MigrationProgress }) {
  return (
    <dl className="org-facts" aria-label="迁移进度">
      <div>
        <dt>阶段</dt>
        <dd>{progress.phase}</dd>
      </div>
      <div>
        <dt>已传</dt>
        <dd>
          {progress.bytes} · {progress.files}
        </dd>
      </div>
      <div>
        <dt>速率</dt>
        <dd>{progress.rate}</dd>
      </div>
      <div>
        <dt>最近更新</dt>
        <dd>{progress.lastProgress}</dd>
      </div>
      {progress.retryReason ? (
        <div>
          <dt>重试原因</dt>
          <dd>{progress.retryReason}</dd>
        </div>
      ) : null}
    </dl>
  );
}
