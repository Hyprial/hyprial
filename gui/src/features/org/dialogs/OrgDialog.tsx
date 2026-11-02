import type { ReactNode, RefObject } from 'react';
import { Dialog, type DialogAction } from '../../../components/primitives';

/**
 * L3 容器。复用基础 Dialog 的焦点陷阱与焦点归还；窄屏改为全屏（第 12 节）。
 * 多步操作在同一个容器内切换步骤，不叠加第二个对话框。
 */
export function OrgDialog({
  title,
  description,
  onClose,
  actions,
  initialFocusRef,
  children,
  wide,
  devUi,
}: {
  readonly title: string;
  readonly description?: string;
  readonly onClose: () => void;
  readonly actions?: ReadonlyArray<DialogAction>;
  readonly initialFocusRef?: RefObject<HTMLElement | null>;
  readonly children?: ReactNode;
  readonly wide?: boolean;
  readonly devUi?: string;
}) {
  return (
    <Dialog
      open
      title={title}
      description={description}
      onClose={onClose}
      actions={actions}
      initialFocusRef={initialFocusRef}
      className={`org-dialog${wide ? ' org-dialog--wide' : ''}`}
    >
      <div className="org-dialog__content" data-dev-ui={devUi}>
        {children}
      </div>
    </Dialog>
  );
}
