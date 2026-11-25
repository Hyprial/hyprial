import type { ComponentType } from 'react';
import {
  ArrowClockwise,
  ArrowLeft,
  ArrowRight,
  Bell,
  CaretDown,
  CaretRight,
  CaretUp,
  ChartBar,
  ChatCircle,
  Check,
  CheckCircle,
  CheckSquare,
  Copy,
  Desktop,
  DotsThree,
  EnvelopeSimple,
  Eye,
  EyeSlash,
  Folder,
  Gear,
  Graph,
  HardDrives,
  House,
  Info,
  Link,
  MagnifyingGlass,
  PaperPlaneTilt,
  PencilSimple,
  PlugsConnected,
  Plus,
  Question,
  Robot,
  SignOut,
  Trash,
  TreeStructure,
  User,
  Users,
  Warning,
  WarningCircle,
  X,
  type IconProps as PhosphorIconProps,
} from '@phosphor-icons/react';
import { cx } from './types';

const ICONS = {
  house: House,
  'chat-circle': ChatCircle,
  'check-square': CheckSquare,
  robot: Robot,
  'tree-structure': TreeStructure,
  desktop: Desktop,
  gear: Gear,
  'plugs-connected': PlugsConnected,
  link: Link,
  question: Question,
  'sign-out': SignOut,
  bell: Bell,
  'magnifying-glass': MagnifyingGlass,
  x: X,
  'caret-down': CaretDown,
  'caret-up': CaretUp,
  'caret-right': CaretRight,
  check: Check,
  plus: Plus,
  'arrow-clockwise': ArrowClockwise,
  warning: Warning,
  'warning-circle': WarningCircle,
  info: Info,
  'check-circle': CheckCircle,
  trash: Trash,
  'pencil-simple': PencilSimple,
  eye: Eye,
  'eye-slash': EyeSlash,
  copy: Copy,
  'arrow-left': ArrowLeft,
  'arrow-right': ArrowRight,
  'dots-three': DotsThree,
  'paper-plane-tilt': PaperPlaneTilt,
  users: Users,
  'envelope-simple': EnvelopeSimple,
  'hard-drives': HardDrives,
  graph: Graph,
  'chart-bar': ChartBar,
  folder: Folder,
  user: User,
} as const satisfies Record<string, ComponentType<PhosphorIconProps>>;

export type IconName = keyof typeof ICONS;
export type IconSize = 'sm' | 'md' | 'lg';

export interface IconProps {
  readonly name: IconName;
  readonly size?: IconSize;
  readonly active?: boolean;
  readonly label?: string;
  readonly className?: string;
}

/** Phosphor 图标：默认 bold，active 用 fill；无 label 时对读屏隐藏。 */
export function Icon({ name, size = 'md', active = false, label, className }: IconProps) {
  const Glyph = ICONS[name];
  return (
    <Glyph
      className={cx('ui-icon', `ui-icon--${size}`, className)}
      weight={active ? 'fill' : 'bold'}
      {...(label
        ? { role: 'img', 'aria-label': label }
        : { 'aria-hidden': true })}
    />
  );
}
