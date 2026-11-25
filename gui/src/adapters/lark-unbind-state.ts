export type LarkUnbindReconciliation = 'success' | 'unchanged' | 'changed';

/** Classify one authoritative pin read after an unconfirmed conditional write. */
export function reconcileLarkUnbind(
  expectedActor: string,
  currentActor: string | null,
): LarkUnbindReconciliation {
  if (currentActor === null) return 'success';
  return currentActor === expectedActor ? 'unchanged' : 'changed';
}
