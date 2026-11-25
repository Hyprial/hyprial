/**
 * 删除组织网关（OC7 两步删除）：previewDelete 与 deleteOrg 是两个用户动作，各只发一个 org.delete。
 *
 * 决策：
 * - 只有确认阶段为 complete 才算删除成功；预览已是 complete 时刷新列表，由对话框按已删除处理。
 * - 预览阶段失败时 daemon 什么都没写：不刷新列表，按删除专用文案显示
 *   （AUTHORITY_REQUIRED 不借用邀请文案）。
 * - 确认阶段失败或阶段不是 complete：删除可能已部分执行，先刷新列表再提示；
 *   OPERATION_CONFLICT 在写入前被拒绝，提示未删除；REMOVAL_CONFIRMATION_STALE 与 AUTHORITY_REQUIRED
 *   不断言删除结果（竞争或续删时可能已删除）。
 */
import { describe, expect, it } from 'vitest';
import {
  DomainError,
  TransportError,
  type DispatchState,
  type RequestDiagnostics,
} from '../../../src/client';
import { createOrgLiveSourceForPort } from '../../support/org-backend';
import type { OrgStore } from '../../../src/features/org/impl/domain/context';
import type { OrgDomainSnapshot } from '../../../src/features/org/impl/domain/types';
import { ORG_COPY, ORG_DELETE_REASONS } from '../../../src/features/org/impl/domain/copy';

const ORG = 'group-x';

function diagnostics(dispatch: DispatchState): RequestDiagnostics {
  return {
    method: 'org.delete',
    requestId: null,
    status: null,
    dispatch,
    catalogDigest: null,
    attempts: 1,
    startedAt: 0,
    durationMs: 1,
  };
}

const refused = (code: string, message = `daemon says ${code}`) =>
  new DomainError(code, message, null, diagnostics('dispatched'));

type Answer = (params: Record<string, unknown>) => unknown;

function deletePort(answer: Answer) {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  return {
    calls,
    deletes: () => calls.filter((call) => call.method === 'org.delete').map((call) => call.params),
    listed: () => calls.some((call) => call.method === 'org.list'),
    async request(method: string, params: Record<string, unknown>) {
      calls.push({ method, params });
      if (method === 'org.delete') return answer(params);
      if (method === 'org.list') {
        return {
          orgs: [{ org: ORG, spaceId: 'space-x', role: 'owner', devices: 1, state: 'active' }],
          leftOrgs: [],
        };
      }
      throw new Error(`unexpected method ${method}`);
    },
  };
}

function gatewayFor(port: ReturnType<typeof deletePort>) {
  const source = createOrgLiveSourceForPort(port);
  let current: OrgDomainSnapshot = source.initial;
  const store: OrgStore = {
    get: () => current,
    update: (next) => {
      current = next(current);
    },
  };
  return source.createGateway(store);
}

const preview = {
  org: ORG,
  operationId: 'op-1',
  phase: 'pending-confirmation',
  deviceIds: ['dev-1', 'dev-2'],
  authorityDeviceId: 'dev-1',
  irreversible: true,
};
const confirmParams = { org: ORG, confirmed: true, operationId: 'op-1' };

function confirmAnswers(confirm: Answer): Answer {
  return (params) => (params.confirmed === true ? confirm(params) : preview);
}

function message(outcome: { readonly kind: string; readonly message?: string }): string {
  return outcome.kind === 'failed' ? (outcome.message ?? '') : '';
}

describe('org delete preview', () => {
  it('reads one unconfirmed org.delete and returns what the dialog shows', async () => {
    const port = deletePort(() => preview);
    await expect(gatewayFor(port).previewDelete(ORG)).resolves.toEqual({
      kind: 'ok',
      value: {
        operationId: 'op-1',
        phase: 'pending-confirmation',
        devices: ['dev-1', 'dev-2'],
        authority: 'dev-1',
        irreversible: true,
      },
    });
    expect(port.deletes()).toEqual([{ org: ORG }]);
    expect(port.listed()).toBe(false);
  });

  it('refreshes the list when the preview already reports complete', async () => {
    const port = deletePort(() => ({ ...preview, phase: 'complete' }));
    await expect(gatewayFor(port).previewDelete(ORG)).resolves.toMatchObject({
      kind: 'ok',
      value: { phase: 'complete' },
    });
    expect(port.deletes()).toEqual([{ org: ORG }]);
    expect(port.listed()).toBe(true);
  });

  it('rejects a shapeless preview or one missing the operationId, device list or irreversible flag', async () => {
    for (const answer of [
      {},
      { ...preview, operationId: '' },
      { ...preview, deviceIds: 'dev-1' },
      { ...preview, irreversible: undefined },
    ]) {
      const port = deletePort(() => answer);
      await expect(gatewayFor(port).previewDelete(ORG)).resolves.toMatchObject({
        kind: 'failed',
        unknown: true,
      });
    }
  });

  it('shows the delete-specific authority message when the preview is refused on a non-authority device', async () => {
    const port = deletePort(() => {
      throw refused('AUTHORITY_REQUIRED');
    });
    const outcome = await gatewayFor(port).previewDelete(ORG);
    expect(outcome).toEqual({
      kind: 'failed',
      code: 'AUTHORITY_REQUIRED',
      message: ORG_DELETE_REASONS.AUTHORITY_REQUIRED,
    });
    expect(message(outcome)).toContain('管理设备上删除');
    expect(message(outcome)).not.toContain('邀请');
    expect(port.listed()).toBe(false);
  });

  it('names the blocking sub-orgs from the daemon after a Chinese lead-in', async () => {
    const port = deletePort(() => {
      throw refused('OPERATION_CONFLICT', "org 'group-x' still has sub-orgs group-x-a");
    });
    const outcome = await gatewayFor(port).previewDelete(ORG);
    expect(outcome).toMatchObject({ kind: 'failed', code: 'OPERATION_CONFLICT' });
    expect(message(outcome)).toMatch(/^请先删除它的子组织/);
    expect(message(outcome)).toContain('group-x-a');
  });

  it('refreshes the list when the daemon says the org is already deleted', async () => {
    const port = deletePort(() => {
      throw refused('ORG_ALREADY_DELETED');
    });
    await expect(gatewayFor(port).previewDelete(ORG)).resolves.toEqual({
      kind: 'failed',
      code: 'ORG_ALREADY_DELETED',
      message: ORG_DELETE_REASONS.ORG_ALREADY_DELETED,
    });
    expect(port.listed()).toBe(true);
  });
});

