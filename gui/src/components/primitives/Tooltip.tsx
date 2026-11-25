import {
  cloneElement,
  useEffect,
  useId,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactElement,
  type ReactNode,
} from 'react';
import { Icon, type IconName } from './Icon';
import { cx } from './types';

function useEscapeToClose(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open || typeof document === 'undefined') return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, close]);
}

export interface TooltipProps {
  readonly content: ReactNode;
  readonly children: ReactElement<{ 'aria-describedby'?: string }>;
  readonly className?: string;
}

/**
 * 最小可访问提示：hover/focus 显示，Escape/blur/mouseleave 隐藏。
 * 内容常驻 DOM，以 hidden 切换，读屏可经 aria-describedby 到达。
 */
export function Tooltip({ content, children, className }: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);
  useEscapeToClose(open, close);
  const existing = children.props['aria-describedby'];

  return (
    <span
      className={cx('ui-tooltip', className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={close}
      onFocus={() => setOpen(true)}
      onBlur={close}
    >
      {cloneElement(children, { 'aria-describedby': existing ? `${existing} ${id}` : id })}
      <span id={id} role="tooltip" className="ui-tooltip__content" hidden={!open}>
        {content}
      </span>
    </span>
  );
}

export interface InfoTipProps {
  readonly content: ReactNode;
  readonly label?: string;
  readonly className?: string;
  readonly icon?: IconName;
}

/** 承接可选补充解释的信息按钮；鼠标 hover/键盘 focus 显示，触屏点击切换。 */
export function InfoTip({ content, label = '说明', className, icon = 'info' }: InfoTipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const pointerType = useRef('');
  const close = () => setOpen(false);
  useEscapeToClose(open, close);

  const onPointerDown = (event: ReactPointerEvent) => {
    pointerType.current = event.pointerType;
  };

  return (
    <span
      className={cx('ui-tooltip ui-infotip', className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={close}
    >
      <button
        type="button"
        className="ui-infotip__button"
        aria-label={label}
        aria-describedby={id}
        aria-expanded={open}
        onPointerDown={onPointerDown}
        onFocus={() => {
          if (pointerType.current !== 'touch') setOpen(true);
        }}
        onBlur={close}
        onClick={() => {
          if (pointerType.current === 'mouse') setOpen(true);
          else setOpen((value) => !value);
          pointerType.current = '';
        }}
      >
        <Icon name={icon} size="sm" />
      </button>
      <span id={id} role="tooltip" className="ui-tooltip__content" hidden={!open}>
        {content}
      </span>
    </span>
  );
}
