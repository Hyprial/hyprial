import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

const merge = extendTailwindMerge({ prefix: 'tw' });

/** Merge only our prefixed utilities; keep the project's semantic class hooks. */
export function cn(...inputs: ClassValue[]) {
  return merge(clsx(inputs));
}
