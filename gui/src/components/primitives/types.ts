/** Presentation-only tones. These are not protocol values. */
export type UiTone = 'neutral' | 'info' | 'positive' | 'warning' | 'danger';

/** Rendering state supplied by a page/controller; never a second business contract. */
export type AsyncView<T> =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly data: T }
  | { readonly status: 'empty'; readonly title?: string; readonly description?: string }
  | { readonly status: 'unsupported'; readonly reason: string; readonly code?: string }
  | {
      readonly status: 'error';
      readonly message: string;
      readonly retryable: boolean;
      readonly code?: string;
    };

export interface UiAction {
  readonly label: string;
  readonly onClick: () => void;
  readonly disabled?: boolean;
}

/** The single status-to-tone mapping for new components and pages. */
export function statusToTone(status: AsyncView<unknown>['status']): UiTone {
  switch (status) {
    case 'ready':
      return 'positive';
    case 'loading':
      return 'info';
    case 'unsupported':
      return 'warning';
    case 'error':
      return 'danger';
    case 'empty':
      return 'neutral';
  }
}

/** Join conditional class names without pulling in a dependency. */
export function cx(
  ...parts: ReadonlyArray<string | false | null | undefined>
): string {
  return parts.filter((part): part is string => Boolean(part)).join(' ');
}
