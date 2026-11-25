import { useEffect, useState, type Dispatch } from 'react';
import { Button, Card, Icon } from '../components/primitives';
import { getRecentChatTarget } from '../state/selectors';
import type { AppAction, AppState } from '../state/types';
import { OrgReminderCards, PostLoginInvites } from '../features/org';
import { useGuiCapabilities } from '../app/capabilities';
import { usePageAvailable } from '../app/page-availability';
import { useAgentDirectory } from '../features/agents';
import { agentStatusCounts, platformBindingCounts, type StartStatusCounts, type StartSummary } from './start/start-summary';
import './start/start-page.css';

interface StartPageProps {
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly summary?: StartSummary;
  readonly onLoginRequired?: () => void;
  /** The live first-run receipt is not complete; the wizard is the resume path. */
  readonly firstRunPending?: boolean;
  readonly onResumeFirstRun?: () => void;
}

function statusValue(counts: StartStatusCounts | undefined): string {
  if (!counts || counts.normal === null || counts.abnormal === null) {
    return '未确认';
  }
  return `${counts.normal} / ${counts.abnormal}`;
}

function statusDetail(counts: StartStatusCounts | undefined): string | null {
  if (!counts || counts.normal === null || counts.abnormal === null) {
    return null;
  }
  return '正常 / 异常';
}

export function StartPage({ state, dispatch, summary, onLoginRequired, firstRunPending, onResumeFirstRun }: StartPageProps) {
  const agentPageAvailable = usePageAvailable('contacts');
  const connectionsPageAvailable = usePageAvailable('connections');
  const recentChat = onLoginRequired ? null : getRecentChatTarget(state);
  const { platformIntegrations, agents } = useGuiCapabilities();
  const directory = useAgentDirectory(onLoginRequired || summary?.agents ? undefined : agents);
  const agentCounts = onLoginRequired ? undefined : summary?.agents ??
    (directory.status === 'ready' ? agentStatusCounts(directory.rows) : undefined);
  const agentsSource = Boolean(summary?.agents || agents);
  const platformSource = Boolean(summary?.platformBindings || platformIntegrations);
  const statusShown = agentsSource || platformSource || connectionsPageAvailable;
  const [livePlatformBindings, setLivePlatformBindings] = useState<StartStatusCounts>();
  const givenPlatformBindings = summary?.platformBindings;
  useEffect(() => {
    if (onLoginRequired || givenPlatformBindings || !platformIntegrations) return;
    let alive = true;
    setLivePlatformBindings(undefined);
    void platformIntegrations.list().then(rows => {
      if (alive) setLivePlatformBindings(platformBindingCounts(rows));
    }).catch(() => {
      if (alive) setLivePlatformBindings(undefined);
    });
    return () => { alive = false; };
  }, [givenPlatformBindings, platformIntegrations, onLoginRequired]);
  const platformBindings = onLoginRequired ? undefined : givenPlatformBindings ?? livePlatformBindings;

  return (
    <section className="page page-start" data-page="start">
      <h1 className="page-title" id="start-title" tabIndex={-1}>
        开始
      </h1>

      {!onLoginRequired && <><OrgReminderCards dispatch={dispatch} /><PostLoginInvites /></>}

      <div className="start-bento">
        {firstRunPending && onResumeFirstRun && (
          <Card
            as="section"
            padding="lg"
            className="recommendation-card"
            aria-labelledby="first-run-pending-title"
            data-start-block="first-run"
          >
            <h2 id="first-run-pending-title">完成首次设置</h2>
            <p>登录、初始化设备、配置运行方式，并确认一条真实回复。</p>
            <Button variant="primary" onClick={() => onResumeFirstRun()}>
              继续首次设置
            </Button>
          </Card>
        )}

        {agentPageAvailable && (
          <Card
            as="section"
            padding="lg"
            className="recommendation-card"
            aria-labelledby="recommendation-title"
            data-start-block="recommendation"
          >
            <h2 id="recommendation-title">给下一件事，找一位帮手</h2>
            <p>创建一个专注特定职责的 Agent。</p>
            <Button
              variant="primary"
              leadingIcon={<Icon name="plus" size="sm" />}
              onClick={() => onLoginRequired ? onLoginRequired() : dispatch({ type: 'open-agent-create' })}
            >
              创建 Agent
            </Button>
          </Card>
        )}

        {statusShown && <Card
          as="section"
          padding="lg"
          className="start-status"
          aria-label="概览"
          data-start-block="status"
        >
          <ul className="start-status__list">
            {agentsSource && <li className="start-status__item" data-summary="agents">
              <h2 className="start-status__label">Agent 状态</h2>
              <p className="start-status__value">{statusValue(agentCounts)}</p>
              {statusDetail(agentCounts) && <span className="start-status__detail">{statusDetail(agentCounts)}</span>}
            </li>}
            {platformSource && <li
              className="start-status__item"
              data-summary="platform-bindings"
              data-start-block="decision"
            >
              <h2 className="start-status__label" id="decision-title">平台绑定</h2>
              <p className="start-status__value">{statusValue(platformBindings)}</p>
              {statusDetail(platformBindings) && <span className="start-status__detail">{statusDetail(platformBindings)}</span>}
            </li>}
          </ul>
          {connectionsPageAvailable && <Button
            className="start-status__action"
            variant="text"
            size="compact"
            onClick={() => onLoginRequired ? onLoginRequired() : dispatch({ type: 'navigate', page: 'connections' })}
          >
            查看 Agent 设置
          </Button>}
        </Card>}

        {recentChat && (
          <Card
            as="section"
            padding="lg"
            className="recent-chat-card"
            data-start-block="recent-chat"
            aria-labelledby="recent-chat-title"
          >
            <div className="recent-chat-card__text">
              <span className="recent-chat-card__label">最近对话</span>
              <h2 id="recent-chat-title">{recentChat.displayName}</h2>
              <p className="recent-chat-card__role">{recentChat.role}</p>
            </div>
            <Button
              variant="secondary"
              onClick={() =>
                dispatch({ type: 'open-chat', targetId: recentChat.id })
              }
            >
              打开聊天
            </Button>
          </Card>
        )}
      </div>
    </section>
  );
}
