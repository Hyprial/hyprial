import { Notice } from './primitives';

export function NoticeBanner({ text, onDismiss }: { readonly text: string; readonly onDismiss: () => void }) {
  return (
    <Notice tone="warning" onDismiss={onDismiss} dismissLabel="关闭提示">
      {text}
    </Notice>
  );
}
