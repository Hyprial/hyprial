import {
  Button,
  Notice,
  StatusCard,
} from '../../../components/primitives';
import { FirstMessageComposer } from './FirstMessageComposer';
import { StepCard } from './StepCard';
import { conversationPresentation } from './presentation';
import type {
  ChatStatus,
  FirstRunAttemptView,
  FirstRunConversationRuntime,
  ReplyMessageView,
  StepViewState,
} from './types';

export interface ReplyConfirmationView {
  readonly attempt: FirstRunAttemptView | null;
  readonly agentUri: string | null;
  readonly busy: boolean;
  readonly notice: string | null;
  readonly messages?: readonly ReplyMessageView[];
  readonly onStart: () => void;
  readonly onRefresh?: () => void;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}

interface ConversationStepProps {
  readonly state: StepViewState;
  readonly chatStatus: ChatStatus;
  readonly conversation?: FirstRunConversationRuntime;
  readonly reply?: ReplyConfirmationView;
  readonly onOpenChat?: () => void;
  readonly onComplete?: () => void;
}

export function ConversationStep({
  state,
  chatStatus,
  conversation,
  reply,
  onOpenChat,
  onComplete,
}: ConversationStepProps) {
  if (reply) return (
    <StepCard id="conversation" title="真实回复" state={state}>
      {reply.attempt?.state === 'confirmed' ? (
        <>
          <p>已收到并确认这条确认消息的真实回复，首次设置完成。</p>
          <div className="settings-status-list">
            <StatusCard label="真实回复确认" value="已确认" tone="positive"
              hint="确认来自服务端持久记录，不是本页面的发送成功回执。" />
          </div>
          <ReplyMessageLog messages={reply.messages} />
          {onComplete && (
            <div className="settings-actions">
              <Button variant="primary" onClick={onComplete}>进入开始页</Button>
            </div>
          )}
        </>
      ) : (
        <>
          <p>发送一条确认消息，并等待该消息对应的真实回复；只有你确认收到回复后，首次设置才完成。</p>
          <ReplyMessageLog messages={reply.messages} />
          {!reply.agentUri && (
            <Notice tone="warning" title="没有可确认的 Agent">
              请先完成运行配置；当前不会发送消息，也不会记为完成。
            </Notice>
          )}
          {reply.notice && (
            <Notice tone={reply.attempt?.state === 'bound' ? 'info' : 'warning'} title="真实回复确认">
              {reply.notice}
            </Notice>
          )}
          <div className="settings-actions">
            {reply.agentUri && (reply.attempt === null
              || reply.attempt.state === 'pending'
              || reply.attempt.state === 'cancelled'
              || reply.attempt.state === 'superseded') && (
              <Button variant="primary" disabled={reply.busy} onClick={reply.onStart}>
                {reply.attempt === null ? '发送确认消息'
                  : reply.attempt.state === 'pending' ? '检查发送结果'
                  : '重新开始确认'}
              </Button>
            )}
            {reply.attempt?.state === 'bound' && (
              <>
                {reply.onRefresh && (
                  <Button variant="secondary" disabled={reply.busy} onClick={reply.onRefresh}>
                    刷新回复
                  </Button>
                )}
                <Button variant="primary" disabled={reply.busy} onClick={reply.onConfirm}>
                  我已收到真实回复，确认完成
                </Button>
              </>
            )}
            {reply.busy && <span role="status">正在处理…</span>}
          </div>
          {(reply.attempt?.state === 'pending' || reply.attempt?.state === 'bound') && (
            <div className="settings-actions">
              <Button variant="text" disabled={reply.busy} onClick={reply.onCancel}>
                取消本次确认
              </Button>
            </div>
          )}
          {onOpenChat && (
            <div className="settings-actions">
              <Button variant="text" onClick={onOpenChat}>打开聊天查看回复</Button>
            </div>
          )}
        </>
      )}
    </StepCard>
  );
  if (conversation && state === 'active') return (
    <StepCard id="conversation" title="首次对话" state={state}>
      <p>向 {conversation.targetName} 发送第一条消息。</p>
      {conversation.sourceLabel && (
        <Notice tone="info" title="隔离数据来源">{conversation.sourceLabel}</Notice>
      )}
      <FirstMessagePanel conversation={conversation} />
    </StepCard>
  );
  const conversationPresentationModel = chatStatus === 'unavailable'
    ? null : conversationPresentation(chatStatus);

  return (
    <StepCard
      id="conversation"
      title="首次对话"
      state={state}
    >
      {(state === 'active' || state === 'completed') && (
        conversationPresentationModel && (
          <>
            <p>向个人助理发送一条消息。</p>
            <div className="settings-status-list">
              <StatusCard label="首次对话" {...conversationPresentationModel} />
            </div>

            {chatStatus === 'ready' && onOpenChat && (
              <div className="settings-actions">
                <Button variant="primary" onClick={onOpenChat}>
                  打开聊天
                </Button>
              </div>
            )}

            {chatStatus === 'completed' && onComplete && (
              <div className="settings-actions">
                <Button variant="primary" onClick={onComplete}>
                  进入开始页
                </Button>
              </div>
            )}
          </>
        )
      )}
    </StepCard>
  );
}

