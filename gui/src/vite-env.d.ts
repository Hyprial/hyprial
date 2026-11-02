/// <reference types="vite/client" />
import type { AuthClient } from './platform/auth';
declare global {
  const __HYPRIAL_DEV_MODULE_LABELS__: boolean;
  const __HYPRIAL_DEV_PREVIEW__: boolean;
  const __HYPRIAL_BACKEND_ENDPOINT__: string;
  const __HYPRIAL_LIVE_ENABLED__: boolean;
  const __HYPRIAL_REFERENCE_HTTP__: boolean;
  interface Window {
    /** Narrow trusted native capability, never renderer-owned credentials. */
    readonly __hyprialAccount?: AuthClient;
  }
}
