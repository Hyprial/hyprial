// Adapted from shadcn/ui new-york-v4 (MIT); provenance: gui/docs/shadcn.md.
// Local adaptations vs the registry source: @radix-ui/react-select subpackage
// instead of the radix-ui umbrella; local cn; tw: utility prefix; inline SVG
// icons instead of lucide-react; control-height tokens; light theme only; no
// animation utilities (tw-animate-css is not a dependency); pruned to the
// parts this project consumes (Root/Trigger/Value/Content/Item/scroll
// buttons, item-aligned position only).
import * as React from 'react';
import * as SelectPrimitive from '@radix-ui/react-select';
import { cn } from '@/lib/utils';

function Select({
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Root>) {
  return <SelectPrimitive.Root data-slot="select" {...props} />
}

function SelectValue({
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Value>) {
  return <SelectPrimitive.Value data-slot="select-value" {...props} />
}

function SelectTrigger({
  className,
  size = "default",
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Trigger> & {
  size?: "sm" | "default"
}) {
  return (
    <SelectPrimitive.Trigger
      data-slot="select-trigger"
      data-size={size}
      className={cn(
        "tw:flex tw:w-fit tw:min-w-0 tw:items-center tw:justify-between tw:gap-2 tw:rounded-sm tw:border tw:border-solid tw:border-input tw:bg-card tw:px-3 tw:py-0 tw:text-sm tw:whitespace-nowrap tw:outline-none tw:transition-[color,box-shadow] tw:duration-[var(--ui-motion-fast)] tw:ease-[var(--ui-ease-standard)] tw:focus-visible:border-ring tw:focus-visible:ring-2 tw:focus-visible:ring-ring/50 tw:disabled:cursor-not-allowed tw:disabled:opacity-55 tw:data-[placeholder]:text-muted-foreground tw:data-[size=default]:min-h-[var(--ui-control-height-md)] tw:data-[size=sm]:min-h-[var(--ui-control-height-sm)] tw:*:data-[slot=select-value]:line-clamp-1 tw:*:data-[slot=select-value]:flex tw:*:data-[slot=select-value]:min-w-0 tw:*:data-[slot=select-value]:items-center tw:*:data-[slot=select-value]:gap-2 tw:[&_svg]:pointer-events-none tw:[&_svg]:shrink-0 tw:[&_svg]:size-4 tw:[&_svg]:text-muted-foreground",
        className
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="tw:opacity-50">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  )
}

function SelectContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        data-slot="select-content"
        className={cn(
          "tw:relative tw:z-50 tw:max-h-(--radix-select-content-available-height) tw:min-w-[8rem] tw:origin-(--radix-select-content-transform-origin) tw:overflow-x-hidden tw:overflow-y-auto tw:rounded-sm tw:border tw:border-solid tw:border-border tw:bg-popover tw:text-popover-foreground tw:font-sans tw:text-sm tw:shadow-[var(--ui-shadow-card)]",
          className
        )}
        position="item-aligned"
        {...props}
      >
        <SelectScrollUpButton />
        <SelectPrimitive.Viewport className={cn("tw:p-1")}>
          {children}
        </SelectPrimitive.Viewport>
        <SelectScrollDownButton />
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  )
}

function SelectItem({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      data-slot="select-item"
      className={cn(
        "tw:relative tw:flex tw:w-full tw:cursor-default tw:items-center tw:gap-2 tw:rounded-sm tw:py-1.5 tw:pr-8 tw:pl-2 tw:text-sm tw:outline-hidden tw:select-none tw:focus:bg-accent tw:focus:text-accent-foreground tw:data-[disabled]:pointer-events-none tw:data-[disabled]:opacity-50",
        className
      )}
      {...props}
    >
      <span
        data-slot="select-item-indicator"
        className="tw:absolute tw:right-2 tw:flex tw:size-3.5 tw:items-center tw:justify-center"
      >
        <SelectPrimitive.ItemIndicator>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="tw:size-4">
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </SelectPrimitive.ItemIndicator>
      </span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  )
}

function SelectScrollUpButton({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.ScrollUpButton>) {
  return (
    <SelectPrimitive.ScrollUpButton
      data-slot="select-scroll-up-button"
      className={cn(
        "tw:flex tw:cursor-default tw:items-center tw:justify-center tw:py-1",
        className
      )}
      {...props}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="tw:size-4">
        <path d="m18 15-6-6-6 6" />
      </svg>
    </SelectPrimitive.ScrollUpButton>
  )
}

function SelectScrollDownButton({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.ScrollDownButton>) {
  return (
    <SelectPrimitive.ScrollDownButton
      data-slot="select-scroll-down-button"
      className={cn(
        "tw:flex tw:cursor-default tw:items-center tw:justify-center tw:py-1",
        className
      )}
      {...props}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="tw:size-4">
        <path d="m6 9 6 6 6-6" />
      </svg>
    </SelectPrimitive.ScrollDownButton>
  )
}

export {
  Select,
  SelectContent,
  SelectItem,
  SelectScrollDownButton,
  SelectScrollUpButton,
  SelectTrigger,
  SelectValue,
}
