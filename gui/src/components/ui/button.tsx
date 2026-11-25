// Adapted from shadcn/ui new-york-v4 (MIT); provenance: gui/docs/shadcn.md.
import type { ComponentProps } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'tw:inline-flex tw:min-w-0 tw:items-center tw:justify-center tw:gap-2 tw:border-0 tw:rounded-[var(--ui-radius-pill)] tw:font-sans tw:font-medium tw:leading-[var(--ui-leading-tight)] tw:text-center tw:cursor-pointer tw:transition-[transform,background-color,color] tw:duration-[var(--ui-motion-fast)] tw:ease-[var(--ui-ease-standard)] tw:active:scale-[0.98] tw:focus-visible:outline-[length:var(--ui-focus-width)] tw:focus-visible:outline-solid tw:focus-visible:outline-[var(--ui-focus)] tw:focus-visible:outline-offset-2 tw:disabled:cursor-not-allowed tw:disabled:opacity-[0.42] tw:disabled:active:scale-100 tw:motion-reduce:transition-none tw:motion-reduce:active:scale-100',
  {
    variants: {
      variant: {
        default:
          'tw:bg-[var(--ui-accent)] tw:text-[var(--ui-on-accent)] tw:enabled:hover:bg-[var(--ui-accent-hover)]',
        destructive:
          'tw:bg-[var(--ui-danger-wash)] tw:text-[var(--ui-danger-ink)] tw:enabled:hover:bg-[var(--ui-danger-wash-hover)]',
        outline:
          'tw:bg-[var(--ui-neutral-fill)] tw:text-[var(--ui-ink-1)] tw:enabled:hover:bg-[var(--ui-neutral-fill-hover)]',
        secondary:
          'tw:bg-[var(--ui-neutral-fill)] tw:text-[var(--ui-ink-1)] tw:enabled:hover:bg-[var(--ui-neutral-fill-hover)]',
        ghost:
          'tw:bg-transparent tw:text-[var(--ui-ink-2)] tw:enabled:hover:bg-[var(--ui-neutral-fill)] tw:enabled:hover:text-[var(--ui-ink-1)]',
        link:
          'tw:bg-transparent tw:text-[var(--ui-ink-2)] tw:enabled:hover:text-[var(--ui-ink-1)] tw:enabled:hover:underline tw:underline-offset-[3px]',
      },
      size: {
        default: 'tw:min-h-[var(--ui-control-height-md)] tw:px-6 tw:py-0 tw:text-base',
        sm: 'tw:min-h-[var(--ui-control-height-sm)] tw:px-4.5 tw:py-0 tw:text-sm',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
);

function Button({
  className,
  variant = 'default',
  size = 'default',
  asChild = false,
  type = 'button',
  ...props
}: ComponentProps<'button'> & VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'button';
  return (
    <Comp
      {...props}
      type={asChild ? undefined : type}
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size }), 'tw:leading-[var(--ui-leading-tight)]', className)}
    />
  );
}

export { Button, buttonVariants };
