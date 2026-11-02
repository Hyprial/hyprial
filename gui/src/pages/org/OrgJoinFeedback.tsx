import type { OrgJoinState } from './org-view-model';

export function OrgJoinFeedback({
  message,
  onRetry,
}: {
  readonly message: string | null;
  readonly onRetry?: () => void;
}) {
  if (!message) {
    return null;
  }
  return (
    <div className="org-join-failed">
      <p className="org-join-error" role="alert">
        {message}
      </p>
      <button type="button" className="button button-secondary" onClick={onRetry}>
        重试加入
      </button>
    </div>
  );
}

/** Message shown for a failed join: the local attempt wins over scene data. */
export function joinFeedbackMessage(
  join: OrgJoinState,
  localError: string | null,
): string | null {
  if (localError) {
    return localError;
  }
  return join.state === 'failed' ? (join.message ?? null) : null;
}
