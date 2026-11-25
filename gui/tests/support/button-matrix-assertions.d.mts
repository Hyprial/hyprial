export interface ProductionPrimaryButton {
  readonly tag?: string;
  readonly slot?: string | null;
  readonly className: string;
  readonly radius: string;
  readonly background: string;
  readonly color: string;
  readonly border: string;
}

export function assertProductionPrimary(buttons: readonly ProductionPrimaryButton[], width: number): void;
