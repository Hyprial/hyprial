import { createContext, useContext, useState, type ReactNode } from 'react';
import { createAccountActions, createUnavailableActions, type FirstRunActions } from '../features/onboarding';
import { createNativeAuthClient, createWebAuthClient, type AuthClient } from '../platform/auth';
import { createWebSetupClient } from '../platform/setup';
import { createWebHostTransport } from '../platform/transport';
import { createHttpAgentProviderApi, type AgentProviderApi, type ProviderLoginFile } from '../features/agent-provider/api';

/** App composition only. Pages never select a process, endpoint or credential store. */
export interface GuiCapabilities {
  readonly firstRun: FirstRunActions;
  /** Fixture-only composition gate for the account login reference section. */
  readonly accountLoginDisabled?: boolean;
  readonly agentProvider?: AgentProviderApi;
  /** Supplied only by a trusted host flow; never derived from browser input. */
  readonly agentProviderLoginFiles?: (actor: string, provider: string) => Promise<readonly ProviderLoginFile[]>;
}
export function createWebGuiCapabilities(fetcher: typeof fetch = fetch): GuiCapabilities {
  const transport = createWebHostTransport(fetcher);
  return { firstRun: createAccountActions(createWebAuthClient(fetcher, transport), createWebSetupClient(fetcher, transport)),
    agentProvider: typeof __HYPRIAL_REFERENCE_HTTP__ !== 'undefined' && __HYPRIAL_REFERENCE_HTTP__ ? createHttpAgentProviderApi('/api/reference/live', fetcher) : undefined };
}
/**
 * Native hosts inject the same narrow account capability. Setup/model/Squire
 * stay explicitly unavailable until a host capability exists for them.
 */
export function createNativeGuiCapabilities(account: AuthClient): GuiCapabilities {
  return { firstRun: createAccountActions(createNativeAuthClient(account)) };
}
/** A missing native bridge keeps the same-origin Web host as the default. */
export function createGuiCapabilities(account?: AuthClient): GuiCapabilities {
  return account ? createNativeGuiCapabilities(account) : createWebGuiCapabilities();
}
const unavailable: GuiCapabilities = { firstRun: createUnavailableActions() };
const Capabilities = createContext<GuiCapabilities>(unavailable);
export function GuiCapabilitiesProvider({ value, children }: { readonly value?: GuiCapabilities; readonly children: ReactNode }) {
  const [capabilities] = useState(() => value ?? createWebGuiCapabilities());
  return <Capabilities.Provider value={value ?? capabilities}>{children}</Capabilities.Provider>;
}
export function useGuiCapabilities(): GuiCapabilities { return useContext(Capabilities); }
