/**
 * Decision record (PR 1f/F0b, 2026-11-14):
- The shell hides a destination whose source or port is absent (hiddenPages).
  Navigation that starts inside a page does not pass through the rail, so an
  entry point elsewhere would land on the start page silently.
- This module is the one shared fact for such entry points: the shell
  provides the hidden set, and a control that navigates to another
  destination renders only when `usePageAvailable(page)` is true.
- Without a provider every page is available, so a component rendered alone
  in a test keeps its controls.
- The value comes from capabilities and data only, never from build mode.
 */
import { createContext, useContext, type ReactNode } from 'react';
import type { GuiPage } from '../state/gui-view-model';

const EMPTY: ReadonlySet<GuiPage> = new Set<GuiPage>();

const HiddenPagesContext = createContext<ReadonlySet<GuiPage>>(EMPTY);

export function PageAvailabilityProvider({
  hiddenPages,
  children,
}: {
  readonly hiddenPages: ReadonlySet<GuiPage>;
  readonly children: ReactNode;
}) {
  return <HiddenPagesContext.Provider value={hiddenPages}>{children}</HiddenPagesContext.Provider>;
}

export function usePageAvailable(page: GuiPage): boolean {
  return !useContext(HiddenPagesContext).has(page);
}
