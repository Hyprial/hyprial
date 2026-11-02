// Adapted from shadcn/ui new-york-v4 (MIT); provenance: gui/docs/shadcn.md.
import type { ComponentProps } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'tw:inline-flex tw:min-w-0 tw:items-center tw:justify-center tw:gap-2 tw:border tw:border-solid tw:border-transparent tw:rounded-sm tw:font-sans tw:text-sm tw:font-semibold tw:leading-[var(--ui-leading-tight)] tw:text-center tw:cursor-pointer tw:transition-colors tw:duration-[var(--ui-motion-fast)] tw:ease-[var(--ui-ease-standard)] tw:focus-visible:outline-2 tw:focus-visible:outline-solid tw:focus-visible:outline-ring tw:focus-visible:outline-offset-2 tw:disabled:cursor-not-allowed tw:disabled:opacity-55 tw:disabled:shadow-none tw:motion-reduce:transition-none',
  {
    variants: {
      variant: {
        default: 'tw:bg-primary tw:text-primary-foreground tw:shadow-[var(--ui-shadow-sm)] tw:enabled:hover:bg-primary-hover',
        destructive: 'tw:bg-destructive tw:text-primary-foreground tw:enabled:hover:opacity-90',
        outline: 'tw:bg-card tw:border-input tw:text-primary tw:enabled:hover:bg-accent tw:enabled:hover:border-primary',
        secondary: 'tw:bg-secondary tw:text-secondary-foreground tw:enabled:hover:bg-accent',
        ghost: 'tw:bg-transparent tw:text-secondary-foreground tw:enabled:hover:bg-muted',
        link: 'tw:bg-transparent tw:text-primary tw:enabled:hover:text-primary-hover tw:enabled:hover:underline tw:underline-offset-[3px]',
      },
      size: {
        default: 'tw:min-h-[var(--ui-control-height-md)] tw:px-4 tw:py-0',
        sm: 'tw:min-h-[var(--ui-control-height-sm)] tw:px-3 tw:py-0 tw:text-xs',
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
