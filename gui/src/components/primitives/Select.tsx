import type { Ref } from 'react';
import {
  Select as SelectRoot,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../ui/select';
import { cx } from './types';

export interface SelectOption {
  readonly value: string;
  readonly label: string;
}

export interface SelectProps {
  /** Labelled by the page's <label htmlFor>; the id lands on the trigger. */
  readonly id?: string;
  /**
   * Controlled value. The empty string selects the option whose value is ''
   * (e.g. 不加入组织 / 未指定); Radix forbids '' item values so the adapter
   * translates it through an internal sentinel.
   */
  readonly value: string;
  readonly options: readonly SelectOption[];
  readonly onValueChange: (value: string) => void;
  readonly disabled?: boolean;
  readonly className?: string;
  readonly ref?: Ref<HTMLButtonElement>;
}

const EMPTY_VALUE = '__hyprial-empty__';

/**
 * Project-facing single-choice select over the shadcn/Radix base component.
 *
 * Options are a flat value/label list so existing form code keeps its
 * "empty option with a label" convention. Keyboard, focus and Esc behavior
 * come from Radix; the trigger keeps the ui-select layout hook and defaults
 * to full width like the native selects it replaces.
 */
export function Select({
  id,
  value,
  options,
  onValueChange,
  disabled = false,
  className,
  ref,
}: SelectProps) {
  return (
    <SelectRoot
      value={value === '' ? EMPTY_VALUE : value}
      onValueChange={(next) => onValueChange(next === EMPTY_VALUE ? '' : next)}
      disabled={disabled}
    >
      <SelectTrigger
        id={id}
        ref={ref}
        className={cx('ui-select tw:w-full', className)}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem
            key={option.value}
            value={option.value === '' ? EMPTY_VALUE : option.value}
          >
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </SelectRoot>
  );
}
