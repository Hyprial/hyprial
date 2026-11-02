import type { Dispatch } from 'react';
import type { StubOperations } from '../adapters/stub';
import { Button, Card } from '../components/primitives';
import { getRecentChatTarget } from '../state/selectors';
import type { AppAction, AppState } from '../state/types';
import { OrgReminderCards, PostLoginInvites } from '../features/org/crosstab/OrgReminderCards';

interface StartPageProps {
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly stub: StubOperations;
}

export function StartPage({ state, dispatch }: StartPageProps) {
  const recentChat = getRecentChatTarget(state);

  return (
    <section className="page page-start" data-page="start">
      <h1 className="page-title" id="start-title" tabIndex={-1}>
        开始
      </h1>

      {state.firstRun.status === 'complete' && (
        <p className="start-setup-status">
          本地首次设置流程已完成 · 真实 Squire 回复状态未确认
        </p>
      )}

      {/* dev-04：组织提醒卡与登录后的邀请确认；没有组织数据时都不渲染。 */}
      <OrgReminderCards dispatch={dispatch} />
      <PostLoginInvites />

      <div className="start-bento">
        <div className="metrics" aria-label="概览">
          <article className="card metric" data-summary="agents">
            <span className="metric-label">Agent 状态</span>
            <strong className="metric-value metric-value-copy">未确认</strong>
          </article>
          <article className="card metric" data-summary="tasks">
            <span className="metric-label">任务状态</span>
            <strong className="metric-value metric-value-copy">未确认</strong>
          </article>
          <article className="card metric" data-summary="decisions">
            <span className="metric-label">连接状态</span>
            <strong className="metric-value metric-value-copy">未确认</strong>
          </article>
        </div>

        <Card
          as="section"
          padding="lg"
          className="recommendation-card"
          aria-labelledby="recommendation-title"
          data-start-block="recommendation"
        >
          <span className="eyebrow">为你推荐</span>
          <h2 id="recommendation-title">给下一件事，找一位帮手</h2>
          <p>
            创建一个专注特定职责的 Agent，让想法有个开始。
          </p>
          <Button
            variant="primary"
            onClick={() => dispatch({ type: 'open-agent-create' })}
          >
            创建 Agent
          </Button>
        </Card>

        <section
          className="card decision-card"
          aria-labelledby="decision-title"
          data-start-block="decision"
        >
          <h2 className="section-title" id="decision-title">
            连接状态
          </h2>
          <p className="copy">连接信息尚未确认。</p>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => dispatch({ type: 'navigate', page: 'connections' })}
          >
            查看连接
          </button>
        </section>
        {recentChat && (
          <section
            className="card recent-chat-card"
            data-start-block="recent-chat"
            aria-labelledby="recent-chat-title"
          >
            <span className="eyebrow">最近对话</span>
            <h2 id="recent-chat-title">{recentChat.displayName}</h2>
            <p className="copy">{recentChat.role}</p>
            <button
              type="button"
              className="button button-secondary"
              onClick={() =>
                dispatch({ type: 'open-chat', targetId: recentChat.id })
              }
            >
              打开聊天
            </button>
          </section>
        )}
      </div>
    </section>
  );
}
