import {
  TransportError,
  type HttpTransport,
  type MethodAccess,
  type MethodAvailability,
  type RequestOptions,
} from '../../client';
import { createGeneratedClient } from '../../client/generated';
import type { BackendDescriptor, BackendMethod, ManagedBackendConnection } from './types';

/**
 * Common composition only: bind one already-authorized transport generation.
 * Browser bootstrap constructs it after the same-origin session handshake.
 * Passing metadata here neither authenticates nor starts a host. Features
 * receive only a Pick<GeneratedClient, ...> from the returned client.
 * Availability starts with the served catalog. Browser sessions also pin the
 * mutating methods admitted by the verified host; legacy fixtures may omit
 * that list. A present list fails closed for every unlisted write.
 */
export function createBackendConnection(
  transport: Pick<HttpTransport, 'invoke'>,
  descriptor: BackendDescriptor,
  lifecycle: Pick<ManagedBackendConnection, 'beginTransition'> = {},
): ManagedBackendConnection {
  if (descriptor.interfaceVersion !== 1
    || !['daemon', 'contract-mock'].includes(descriptor.driver)
    || !['bootstrap', 'restoring', 'ready'].includes(descriptor.phase)
    || typeof descriptor.generation !== 'string' || !descriptor.generation.trim()
    || (descriptor.principalActor !== undefined && (typeof descriptor.principalActor !== 'string' || !descriptor.principalActor.trim()))
    || (descriptor.principalNode !== undefined && (typeof descriptor.principalNode !== 'string' || !descriptor.principalNode.trim()))
    || typeof descriptor.catalogDigest !== 'string' || !descriptor.catalogDigest.trim()) {
    throw new TypeError('invalid backend injection descriptor');
  }
  let methods: readonly BackendMethod[] | null = null;
  if (descriptor.methods !== undefined) {
    const seen = new Set<string>();
    for (const method of descriptor.methods) {
      if (!method || typeof method.name !== 'string' || !method.name.trim()
        || typeof method.callable !== 'boolean'
        || typeof method.state !== 'string' || !method.state.trim()
        || typeof method.mutates !== 'boolean') {
        throw new TypeError('invalid backend catalog method metadata');
      }
      if (seen.has(method.name)) throw new TypeError('duplicate backend catalog method');
      seen.add(method.name);
    }
    methods = Object.freeze(descriptor.methods.map((method) => Object.freeze({
      name: method.name,
      callable: method.callable,
      state: method.state,
      mutates: method.mutates,
    })));
  }
  let verifiedWrites: ReadonlySet<string> | null = null;
  if (descriptor.verifiedWrites !== undefined) {
    const seen = new Set<string>();
    for (const method of descriptor.verifiedWrites) {
      if (typeof method !== 'string' || !method.trim() || seen.has(method)) {
        throw new TypeError('invalid verified write metadata');
      }
      if (methods !== null && !methods.some((entry) => entry.name === method && entry.mutates)) {
        throw new TypeError('verified write is not a mutating catalog method');
      }
      seen.add(method);
    }
    verifiedWrites = Object.freeze(new Set(seen));
  }
  const methodStatus = (method: string): MethodAvailability => {
    if (methods === null) return Object.freeze({ available: true });
    const declared = methods.find((entry) => entry.name === method);
    if (declared === undefined) {
      return Object.freeze({
        available: false,
        code: 'METHOD_NOT_FOUND',
        reason: `当前后端目录没有声明 ${method}（METHOD_NOT_FOUND）。`,
      });
    }
    if (!declared.callable) {
      return Object.freeze({
        available: false,
        code: 'METHOD_NOT_CALLABLE',
        reason: `当前后端目录声明 ${method} 不可调用（${declared.state}）。`,
      });
    }
    return Object.freeze({ available: true });
  };
  const methodAccess: MethodAccess = (method) => {
    const catalogStatus = methodStatus(method);
    if (!catalogStatus.available || verifiedWrites === null) return catalogStatus;
    const mutates = methods?.find((entry) => entry.name === method)?.mutates === true;
    if (mutates && !verifiedWrites.has(method)) {
      return Object.freeze({
        available: false,
        code: 'WRITE_METHOD_UNVERIFIED',
        reason: `当前已验证会话未准入写方法 ${method}（WRITE_METHOD_UNVERIFIED）。`,
      });
    }
    return catalogStatus;
  };
  // Copy only public fields. Never retain arbitrary host/session credentials.
  const snapshot: BackendDescriptor = Object.freeze({
    interfaceVersion: 1,
    driver: descriptor.driver,
    phase: descriptor.phase,
    generation: descriptor.generation,
    catalogDigest: descriptor.catalogDigest,
    ...(descriptor.principalActor === undefined ? {} : { principalActor: descriptor.principalActor }),
    ...(descriptor.principalNode === undefined ? {} : { principalNode: descriptor.principalNode }),
    ...(methods === null ? {} : { methods }),
    ...(verifiedWrites === null ? {} : { verifiedWrites: Object.freeze([...verifiedWrites]) }),
  });
  let retired = false;
  const client = createGeneratedClient({
    async invoke<TResult>(method: string, params: object = {}, options: RequestOptions = {}): Promise<TResult> {
      if (retired) {
        throw new TransportError('configuration', 'BACKEND_CONNECTION_RETIRED',
          'backend connection was retired; obtain the current connection', {
            method, requestId: null, status: null, dispatch: 'not-dispatched',
            catalogDigest: snapshot.catalogDigest, attempts: 0, startedAt: Date.now(), durationMs: 0,
          });
      }
      const availability = methodAccess(method, params);
      if (!availability.available) {
        throw new TransportError('configuration', availability.code ?? 'METHOD_NOT_AVAILABLE',
          availability.reason ?? `${method} is unavailable in the pinned catalog`, {
            method, requestId: null, status: null, dispatch: 'not-dispatched',
            catalogDigest: snapshot.catalogDigest, attempts: 0, startedAt: Date.now(), durationMs: 0,
          });
      }
      // Do not catch/reclassify errors or replay requests. In-flight results keep
      // their original dispatch/receipt semantics after this generation retires.
      return transport.invoke<TResult>(method, params, { ...options, catalogDigest: snapshot.catalogDigest });
    },
  });
  return Object.freeze({
    descriptor: snapshot,
    client,
    methods,
    methodStatus,
    methodAccess,
    ...(lifecycle.beginTransition ? { beginTransition: lifecycle.beginTransition } : {}),
    retire() { retired = true; },
  });
}
