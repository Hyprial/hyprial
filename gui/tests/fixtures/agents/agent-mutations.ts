/**
 * implG2 (2026-11-02) fixtures: create/stop contract cases.
 *
 * Single source: MethodSpec examples (implBc) as emitted into
 * the generated contract table. No second DTO is invented here; the shapes below
 * are re-exports of the contract corpus so tests and review read the same
 * examples the Contract Mock serves.
 * implG3 (2026-11-02): agent.start left the contract at 966709bdab, so its
 * example set no longer exists here; start-availability tests assert zero
 * requests instead of contract examples.
 */

import { contractExamples } from '../../../src/dev/contract-mock';

export interface ContractExampleCase {
  readonly name: string;
  readonly kind: string;
  readonly description: string;
  readonly params: Record<string, unknown>;
  readonly result?: Record<string, unknown>;
  readonly error?: { readonly code: string; readonly message: string; readonly data?: unknown };
}

export const AGENT_CREATE_CASES = contractExamples('agent.create') as readonly ContractExampleCase[];
export const AGENT_STOP_CASES = contractExamples('agent.stop') as readonly ContractExampleCase[];

export function contractCase(
  cases: readonly ContractExampleCase[],
  name: string,
): ContractExampleCase {
  const found = cases.find((entry) => entry.name === name);
  if (!found) {
    throw new Error(`contract example case not found: ${name}`);
  }
  return found;
}

export const CREATE_NEW_AGENT = contractCase(AGENT_CREATE_CASES, 'new-agent');
export const CREATE_NAME_TAKEN = contractCase(AGENT_CREATE_CASES, 'name-taken');

export const STOP_RUNNING_AGENT = contractCase(AGENT_STOP_CASES, 'running-agent');
export const STOP_NOTHING_RUNNING = contractCase(AGENT_STOP_CASES, 'nothing-running');
export const STOP_UNSETTLED = contractCase(AGENT_STOP_CASES, 'stop-unsettled');

/** The actor name shared by the lifecycle example corpus. */
export const EXAMPLE_AGENT_NAME = 'worker';
