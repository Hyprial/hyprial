import { useState } from 'react';
import type { Dispatch } from 'react';
import type { AppAction } from '../state/types';
import {
  formatStubNotice,
  type StubOperations,
  type StubResult,
} from './stub';

export interface StubAction {
  readonly busy: boolean;
  run(
    command: string,
    params?: Readonly<Record<string, unknown>>,
  ): Promise<StubResult | undefined>;
}

/**
 * Shared UI plumbing for this batch: invoke the placeholder, surface the
 * sanitized rejection, and never manufacture a successful local transition.
 */
export function useStubAction(
  client: StubOperations,
  dispatch: Dispatch<AppAction>,
): StubAction {
  const [busy, setBusy] = useState(false);

  async function run(
    command: string,
    params: Readonly<Record<string, unknown>> = {},
  ): Promise<StubResult | undefined> {
    if (busy) {
      return undefined;
    }
    setBusy(true);
    try {
      const result = await client.call(command, params);
      dispatch({
        type: 'push-notice',
        tone: 'warning',
        text: formatStubNotice(result),
        source: command,
      });
      return result;
    } catch {
      dispatch({
        type: 'push-notice',
        tone: 'error',
        text: '操作失败：服务暂时无法连接。',
        source: command,
      });
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  return { busy, run };
}
