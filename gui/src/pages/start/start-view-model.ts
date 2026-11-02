import type { AsyncView } from '../../components/primitives';

/**
 * Presentation data for the real live Start page. Every field is derived by
 * the LiveApp container; the display never reads an operation port, fixture
 * store, or live hook directly.
 */
export interface StartMetricViewModel {
  readonly id: 'nodes' | 'agents' | 'connectors';
  readonly label: string;
  /**
   * `null` means the source could not provide the value. It is rendered as
   * unavailable and never coerced to zero.
   */
  readonly value: number | null;
}

export interface StartNodeViewModel {
  readonly nodeId: string;
  readonly status: string;
}

export interface StartInstanceViewModel {
  readonly eyebrow: string;
  readonly title: string;
  readonly description: string;
  readonly metadata: string;
}

// The live board has no typed machine interface at this baseline. Keeping this
// as an explicit unsupported state prevents demo task counts or fake progress.
export interface StartBoardViewModel {
  readonly status: 'unsupported';
  readonly reason: string;
  readonly code: string;
}

export interface StartPageDataViewModel {
  readonly metrics: readonly [
    StartMetricViewModel,
    StartMetricViewModel,
    StartMetricViewModel,
  ];
  readonly instance: StartInstanceViewModel;
  /** Ready and empty are real directory states; loading/error/unsupported are
   * rendered explicitly if the data source reports them. */
  readonly nodes: AsyncView<readonly StartNodeViewModel[]>;
  readonly board: StartBoardViewModel;
}

export type StartPageViewModel = AsyncView<StartPageDataViewModel>;

export interface StartPageActions {
  readonly openChat: () => void;
  readonly openAgents: () => void;
  readonly retry: () => void;
}
