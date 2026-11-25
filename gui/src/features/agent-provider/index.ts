/**
 * Decision record (GUI deep modules B4/C2, 2026-11-08):
 * - Formal surface only: the settings section view, the daemon-backed adapter
 *   factories and the login-file builder used by the app composition.
 * - Development mocks and the generated examples fixture live under
 *   `src/dev/mock/`; this entry never re-exports them.
 */

export { NativeAuthList, type NativeAuthApi, type NativeAuthRecord } from './impl/native-auth';
export { CredentialList, CredentialListItem } from './impl/credential-list';
export { type ProviderLoginFile } from './impl/api';
export {
  createProviderLoginFilePicker,
  createProviderLoginFiles,
  providerLoginFileNames,
  providerLoginFileLocation,
  providerLoginFileMessage,
  type ProviderLoginFilePicker,
  type ProviderLoginFiles,
} from './impl/login-files';
