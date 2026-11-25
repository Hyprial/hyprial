import { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { StrictMode } from 'react';
import '../../styles/tokens.css';
import '../../styles/app.css';
import '../../styles/workspace-pages.css';
import { Notice } from '../../components/primitives';
import { FirstRunOnboarding, useFirstRunConversation } from '../../features/onboarding';
import { createRuntimeDemoActions } from '../mock/startup-agents';
import { ChatController } from '../../features/agent-chat';
import { createAgentChatMock, type AgentChatScenario } from '../mock/agent-chat';

const CHAT_SCENARIOS: readonly AgentChatScenario[] = [
  'success',
  'failure',
  'unknown',
  'empty',
  'error',
  'unconfigured',
];

function isChatScenario(value: string | null): value is AgentChatScenario {
  return value !== null && (CHAT_SCENARIOS as readonly string[]).includes(value);
}

/**
 * Private, non-product preview for the dev01 onboarding component.
 *
 * The chat runtime is the existing explicit contract mock; it is selected with
 * `?chat=<scenario>` and never connects to a real service. The former
 * `?auth=local` mode is removed: the shared web capability set no longer
 * carries first-run actions, so it had no ports left to exercise. The demo action set
 * is local-only and must never be wired into AppShell or used as evidence of
 * real login, model, Agent, or chat connectivity.
 */
function Preview() {
  const requested = new URLSearchParams(window.location.search).get('chat');
  const previewActions = useMemo(() => createRuntimeDemoActions(), []);
  const previewSource = useMemo(() => ({
    notice: {
      title: '隔离预览场景',
      body: '所有成功状态仅由本地隔离回调返回；不会登录真实账号、访问网络、配置真实 Agent 或发送聊天消息。',
    },
    label: '隔离预览 · 不访问网络、不写存储',
    savedKeyNote: '（隔离预览）',
  }), []);
  const [chat] = useState(() => isChatScenario(requested)
    ? new ChatController(createAgentChatMock(requested))
    : null);
  useEffect(() => { void chat?.start(); }, [chat]);
  const conversation = useFirstRunConversation(chat, chat ? 'FIXTURE · 隔离契约模拟 · 不连接真实服务' : undefined);
  const [completionCalled, setCompletionCalled] = useState(false);
  const [exitCalled, setExitCalled] = useState(false);

  return (
    <div className="app" style={{ background: 'var(--ui-ground)' }}>
      <main className="main">
        <div className="content">
          <Notice tone="info" title="私有预览（非产品入口）">
            本页只使用隔离预览回调与显式选择的契约模拟，不访问网络、不写存储、不创建真实账号或 Agent。
          </Notice>
          {chat && (
            <p className="settings-footnote" data-chat-scenario={requested}>
              契约场景：{requested}。可用值：{CHAT_SCENARIOS.join(' / ')}。
            </p>
          )}
          <section className="page page-first-run" data-page="first-run">
            <FirstRunOnboarding
              scenario="isolated"
              actions={previewActions}
              source={previewSource}
              conversation={conversation ?? undefined}
              onComplete={() => setCompletionCalled(true)}
              onExit={() => setExitCalled(true)}
            />
          </section>
          {conversation?.sendAccepted && (
            <Notice tone="info" title="发送已被服务接收">
              真实集成在此刻自动进入开始页；本预览页保留在原处。
            </Notice>
          )}
          {completionCalled && (
            <Notice tone="info" title="预览回调">
              已调用 onComplete；真实集成由发送接收确认触发，而不是按钮自身。
            </Notice>
          )}
          {exitCalled && (
            <Notice tone="info" title="预览回调">
              已调用 onExit；产品中由共享路由返回设置。
            </Notice>
          )}
        </div>
      </main>
    </div>
  );
}

const root = document.getElementById('root');
if (!root) {
  throw new Error('Missing preview root');
}

createRoot(root).render(
  <StrictMode>
    <Preview />
  </StrictMode>,
);
