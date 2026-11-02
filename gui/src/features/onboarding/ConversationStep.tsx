import {
  Button,
  Notice,
  StatusCard,
} from '../../components/primitives';
import { ConversationPreviewForm } from './ConversationPreviewForm';
import { StepCard } from './StepCard';
import { conversationPresentation } from './presentation';
import type {
  ChatStatus,
  FirstRunScenario,
  StepViewState,
} from './types';

interface ConversationStepProps {
  readonly state: StepViewState;
  readonly scenario: FirstRunScenario;
  readonly chatStatus: ChatStatus;
  readonly conversationPreview?: { readonly onConfirm: (text: string) => void };
  readonly onOpenChat?: () => void;
  readonly onComplete?: () => void;
}

export function ConversationStep({
  state,
  scenario,
  chatStatus,
  conversationPreview,
  onOpenChat,
  onComplete,
}: ConversationStepProps) {
  if (conversationPreview && state === 'active') return (
    <StepCard id="conversation" title="首次对话" state={state}>
      <p>向 Squire 描述你想先做的事，预览从首次设置进入会话的流程。</p>
      <ConversationPreviewForm onConfirm={conversationPreview.onConfirm} />
    </StepCard>
  );
  const conversation = conversationPresentation(chatStatus, scenario);

  return (
    <StepCard
      id="conversation"
      title="首次对话"
      state={state}
    >
      {state === 'locked' && (
        <p>完成 Squire 创建与启动后，才能进入首次对话引导。</p>
      )}

      {(state === 'active' || state === 'completed') && (
        <>
          <p>
            请在聊天中向 Squire 发送一条真实消息，并确认收到真实回复。本步骤只做引导与交接。
          </p>
          <div className="settings-status-list">
            <StatusCard label="首次对话" {...conversation} />
          </div>

          {chatStatus === 'unavailable' && (
            <Notice tone="warning" title="聊天能力尚未接入">
              跨 track 聊天尚未接线；本步骤保持不可用，不会模拟发送、回复或完成状态。
            </Notice>
          )}

          {chatStatus === 'ready' && (
            <>
              <div className="settings-actions">
                <Button
                  variant="primary"
                  disabled={!onOpenChat}
                  onClick={onOpenChat}
                >
                  打开聊天
                </Button>
              </div>
              {!onOpenChat && (
                <p className="settings-footnote">
                  共享聊天入口尚未提供；按钮禁用，不会发生跳转或发送。
                </p>
              )}
            </>
          )}

          {chatStatus === 'completed' && (
            <>
              <div className="settings-actions">
                <Button
                  variant="primary"
                  disabled={!onComplete}
                  onClick={onComplete}
                >
                  进入开始页
                </Button>
              </div>
              {!onComplete && (
                <p className="settings-footnote">
                  开始页路由尚未提供；完成状态保留在本组件外，不能由点击自行伪造。
                </p>
              )}
            </>
          )}

          <p className="settings-footnote">
            完成状态需由真实聊天收发确认。
          </p>
        </>
      )}
    </StepCard>
  );
}
