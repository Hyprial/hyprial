// Adapted from shadcn/ui new-york-v4 (MIT); provenance: gui/docs/shadcn.md.
// Local adaptations vs the registry source: @radix-ui/react-select subpackage
// instead of the radix-ui umbrella; local cn; tw: utility prefix; Phosphor
// icons via the shared Icon primitive instead of lucide-react; control-height tokens; light theme only; no
// animation utilities (tw-animate-css is not a dependency); pruned to the
// parts this project consumes (Root/Trigger/Value/Content/Item/scroll
// buttons, item-aligned position only).
import * as React from 'react';
import * as SelectPrimitive from '@radix-ui/react-select';
import { cn } from '@/lib/utils';
import { Icon } from '../primitives/Icon';

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
        "tw:flex tw:w-fit tw:min-w-0 tw:items-center tw:justify-between tw:gap-2 tw:rounded-[var(--ui-radius-pill)] tw:border-0 tw:bg-[var(--ui-input-fill)] tw:px-5 tw:py-0 tw:text-sm tw:whitespace-nowrap tw:outline-none tw:transition-[color,background-color] tw:duration-[var(--ui-motion-fast)] tw:ease-[var(--ui-ease-standard)] tw:focus-visible:outline-[length:3px] tw:focus-visible:outline-solid tw:focus-visible:outline-[var(--ui-focus)] tw:focus-visible:outline-offset-2 tw:disabled:cursor-not-allowed tw:disabled:opacity-[0.42] tw:data-[placeholder]:text-muted-foreground tw:data-[size=default]:min-h-[var(--ui-control-height-md)] tw:data-[size=sm]:min-h-[var(--ui-control-height-sm)] tw:*:data-[slot=select-value]:line-clamp-1 tw:*:data-[slot=select-value]:flex tw:*:data-[slot=select-value]:min-w-0 tw:*:data-[slot=select-value]:items-center tw:*:data-[slot=select-value]:gap-2 tw:[&_svg]:pointer-events-none tw:[&_svg]:shrink-0 tw:[&_svg]:size-4 tw:[&_svg]:text-muted-foreground",
        className
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <Icon name="caret-down" size="sm" className="tw:opacity-60" />
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
          "tw:relative tw:z-(--ui-z-dialog-popover) tw:max-h-(--radix-select-content-available-height) tw:min-w-[8rem] tw:origin-(--radix-select-content-transform-origin) tw:overflow-x-hidden tw:overflow-y-auto tw:rounded-md tw:border-0 tw:bg-popover tw:text-popover-foreground tw:font-sans tw:text-sm tw:shadow-(--ui-shadow-popover)",
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
        "tw:relative tw:flex tw:w-full tw:cursor-default tw:items-center tw:gap-2 tw:rounded-sm tw:py-2 tw:pr-8 tw:pl-3 tw:text-sm tw:outline-hidden tw:select-none tw:focus:bg-[var(--ui-neutral-fill)] tw:focus:text-[var(--ui-ink-1)] tw:data-[disabled]:pointer-events-none tw:data-[disabled]:opacity-50",
        className
      )}
      {...props}
    >
      <span
        data-slot="select-item-indicator"
        className="tw:absolute tw:right-2 tw:flex tw:size-3.5 tw:items-center tw:justify-center"
      >
        <SelectPrimitive.ItemIndicator>
          <Icon name="check" size="sm" />
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
      <Icon name="caret-up" size="sm" />
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
      <Icon name="caret-down" size="sm" />
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
