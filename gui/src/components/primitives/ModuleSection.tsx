import { useId, type ComponentProps, type ReactNode, type Ref } from 'react';
import { Card } from './Card';

type ProtectedSectionProps = 'id' | 'aria-labelledby' | 'data-module' | 'data-busy';

export interface ModuleSectionProps {
  /** 稳定 aria 锚点；缺省由 useId 生成。 */
  readonly id?: string;
  readonly title: string;
  readonly titleAddon?: ReactNode;
  readonly description?: string;
  /** 动作区直接接收现成控件（保留其 variant/aria/loading/表单语义）。 */
  readonly actions?: ReactNode;
  readonly children?: ReactNode;
  readonly tone?: 'neutral' | 'info' | 'warning';
  readonly className?: string;
  /**
   * 额外 section 属性（role / aria-describedby 等）直落同一个 surface。
   * 组件契约字段被 omit，且 `data-*` 契约键在类型层拒绝（运行时也被后置覆盖）。
   */
  readonly sectionProps?: Omit<
    ComponentProps<'section'>,
    'children' | 'className' | 'ref' | ProtectedSectionProps
  > & { readonly 'data-module'?: never; readonly 'data-busy'?: never };
  readonly sectionRef?: Ref<HTMLElement>;
}

/**
 * Connections 一级模块的唯一 surface（nendo 模块流）。
 * surface 本身即语义 section：Card as="section" 直接承载 data-module 与 aria-labelledby，
 * 不再套一层 section；标题、说明、状态、动作与内容都在同一卡面内。
 * 只负责布局与语义，不接管业务回调与状态判断。
 */
export function ModuleSection({
  id,
  title,
  titleAddon,
  description,
  actions,
  children,
  tone = 'neutral',
  className,
  sectionProps,
  sectionRef,
}: ModuleSectionProps) {
  const generated = useId().replace(/:/g, '');
  const headingId = id ?? `ui-module-${generated}`;
  return (
    <Card
      as="section"
      {...sectionProps}
      padding="lg"
      tone={tone}
      className={['ui-module ui-module__surface', className].filter(Boolean).join(' ')}
      data-module=""
      // data-busy 属受保护契约：类型禁止外部传入，显式置空保证 DOM 不出现该属性。
      data-busy={undefined}
      aria-labelledby={headingId}
      ref={sectionRef}
      id={undefined}
    >
      <header className="ui-module__header">
        <div className="ui-module__heading">
          <div className="ui-module__title-row">
            <h2 id={headingId} className="ui-module__title">
              {title}
            </h2>
            {titleAddon}
          </div>
          {description && <p className="ui-module__description">{description}</p>}
        </div>
        {actions && <div className="ui-module__actions">{actions}</div>}
      </header>
      {children !== undefined && <div className="ui-module__body">{children}</div>}
    </Card>
  );
}
