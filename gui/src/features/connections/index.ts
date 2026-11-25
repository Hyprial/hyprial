import './impl/styles/agent-settings.css';

export { CredentialsSection } from './CredentialsSection';
export { PlatformIntegrationsSection } from './PlatformIntegrationsSection';
export { PlatformIntegrationDetail } from './PlatformIntegrationDetail';
export { type CredentialsSectionModel } from './credentials-view-model';
export {
  createPlatformIntegrationsApiFromClient,
  PLATFORM_PROVIDERS,
  type PlatformIntegrationsApi,
  type PlatformIntegration,
  type AdapterAuthorization,
  type AdapterIdentityRow,
  type AdapterOperation,
  type AdapterOperationStatus,
  type AddPlatformInput,
  type RegisterPlatformInput,
  type AdapterRegistration,
} from './platform-integrations-api';
export {
  createNativeAuthApiFromClient,
  createSharedCredentialsApiFromClient,
  type CredentialEntry,
  type CredentialGrant,
  type SharedCredentialAction,
  type SharedCredentialWrite,
  type SharedCredentialsApi,
  type SharedGrantRow,
} from './shared-credentials-api';

export { SECRET_PROVIDER_ENVIRONMENTS, secretEntryReference, parseSecretEntryReference, secretProviderForEnvironments } from './impl/secret-entries';
