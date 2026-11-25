import type { MethodAccess, MethodAvailability } from '../../client';
import type { GeneratedClient } from '../../client/generated';

/** Host-selected data source. Features never select or infer this value. */
export type BackendDriver = 'daemon' | 'contract-mock';
/** Display metadata, not authorization or a claim that every method is callable. */
export type BackendPhase = 'bootstrap' | 'restoring' | 'ready';
export type BackendTransition = 'initialize' | 'logout' | 'reconnect';

/** Safe verified catalog metadata. Never contains daemon credentials or schemas. */
export interface BackendMethod {
  readonly name: string;
  readonly callable: boolean;
  readonly state: string;
  readonly mutates: boolean;
}

/** Internal injection contract v1; not a new HTTP or daemon wire schema. */
export interface BackendDescriptor {
  readonly interfaceVersion: 1;
  readonly driver: BackendDriver;
  readonly phase: BackendPhase;
  readonly generation: string;
  readonly catalogDigest: string;
  /** Host-verified display/session scope only; never request authorization. */
  readonly principalActor?: string;
  readonly principalNode?: string;
  /** Omitted only by direct test/fixture injections without a served catalog. */
  readonly methods?: readonly BackendMethod[];
  /** Mutating methods admitted by the verified host session. Omitted by legacy fixtures. */
  readonly verifiedWrites?: readonly string[];
}

export interface BackendConnection {
  readonly descriptor: BackendDescriptor;
  readonly client: GeneratedClient;
  readonly methods: readonly BackendMethod[] | null;
  readonly methodStatus: (method: string) => MethodAvailability;
  readonly methodAccess: MethodAccess;
  readonly beginTransition?: (reason: BackendTransition) => void;
}

/** Only the common composition/lifecycle owner receives this control handle. */
export interface ManagedBackendConnection extends BackendConnection {
  /** Reject new calls through retained adapters. Does not retry/cancel in-flight writes. */
  retire(): void;
}

export type BackendState =
  | { readonly status: 'connected'; readonly connection: BackendConnection }
  | { readonly status: 'connecting' | 'transitioning' | 'unavailable'; readonly reason?: string };

export type { GeneratedClient } from '../../client/generated';
