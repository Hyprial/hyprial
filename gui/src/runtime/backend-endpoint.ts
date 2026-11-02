export interface BackendEndpointInfo {
  readonly configured: boolean;
  readonly endpoint: string;
  readonly label: string;
}

function compiledEndpoint(): string {
  return typeof __HYPRIAL_BACKEND_ENDPOINT__ === 'string'
    ? __HYPRIAL_BACKEND_ENDPOINT__
    : '';
}

/**
 * Browser-visible configuration only. It never turns a configured local stub
 * into a claim that the backend, account or Agent is online.
 */
export function getBackendEndpointInfo(
  raw: string | null | undefined = compiledEndpoint(),
): BackendEndpointInfo {
  const endpoint = (raw ?? '').trim();
  return endpoint
    ? {
        configured: true,
        endpoint,
        label: `本机占位端点 ${endpoint}`,
      }
    : {
        configured: false,
        endpoint: '',
        label: '未配置本机占位端点',
      };
}
