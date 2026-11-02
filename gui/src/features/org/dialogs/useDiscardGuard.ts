import { useRef, useState, type RefObject } from 'react';

/**
 * 编辑器的退出保护。有未保存的修改时，Esc、×、“关闭”都先切到确认步骤，
 * 在同一个 L3 容器内完成，不叠加对话框（第 2 节）。确认步骤里再按 Esc 或 × 等于“继续编辑”。
 * “继续编辑”把焦点还给退出前所在的控件；编辑内容在确认期间只隐藏、不卸载，原样保留。
 */
export function useDiscardGuard({
  dirty,
  onClose,
  initialFocusRef,
}: {
  readonly dirty: boolean;
  readonly onClose: () => void;
  /** 编辑器刚打开时的焦点位置。 */
  readonly initialFocusRef: RefObject<HTMLElement | null>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [resumed, setResumed] = useState(false);
  const confirmRef = useRef<HTMLParagraphElement>(null);
  const resumeRef = useRef<HTMLElement | null>(null);

  function resume(): void {
    setConfirming(false);
    setResumed(true);
  }

  function requestClose(): void {
    if (confirming) {
      resume();
      return;
    }
    if (!dirty) {
      onClose();
      return;
    }
    const active = document.activeElement;
    resumeRef.current =
      active instanceof HTMLElement && active !== document.body ? active : initialFocusRef.current;
    setConfirming(true);
  }

  return {
    confirming,
    requestClose,
    resume,
    confirmRef,
    // 切换步骤时换一个焦点目标，Dialog 据此把焦点移到确认说明或退回原控件。
    focusRef: confirming ? confirmRef : resumed ? resumeRef : initialFocusRef,
  };
}
