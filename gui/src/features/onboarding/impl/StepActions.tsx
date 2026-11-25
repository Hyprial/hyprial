import { Button } from '../../../components/primitives';

/** 单个步骤动作；没有端口的动作不构造，不渲染。 */
export interface StepAction {
  readonly label: string;
  readonly onClick?: () => void;
  readonly disabled?: boolean;
  readonly loading?: boolean;
  readonly type?: 'button' | 'submit';
}

/**
 * 步骤动作数据模型：每个步骤只声明动作，不自己写布局。
 * - single：唯一主动作，占满动作区整行
 * - pair：start 靠左、end 靠右；end 缺失（其端口不存在）时只渲染 start
 */
export type StepActionModel =
  | { readonly kind: 'single'; readonly primary: StepAction }
  | { readonly kind: 'pair'; readonly start: StepAction; readonly end?: StepAction };

interface StepActionsProps {
  readonly model: StepActionModel;
}

/**
 * 集中计算 disabled：非 submit 动作没有 handler 视为不可用（避免可点但无行为的按钮）；
 * submit 动作允许没有 onClick（由表单 onSubmit 处理），只受显式 disabled 控制。
 */
function resolveDisabled(action: StepAction): boolean {
  if (action.type === 'submit') return Boolean(action.disabled);
  return Boolean(action.disabled || !action.onClick);
}

/**
 * 共享动作区（v13）：所有步骤共用同一段布局与样式，避免逐步骤复制。
 * 稳定挂点：data-actions="single" | "pair"。
 */
export function StepActions({ model }: StepActionsProps) {
  return (
    <div className="dev01-v17-actions" data-actions={model.kind}>
      {model.kind === 'single' ? (
        <Button
          variant="primary"
          fullWidth
          type={model.primary.type ?? 'button'}
          disabled={resolveDisabled(model.primary)}
          loading={model.primary.loading}
          onClick={model.primary.onClick}
        >
          {model.primary.label}
        </Button>
      ) : (
        <>
          <Button
            variant="text"
            className="dev01-v17-actions__start"
            type={model.start.type ?? 'button'}
            disabled={resolveDisabled(model.start)}
            loading={model.start.loading}
            onClick={model.start.onClick}
          >
            {model.start.label}
          </Button>
          {model.end && (
            <Button
              variant="primary"
              className="dev01-v17-actions__end"
              type={model.end.type ?? 'button'}
              disabled={resolveDisabled(model.end)}
              loading={model.end.loading}
              onClick={model.end.onClick}
            >
              {model.end.label}
            </Button>
          )}
        </>
      )}
    </div>
  );
}
