/*
 * Decision record (dev tools seam, PR 4a of the single GUI launch path plan,
 * 2026-11-14):
 * - The floating tool layer receives everything it knows about the running
 *   composition through this context. It never imports scenario, session or
 *   simulated-data modules, so the tool chunk can later ship next to the real
 *   backend composition without carrying simulated data.
 * - The context is a discriminated union: only the simulated composition can
 *   carry tool contributions. A backend context has no field for them, so a
 *   simulated-only tool cannot be mounted over the real backend by mistake.
 * - Over the real backend, simulated-only tools are not rendered at all:
 *   they are inapplicable developer tools with nothing to operate, so the
 *   layer shows no explanatory text for them (owner directive 2026-11-14).
 * - `scope` wraps the whole layer for as long as it is mounted. The panel
 *   body unmounts while the panel is closed, so contributions that keep
 *   state across open/close (the scenario tab added by #2727) hold it there.
 * - #2727 removed the session tools and the module index from the simulated
 *   panel; it now shows the notice and the scenario browser only. The module
 *   index stays a tool of this layer and is rendered over the backend.
 * - Capabilities the host reports (apiDocs) arrive as plain fields of the
 *   backend context. A tool whose capability is false is not rendered and
 *   leaves no explanatory text; the layer has no capability of its own.
 * - Runtime strings and property names here avoid the simulated-data
 *   vocabulary of the artifact gate on purpose.
 */
import type { ComponentType, ReactNode } from 'react';

export interface SimulatedToolContributions {
  readonly scope: ComponentType<{ readonly children: ReactNode }>;
  readonly notice: ComponentType;
  readonly scenarios: ComponentType;
}

export interface SimulatedDevToolsContext {
  readonly dataSource: 'simulated';
  readonly contributions: SimulatedToolContributions;
}

export interface BackendDevToolsContext {
  readonly dataSource: 'backend';
  readonly apiDocs: boolean;
}

export type DevToolsContext = SimulatedDevToolsContext | BackendDevToolsContext;
