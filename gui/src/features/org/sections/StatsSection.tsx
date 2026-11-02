import { useState } from 'react';
import { Select } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { OrgStats, StatsWindow } from '../domain/types';
import type { WorkspaceApi } from '../workspace/api';
import {
  SectionHeader,
  TabList,
  TabPanel,
  UnavailableBlock,
  UnknownBlock,
} from '../workspace/controls';

const WINDOWS: readonly { id: `${StatsWindow}`; label: string }[] = [
  { id: '1', label: '近 1 天' },
  { id: '7', label: '近 7 天' },
  { id: '30', label: '近 30 天' },
];

/** 每小时消息量折线；数值同时以文字摘要给出，不只靠图形。 */
function HourlyChart({ values, windowDays }: { readonly values: readonly number[]; readonly windowDays: string }) {
  const max = Math.max(1, ...values);
  const width = 320;
  const height = 96;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((value, index) => `${Math.round(index * step)},${Math.round(height - (value / max) * height)}`)
    .join(' ');
  const total = values.reduce((sum, value) => sum + value, 0);
  return (
    <figure className="org-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`每小时消息量：共 ${values.length} 个时段，合计 ${total} 条，峰值 ${max} 条`}
        preserveAspectRatio="none"
      >
        <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" />
      </svg>
      <div className="org-chart__axis" aria-hidden="true">
        <span>0 时</span>
        <span>6 时</span>
        <span>12 时</span>
        <span>18 时</span>
        <span>23 时</span>
      </div>
      <figcaption>
        每小时消息量{windowDays === '1' ? '' : `（近 ${windowDays} 天按小时汇总）`} · 合计 {total} 条 · 峰值 {max} 条
      </figcaption>
    </figure>
  );
}

/**
 * 统计（5.7）：窗口 1/7/30 天；视角为本机可见范围或某个 Agent。
 * 范围说明始终显示；按任务统计是缺口；未接通时不显示示例图表。
 */
export function StatsSection({ api }: { readonly api: WorkspaceApi }) {
  const { snapshot, detail } = api;
  const [windowId, setWindowId] = useState<`${StatsWindow}`>('7');
  const agents = detail?.agents ?? [];
  const requestedAgent = agents.find((agent) => agent.id === api.intent?.segment);
  const [perspective, setPerspective] = useState<string>(requestedAgent?.id ?? 'local');

  if (snapshot.status === 'unwired') {
    return (
      <div className="org-section" data-org-section="stats">
        <SectionHeader title="统计" />
        <UnavailableBlock text={ORG_COPY.statsUnavailable} />
        <p className="org-muted">{ORG_COPY.statsScope}</p>
      </div>
    );
  }

  const stats: OrgStats | null =
    detail?.stats?.[Number(windowId) as StatsWindow] ?? null;
  const agentName =
    perspective === 'local' ? null : (agents.find((agent) => agent.id === perspective)?.name ?? null);
  const pairs = stats
    ? stats.topPairs
        .filter((pair) => !agentName || pair.from === agentName || pair.to === agentName)
        .slice(0, 10)
    : [];
  const participants = stats
    ? stats.participants.filter(
        (row) => !agentName || row.label === agentName || pairs.some((pair) => pair.from === row.label || pair.to === row.label),
      )
    : [];
  const empty = stats !== null && stats.hourly.every((value) => value === 0) && pairs.length === 0;

  return (
    <div className="org-section" data-org-section="stats" data-dev-ui="DEV-UI-ORG-011">
      <SectionHeader
        title="统计"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      <p className="org-scope-note" role="note">
        {ORG_COPY.statsScope}
      </p>
      <div className="org-toolbar">
        <TabList
          items={WINDOWS}
          value={windowId}
          onChange={setWindowId}
          label="时间窗口"
          idPrefix="org-stats-window"
          variant="segments"
        />
        <div className="org-inline-field">
          <label htmlFor="org-stats-perspective">视角</label>
          <Select
            id="org-stats-perspective"
            className="org-select-inline"
            value={perspective}
            options={[
              { value: 'local', label: '本机可见范围' },
              ...agents.map((agent) => ({ value: agent.id, label: `Agent：${agent.name}` })),
            ]}
            onValueChange={setPerspective}
          />
        </div>
      </div>
      <TabPanel idPrefix="org-stats-window" value={windowId}>
        {stats === null ? (
          <UnknownBlock onRefresh={() => void api.refresh()} />
        ) : empty ? (
          <p className="org-empty-inline">这个时间窗口内没有通信记录。</p>
        ) : (
          <>
            <HourlyChart values={stats.hourly} windowDays={windowId} />
            <table className="org-table">
              <caption>关系 Top 10{agentName ? zh`（与 ${agentName} 相连）` : ''}</caption>
              <thead>
                <tr>
                  <th scope="col">发送方 → 接收方</th>
                  <th scope="col">消息数</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map((pair) => (
                  <tr key={`${pair.from}-${pair.to}`}>
                    <td>
                      {pair.from} → {pair.to}
                    </td>
                    <td>{pair.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="org-table">
              <caption>参与者</caption>
              <thead>
                <tr>
                  <th scope="col">参与者</th>
                  <th scope="col">发出</th>
                  <th scope="col">收到</th>
                </tr>
              </thead>
              <tbody>
                {participants.map((row) => (
                  <tr key={row.label}>
                    <td>{row.label}</td>
                    <td>{row.sent}</td>
                    <td>{row.received}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </TabPanel>
      <UnavailableBlock text={ORG_COPY.statsByTask} />
    </div>
  );
}
