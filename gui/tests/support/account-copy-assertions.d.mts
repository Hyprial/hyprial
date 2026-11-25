export interface AccountCopySample {
  readonly display: string;
  readonly height: number;
  readonly text: string | null;
  readonly small: number;
  readonly triggerWidth: number;
  readonly triggerHeight: number;
  readonly triggerLabel: string | null;
  readonly menuItems: readonly string[];
}
export const OWNER_COPY: string;
export const OWNER_NARROW_BREAKPOINT: number;
export function assertAccountCopy(sample: AccountCopySample, width: number): void;
