/**
 * Agent 设置页。
 *
 * 共用凭据与平台接入是两个独立的业务入口；Agent 详情负责平台绑定。
 * 页面只消费 capability 与显式配置端口。
 */
import { CredentialsSection, PlatformIntegrationsSection } from '../features/connections';
import type {
  CredentialsSectionModel,
  PlatformIntegrationsApi,
  SharedCredentialsApi,
} from '../features/connections';
import { useGuiCapabilities } from '../app/capabilities';

interface ConnectionsPageProps {
  readonly credentials?: CredentialsSectionModel;
  readonly credentialsApi?: SharedCredentialsApi;
  readonly platformIntegrationsApi?: PlatformIntegrationsApi;
}

export function ConnectionsPage({
  credentials,
  credentialsApi,
  platformIntegrationsApi,
}: ConnectionsPageProps) {
  const capabilities = useGuiCapabilities();
  const credentialsPort = capabilities.sharedCredentials ?? credentialsApi;
  const platformsPort = platformIntegrationsApi ?? capabilities.platformIntegrations;
  const hasCredentials = credentialsPort !== undefined || credentials !== undefined;
  return (
    <section className="page page-connections" data-page="connections">
      <h1 className="page-title">Agent 设置</h1>
      {(hasCredentials || platformsPort !== undefined) && (
        <div className="connections-flow">
          {hasCredentials && <CredentialsSection api={credentialsPort} model={credentials} />}
          {platformsPort !== undefined && <PlatformIntegrationsSection api={platformsPort} />}
        </div>
      )}
    </section>
  );
}
