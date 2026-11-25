/**
 * implG4 (2026-11-02)：概览页签。
 *
 * - 需要注意：汇总当前已加载事实中的异常（Key 授权），条目带页签跳转；
 *   无异常只声明“已读取信息中暂无异常”，不宣称全局无异常；该句只在它概括的读取
 *   确实完成时出现，读取被拒绝或没有端口时没有证据，整块不渲染。
 * - 2026-11-22（G1）：移除连接记录（link.list）展示与失败重试块。
 * - 活动时间：agent.list 的 lastActiveAtMs 与 activityHints.agentHomeMtimeMs；
 *   两者都未上报时显示“活动时间未上报”，不编造活动流水。
 * - 更多信息：常显、横跨两列；harness / 模型 / 工作目录。
 *   模型在启动时写入，页面只显示当前记录值并提供“设置模型…”入口。
 *   home-census 的布尔/模式事实不进入详情页（无路径、对配置无帮助）。
 * - agent.list.capabilities 是任意 JSON 元数据，不投影成用户能力列表。
 */

import { Button, Icon } from '../../../../components/primitives';
import { Badge } from '../../../../components/Badge';
import type { AgentActivityFacts } from '../../../../state/agent-selectors';
import {
  OVERVIEW_NOTICES_EMPTY,
  formatMsLabel,
  type DetailTabId,
  type OverviewNotice,
} from './agent-detail-format';

export interface OverviewTabProps {
  readonly extension?: import('../agent-extensions').AgentExtensionView;
  readonly notices: readonly OverviewNotice[];
  readonly onJumpTo: (tab: DetailTabId) => void;
  readonly activity?: AgentActivityFacts;
  readonly onConfigureStart?: () => void;
  readonly configureDisabled?: boolean;
  /** Tabs this detail really has; a notice only offers a jump to one of them. */
  readonly availableTabs?: readonly DetailTabId[];
  /** True only when the reads the notices summarise succeeded; otherwise an empty block is not rendered. */
  readonly noAnomalyConfirmed?: boolean;
}

const JUMP_LABELS: Record<DetailTabId, string> = {
  overview: '去概览',
  bindings: '去平台绑定',
  credentials: '去凭据',
  relations: '去所属组织',
};

function activityFactsPresent(activity: AgentActivityFacts | undefined): boolean {
  return activity !== undefined
    && (activity.lastActiveAtMs !== null || activity.homeMtimeMs !== null);
}

export function OverviewTab({
  extension,
  notices,
  onJumpTo,
  activity,
  onConfigureStart,
  configureDisabled = false,
  availableTabs,
  noAnomalyConfirmed = false,
}: OverviewTabProps) {
  const hasActivity = activityFactsPresent(activity);
  const runKnown = extension !== undefined
    && Boolean(extension.harness ?? extension.preferredHarness ?? extension.model);
  const locationKnown = Boolean(activity?.homePath) || activity?.homeMtimeMs != null;
  const moreInfoVisible = runKnown || locationKnown;
  return (
    <div className="agent-detail-grid-two">
      {(notices.length > 0 || noAnomalyConfirmed) && (
        <section className="card agent-detail-card" data-testid="overview-notices" aria-label="需要注意">
          <h3 className="agent-detail-card-title">需要注意</h3>
          {notices.length === 0 ? (
            <p className="agent-manage-note" role="status">
              {OVERVIEW_NOTICES_EMPTY}
            </p>
          ) : (
            <div className="agent-table-scroll">
              <table className="agent-detail-table">
                <tbody>
                  {notices.map((notice) => (
                    <tr key={notice.key}>
                      <td>
                        <Badge tone={notice.tone}>{notice.chip}</Badge>
                      </td>
                      <td>{notice.text}</td>
                      <td>
                        {(availableTabs === undefined || availableTabs.includes(notice.jumpTo)) && (
                          <Button
                            variant="text"
                            size="compact"
                            className="agent-jump-link-layout"
                            onClick={() => onJumpTo(notice.jumpTo)}
                          >
                            {JUMP_LABELS[notice.jumpTo]}
                            <Icon name="caret-right" size="sm" />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section
        className="card agent-detail-card"
        data-testid="overview-activity"
        data-last-active={activity?.lastActiveAtMs ?? ''}
        aria-label="活动时间"
      >
        <h3 className="agent-detail-card-title">活动时间</h3>
        {hasActivity ? (
          <dl className="agent-kv">
            <dt>最后活跃</dt>
            <dd data-activity="last-active">
              {activity?.lastActiveAtMs !== null && activity?.lastActiveAtMs !== undefined
                ? formatMsLabel(activity.lastActiveAtMs)
                : '未上报'}
            </dd>
            <dt>home 最近修改</dt>
            <dd data-activity="home-mtime">
              {activity?.homeMtimeMs !== null && activity?.homeMtimeMs !== undefined
                ? formatMsLabel(activity.homeMtimeMs)
                : '未上报'}
            </dd>
          </dl>
        ) : (
          <p className="agent-manage-note" role="status">活动时间未上报。</p>
        )}
      </section>

      {moreInfoVisible && (
        <section
          className="card agent-detail-card agent-detail-card--wide"
          data-testid="agent-more-info"
          aria-label="更多信息"
        >
          <h3 className="agent-detail-card-title">更多信息</h3>
          {runKnown ? (
            <dl className="agent-kv">
              <dt>harness</dt>
              <dd data-testid="overview-harness">
                {extension?.harness ?? extension?.preferredHarness ?? '未设置'}
              </dd>
              <dt>模型</dt>
              <dd data-testid="overview-model" className="agent-model-value">
                <span>{extension?.model ?? '未设置'}</span>
                {onConfigureStart && (
                  <Button variant="text" size="compact" disabled={configureDisabled}
                    title={extension?.runtime === 'online' ? '请先停止 Agent' : undefined} onClick={onConfigureStart}>
                    设置模型…
                  </Button>
                )}
              </dd>
              {activity?.homePath && (
                <>
                  <dt>工作目录</dt>
                  <dd data-testid="overview-cwd">
                    {activity.homePath}
                    {onConfigureStart && (
                      <Button variant="text" size="compact" disabled={configureDisabled}
                        title={extension?.runtime === 'online' ? '请先停止 Agent' : undefined} onClick={onConfigureStart}>
                        设置工作目录…
                      </Button>
                    )}
                  </dd>
                </>
              )}
            </dl>
          ) : locationKnown ? (
            <dl className="agent-kv">
              <dt>工作目录</dt>
              <dd data-testid="overview-cwd">{activity?.homePath ?? '未上报'}</dd>
            </dl>
          ) : null}
        </section>
      )}
    </div>
  );
}
