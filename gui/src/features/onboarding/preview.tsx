import { ConversationHandoffPreview } from './ConversationHandoffPreview';
import { createWebGuiCapabilities } from '../../app/capabilities';
import { StrictMode, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../../styles/tokens.css';
import '../../styles/app.css';
import '../../styles/workspace-pages.css';
import { Notice } from '../../components/primitives';
import { FirstRunOnboarding, type ChatStatus } from '.';

const PREVIEW_CHAT_STATUS: ChatStatus = 'ready';

/**
 * Private, non-product preview for the dev01 onboarding component.
 *
 * The demo action set is local-only. It must never be wired into AppShell or
 * used as evidence of real login, model, Squire, or chat connectivity.
 */
function Preview() {
  const liveAuth = new URLSearchParams(window.location.search).get('auth') === 'local';
  const actions = useMemo(() => liveAuth ? createWebGuiCapabilities().firstRun : undefined, [liveAuth]);
  const [conversationText, setConversationText] = useState<string | null>(null);
  const [chatOpened, setChatOpened] = useState(false);
  const [completionCalled, setCompletionCalled] = useState(false);
  const [exitCalled, setExitCalled] = useState(false);

  return (
    <div className="app" style={{ background: 'var(--paper)' }}>
      <main className="main">
        <div className="content">
          <Notice tone="info" title="私有预览（非产品入口）">
            {liveAuth
              ? '本页使用当前本机实例的账号与设备入口；启用本机初始化后可保存 DeepSeek Key 并请求启动 Pi/Squire。模型可用需真实会话回复确认，首次对话可模拟进入会话页面，但不发送消息。'
              : '本页只使用隔离预览回调，不访问网络、不写存储、不创建真实账号或 Squire。'}
          </Notice>
          {conversationText !== null ? <ConversationHandoffPreview text={conversationText} /> : <section className="page page-first-run" data-page="first-run">
            <FirstRunOnboarding
              scenario={liveAuth ? 'live' : 'demo'}
              actions={actions}
              conversationPreview={{ onConfirm: setConversationText }}
              chatStatus={liveAuth ? 'unavailable' : PREVIEW_CHAT_STATUS}
              onOpenChat={() => setChatOpened(true)}
              onComplete={() => setCompletionCalled(true)}
              onExit={() => setExitCalled(true)}
            />
          </section>}
          {chatOpened && (
            <Notice tone="info" title="预览回调">
              已调用 onOpenChat；没有发送消息，也没有打开真实聊天。
            </Notice>
          )}
          {completionCalled && (
            <Notice tone="info" title="预览回调">
              已调用 onComplete；真实集成应由聊天完成事件触发，而不是按钮自身。
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