describe('org delete confirm', () => {
  it('sends exactly one confirmed org.delete bound to the operationId and succeeds only on complete', async () => {
    const port = deletePort(confirmAnswers(() => ({ ...preview, phase: 'complete' })));
    await expect(gatewayFor(port).deleteOrg(ORG, 'op-1')).resolves.toEqual({
      kind: 'ok',
      value: undefined,
    });
    expect(port.deletes()).toEqual([confirmParams]);
    expect(port.listed()).toBe(true);
  });

  it('refreshes and says the deletion may be partly done when the confirm is refused', async () => {
    const port = deletePort(
      confirmAnswers(() => {
        throw refused('MEMBERSHIP_SIDECAR_TIMEOUT');
      }),
    );
    const outcome = await gatewayFor(port).deleteOrg(ORG, 'op-1');
    expect(outcome).toMatchObject({ kind: 'failed', code: 'MEMBERSHIP_SIDECAR_TIMEOUT' });
    expect(message(outcome)).toBe(ORG_COPY.deletePartial('组织网络响应超时。'));
    expect(port.deletes()).toEqual([confirmParams]);
    expect(port.listed()).toBe(true);
  });

  it('keeps the possibly-partial hint for a REVISION_CONFLICT confirm, naming the roster change', async () => {
    const port = deletePort(
      confirmAnswers(() => {
        throw refused('REVISION_CONFLICT');
      }),
    );
    const outcome = await gatewayFor(port).deleteOrg(ORG, 'op-1');
    expect(outcome).toMatchObject({ kind: 'failed', code: 'REVISION_CONFLICT' });
    expect(message(outcome)).toBe(ORG_COPY.deletePartial(ORG_DELETE_REASONS.REVISION_CONFLICT));
    expect(port.listed()).toBe(true);
  });

  it('keeps the unknown outcome of a confirm that timed out and still refreshes', async () => {
    const port = deletePort(
      confirmAnswers(() => {
        throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'timeout', diagnostics('unknown'));
      }),
    );
    const outcome = await gatewayFor(port).deleteOrg(ORG, 'op-1');
    expect(outcome).toMatchObject({ kind: 'failed', code: 'REQUEST_TIMEOUT', unknown: true });
    expect(message(outcome)).toMatch(/^删除可能已部分执行/);
    expect(port.listed()).toBe(true);
  });

  it('treats a shapeless confirm answer as unknown and possibly partial', async () => {
    const port = deletePort(confirmAnswers(() => ({})));
    const outcome = await gatewayFor(port).deleteOrg(ORG, 'op-1');
    expect(outcome).toMatchObject({ kind: 'failed', unknown: true });
    expect(message(outcome)).toMatch(/^删除可能已部分执行/);
    expect(port.listed()).toBe(true);
  });

  it('refreshes and reports the daemon phase when the confirm answers a phase other than complete', async () => {
    const port = deletePort(confirmAnswers(() => ({ ...preview, phase: 'tailnet-pending' })));
    const outcome = await gatewayFor(port).deleteOrg(ORG, 'op-1');
    expect(outcome).toMatchObject({ kind: 'failed' });
    expect(message(outcome)).toBe(ORG_COPY.deletePartial(ORG_COPY.deletePhase('tailnet-pending')));
    expect(port.listed()).toBe(true);
  });

  it('gives the stale or authority copy, not the partial-deletion hint, when the confirm is refused that way', async () => {
    const stale = deletePort(
      confirmAnswers(() => {
        throw refused('REMOVAL_CONFIRMATION_STALE');
      }),
    );
    await expect(gatewayFor(stale).deleteOrg(ORG, 'op-1')).resolves.toEqual({
      kind: 'failed',
      code: 'stale',
      message: ORG_COPY.deleteStale,
    });
    const authority = deletePort(
      confirmAnswers(() => {
        throw refused('AUTHORITY_REQUIRED');
      }),
    );
    await expect(gatewayFor(authority).deleteOrg(ORG, 'op-1')).resolves.toMatchObject({
      kind: 'failed',
      code: 'AUTHORITY_REQUIRED',
      message: ORG_DELETE_REASONS.AUTHORITY_REQUIRED,
    });
  });
});
