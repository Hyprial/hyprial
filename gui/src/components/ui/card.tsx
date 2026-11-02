// Adapted from shadcn/ui new-york-v4 (MIT); provenance: gui/docs/shadcn.md.
import type { ComponentProps } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cn } from '@/lib/utils';

/** Slot keeps the public Card's section/article/aside semantics without a wrapper. */
function Card({ className, asChild = false, ...props }: ComponentProps<'div'> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'div';
  return (
    <Comp
      {...props}
      data-slot="card"
      className={cn(
        'tw:min-w-0 tw:rounded-md tw:border tw:border-solid tw:border-border tw:bg-card tw:p-4 tw:text-card-foreground tw:shadow-(--ui-shadow-card)',
        className,
      )}
    />
  );
}

export { Card };
