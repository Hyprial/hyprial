export function NoticeBanner({
  text,
  onDismiss,
}: {
  readonly text: string;
  readonly onDismiss: () => void;
}) {
  return (
    <div
      className="notice"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <span>{text}</span>
      <button
        type="button"
        className="notice-close"
        aria-label="关闭提示"
        onClick={onDismiss}
      >
        ×
      </button>
    </div>
  );
}
