const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };

/**
 * Read the product composition root configuration from explicit environment
 * variables. Nothing here has a hidden default backend: the transport driver
 * and the trusted principal must be named, or startup fails closed with a
 * named error instead of silently serving mock or anonymous data.
 */
export function resolveProductConfig(env = {}) {
  const missing = [];
  const driver = env.HYPRIAL_GUI_TRANSPORT_DRIVER;
  if (typeof driver !== 'string' || !driver.trim()) missing.push('HYPRIAL_GUI_TRANSPORT_DRIVER');
  const principalId = env.HYPRIAL_GUI_PRINCIPAL_ID;
  if (typeof principalId !== 'string' || !principalId.trim()) missing.push('HYPRIAL_GUI_PRINCIPAL_ID');
  if (missing.length) {
    fail('GUI_PRODUCT_CONFIG', `Missing required GUI configuration: ${missing.join(', ')}`);
  }
  let transportConfig = {};
  if (env.HYPRIAL_GUI_TRANSPORT_CONFIG !== undefined) {
    try {
      transportConfig = JSON.parse(env.HYPRIAL_GUI_TRANSPORT_CONFIG);
    } catch {
      fail('GUI_PRODUCT_CONFIG', 'HYPRIAL_GUI_TRANSPORT_CONFIG must be a JSON object');
    }
    if (!transportConfig || typeof transportConfig !== 'object' || Array.isArray(transportConfig)) {
      fail('GUI_PRODUCT_CONFIG', 'HYPRIAL_GUI_TRANSPORT_CONFIG must be a JSON object');
    }
  }
  const studioRoot = env.HYPRIAL_GUI_STUDIO_ROOT;
  const studio = typeof studioRoot === 'string' && studioRoot.trim()
    ? { persistence: 'fs', root: studioRoot }
    : { persistence: 'memory' };
  const host = env.HYPRIAL_GUI_HOST || '127.0.0.1';
  let port = 0;
  if (env.HYPRIAL_GUI_PORT !== undefined && env.HYPRIAL_GUI_PORT !== '') {
    port = Number(env.HYPRIAL_GUI_PORT);
    if (!Number.isSafeInteger(port) || port < 0 || port > 65535) {
      fail('GUI_PRODUCT_CONFIG', 'HYPRIAL_GUI_PORT must be an integer between 0 and 65535');
    }
  }
  const token = env.HYPRIAL_GUI_TOKEN;
  if (token !== undefined && (typeof token !== 'string' || token.length < 16)) {
    fail('GUI_PRODUCT_CONFIG', 'HYPRIAL_GUI_TOKEN must be at least 16 characters when provided');
  }
  return {
    transport: { driver, config: transportConfig },
    principalId,
    studio,
    listen: { host, port },
    ...(token ? { token } : {}),
    ...(env.HYPRIAL_GUI_ASSETS_DIR ? { assetsDir: env.HYPRIAL_GUI_ASSETS_DIR } : {}),
  };
}