const AUTHOR_LABELS: Record<string, string> = {
  user: '我',
  system: '系统',
};

function ReplyMessageLog({ messages }: {
  readonly messages?: readonly ReplyMessageView[];
}) {
  if (!messages || messages.length === 0) return null;
  return (
    <div
      className="dev01-first-message__log"
      role="log"
      aria-label="确认真实回复的消息"
      data-reply-message-log="true"
    >
      {messages.map((message) => (
        <p
          key={message.id}
          className="dev01-first-message__item"
          data-message-id={message.id}
          data-author={message.author}
        >
          <strong>{message.author === 'agent' ? 'Agent' : AUTHOR_LABELS[message.author] ?? message.author}：</strong>
          {message.text}
        </p>
      ))}
    </div>
  );
}

function FirstMessagePanel({ conversation }: {
  readonly conversation: FirstRunConversationRuntime;
}) {
  const { status, error, targetId, messages, history, operation } = conversation;
  return (
    <div className="dev01-first-message" data-first-message="true">
      {status === 'loading' && <p role="status">正在连接聊天服务…</p>}
      {status === 'error' && (
        <Notice tone="warning" title="聊天服务未连接">
          {error ?? '无法确认聊天服务状态，暂不能发送。'}
        </Notice>
      )}
      {status === 'ready' && !targetId && (
        <Notice tone="warning" title="没有可用的聊天对象">
          没有可发送的对象，暂时不能发送第一条消息。
        </Notice>
      )}
      {status === 'ready' && !!targetId && (
        <>
          <div
            className="dev01-first-message__log"
            role="log"
            aria-label={`与 ${conversation.targetName} 的消息`}
            data-first-message-log="true"
          >
            {messages.length === 0 && history.status === 'loading' && (
              <p className="dev01-first-message__item" role="status">正在读取聊天记录…</p>
            )}
            {messages.length === 0 && history.status === 'error' && (
              <p className="dev01-first-message__item" role="note">聊天记录读取失败：{history.error}</p>
            )}
            {messages.map((message) => (
              <p
                key={message.id}
                className="dev01-first-message__item"
                data-message-id={message.id}
                data-author={message.author}
              >
                <strong>{message.author === 'assistant' ? conversation.targetName : AUTHOR_LABELS[message.author] ?? message.author}：</strong>
                {message.text}
              </p>
            ))}
          </div>
          <FirstMessageComposer conversation={conversation} />
          {operation?.status === 'unknown' && (
            <div className="settings-actions">
              <Button onClick={conversation.onCheckResult}>查询发送结果</Button>
            </div>
          )}
          {operation?.status === 'definitive-failure' && !operation.canRetry && (
            <div className="settings-actions">
              <Button onClick={conversation.onRetry}>修改后重新发送</Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
