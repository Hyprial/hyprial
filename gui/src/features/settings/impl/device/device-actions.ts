/**
 * Initialize owns device preparation in the daemon. This adapter submits one
 * write and leaves observation to account status and the host transition.
 * An unknown outcome stays latched for this transport; no automatic replay.
 */
import { ClientError, DomainError, accessRefusal, type MethodAccess } from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';
import type { DeviceActionResult, FirstRunActions } from '../../../onboarding';

type DeviceClient = Pick<GeneratedClient, 'onboarding'>;
const unknown = (): DeviceActionResult => ({ status: 'unknown', message: '初始化结果未知，请刷新状态；不会自动重发。' });
const operationKey = () => globalThis.crypto?.randomUUID?.() ?? `init-${Date.now()}-${Math.random().toString(36).slice(2)}`;

async function initializeDevice(client: DeviceClient, access?: MethodAccess, beginTransition?: () => void): Promise<DeviceActionResult> {
  const refusal = accessRefusal(access, 'onboarding.initialize');
  if (refusal) return { status: 'unavailable', refusal };
  try {
    const receipt = await client.onboarding.initialize({ operationKey: operationKey() });
    if (receipt.state === 'completed' && receipt.initialized) return { status: 'ready' };
    if (receipt.state === 'failed' || receipt.state === 'cancelled') return { status: 'failed', message: '初始化未完成，请刷新状态。' };
    if (receipt.state === 'accepted' || receipt.state === 'promoting') beginTransition?.();
    return unknown();
  } catch (error) {
    if (error instanceof ClientError) {
      if (error.dispatch === 'unknown' || error.code === 'SUBMIT_OUTCOME_UNKNOWN') return unknown();
      if (error.code === 'IDENTITY_UNBOUND') return { status: 'not-ready', message: '请先登录。' };
      if (error instanceof DomainError && error.data && typeof error.data === 'object'
        && 'retryable' in error.data && error.data.retryable === false) return unknown();
      return { status: 'failed', message: `初始化未完成（${error.code}）。`, retryable: true };
    }
    return unknown();
  }
}

export function createConnectedDeviceActions(client: DeviceClient, access?: MethodAccess, beginTransition?: () => void): Pick<FirstRunActions, 'initializeDevice'> {
  let inFlight: Promise<DeviceActionResult> | null = null;
  return { initializeDevice: () => {
    if (!inFlight) inFlight = initializeDevice(client, access, beginTransition).then(result => {
      if (result.status !== 'unknown') inFlight = null;
      return result;
    });
    return inFlight;
  } };
}
