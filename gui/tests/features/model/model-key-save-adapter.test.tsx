import { afterEach, describe, expect, it, vi } from 'vitest';
import { DomainError, TransportError, type RequestDiagnostics, type RequestOptions } from '../../../src/client';
import type { GeneratedClient } from '../../../src/client/generated';
import type { AgentSecretProviderWriteParams } from '../../../src/client/generated/types';
import {
  createConnectedModelKeyActions,
  createModelKeySaveAdapter,
  DEEPSEEK_MODEL_KEY_TARGET,
} from '../../../src/features/model';

const TARGET = { entryId: 'caller-supplied-entry', fieldName: 'caller-supplied-field' } as const;

type Write = (
  params: AgentSecretProviderWriteParams,
  options?: RequestOptions,
) => Promise<unknown>;

function clientWith(write: Write): Pick<GeneratedClient, 'agent'> {
  return { agent: { secret: { providerWrite: write } } } as unknown as Pick<GeneratedClient, 'agent'>;
}

function diagnostics(dispatch: 'not-dispatched' | 'dispatched' | 'unknown'): RequestDiagnostics {
  return {
    method: 'agent.secret.provider-write',
    requestId: null,
    status: null,
    dispatch,
    catalogDigest: null,
    attempts: dispatch === 'not-dispatched' ? 0 : 1,
    startedAt: 0,
    durationMs: 1,
  };
}

function receipt(): unknown {
  return { ok: true, entryId: TARGET.entryId, fieldNames: [TARGET.fieldName] };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('model key save adapter', () => {
  it('uses the caller-supplied target and generated providerWrite, then scrubs its key reference', async () => {
    let sentValue = '';
    let captured: AgentSecretProviderWriteParams | undefined;
    const write = vi.fn(async (params: AgentSecretProviderWriteParams) => {
      captured = params;
      sentValue = params.value;
      return receipt();
    });
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    await expect(adapter.save('  synthetic-key  ')).resolves.toEqual({ status: 'saved' });

    expect(adapter.target).toEqual(TARGET);
    expect(write).toHaveBeenCalledTimes(1);
    expect(sentValue).toBe('synthetic-key');
    expect(captured).toMatchObject({ entryId: TARGET.entryId, fieldName: TARGET.fieldName });
    expect(captured?.value).toBe('');
  });

  it('reports a method-access refusal as the machine refusal only, without dispatching', async () => {
    const write = vi.fn();
    const access = vi.fn(() => ({ available: false, code: 'METHOD_NOT_CALLABLE', reason: '目录声明该方法不可调用。' }));
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET, access);

    await expect(adapter.save('synthetic-key')).resolves.toEqual({
      status: 'unavailable',
      refusal: { code: 'METHOD_NOT_CALLABLE', source: 'agent.secret.provider-write' },
    });
    expect(write).not.toHaveBeenCalled();
    expect(access).toHaveBeenCalledWith('agent.secret.provider-write', {
      entryId: TARGET.entryId,
      fieldName: TARGET.fieldName,
    });
  });

  it('treats a failing access check as a definite failure without dispatching', async () => {
    const write = vi.fn();
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET, () => {
      throw new Error('catalog closed');
    });

    await expect(adapter.save('synthetic-key')).resolves.toMatchObject({ status: 'failed' });
    expect(write).not.toHaveBeenCalled();
  });

  it.each([
    ['an empty key', TARGET, '   '],
    ['a key containing a newline', TARGET, 'line-one\nline-two'],
    ['a missing target', { entryId: '', fieldName: 'caller-supplied-field' }, 'synthetic-key'],
  ])('does not dispatch with %s', async (_label, target, key) => {
    const write = vi.fn();
    const access = vi.fn();
    const adapter = createModelKeySaveAdapter(clientWith(write), target, access);

    await expect(adapter.save(key)).resolves.toMatchObject({ status: 'failed' });
    expect(write).not.toHaveBeenCalled();
    expect(access).not.toHaveBeenCalled();
  });

  it('does not dispatch when the caller aborted before saving', async () => {
    const write = vi.fn();
    const controller = new AbortController();
    controller.abort();
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    await expect(adapter.save('synthetic-key', { signal: controller.signal })).resolves.toMatchObject({
      status: 'failed',
    });
    expect(write).not.toHaveBeenCalled();
  });

  it('maps a not-dispatched client rejection to a definite failure without retrying or echoing the key', async () => {
    const write = vi.fn(async () => {
      throw new DomainError('AGENT_NOT_FOUND', 'refused synthetic-key', null, diagnostics('not-dispatched'));
    });
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    const result = await adapter.save('synthetic-key');

    expect(result.status).toBe('failed');
    expect(result.message).toBe('本次保存未派发，未写入凭证。');
    expect(result.message).not.toContain('synthetic-key');
    expect(write).toHaveBeenCalledTimes(1);
  });

  it.each(['dispatched', 'unknown'] as const)(
    'keeps a %s client failure unknown and never replays it',
    async dispatch => {
      const write = vi.fn(async () => {
        throw new TransportError('network', 'NETWORK_ERROR', 'response lost', diagnostics(dispatch));
      });
      const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

      const result = await adapter.save('synthetic-key');

      expect(result.status).toBe('unknown');
      expect(result.message).toMatch(/不会自动重试/);
      expect(write).toHaveBeenCalledTimes(1);
    },
  );

  it('keeps an abort after dispatch unknown and never replays it', async () => {
    const write = vi.fn(async () => {
      throw new TransportError('aborted', 'REQUEST_ABORTED', 'aborted after dispatch', diagnostics('unknown'));
    });
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    await expect(adapter.save('synthetic-key', { signal: new AbortController().signal })).resolves.toMatchObject({
      status: 'unknown',
    });
    expect(write).toHaveBeenCalledTimes(1);
  });

  it('keeps an untyped client failure unknown rather than claiming a definite rejection', async () => {
    const write = vi.fn(async () => {
      throw new Error('socket closed');
    });
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    await expect(adapter.save('synthetic-key')).resolves.toMatchObject({ status: 'unknown' });
    expect(write).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a mismatched entry', { ok: true, entryId: 'other-entry', fieldNames: [TARGET.fieldName] }],
    ['a missing field', { ok: true, entryId: TARGET.entryId, fieldNames: ['other-field'] }],
    ['a negative acknowledgement', { ok: false, entryId: TARGET.entryId, fieldNames: [TARGET.fieldName] }],
    ['non-string field names', { ok: true, entryId: TARGET.entryId, fieldNames: [1] }],
  ])('keeps %s unknown without retrying', async (_label, value) => {
    const write = vi.fn(async () => value);
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    const result = await adapter.save('synthetic-key');

    expect(result.status).toBe('unknown');
    expect(result.message).toMatch(/不会自动重试/);
    expect(write).toHaveBeenCalledTimes(1);
  });

  it('does not write the key to web storage, history, or logs', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const pushState = vi.spyOn(history, 'pushState');
    const log = vi.spyOn(console, 'log');
    const error = vi.spyOn(console, 'error');
    const warn = vi.spyOn(console, 'warn');
    const write = vi.fn(async () => receipt());
    const adapter = createModelKeySaveAdapter(clientWith(write), TARGET);

    await adapter.save('synthetic-secret');

    expect(setItem).not.toHaveBeenCalled();
    expect(pushState).not.toHaveBeenCalled();
    expect(log).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain('synthetic-secret');
  });
});

describe('connected model key actions', () => {
  function connectedClient(options: {
    readonly grants?: readonly Record<string, unknown>[];
    readonly agents?: readonly Record<string, unknown>[];
    readonly write?: Write;
    readonly listFailure?: Error;
  } = {}) {
    const write = options.write ?? vi.fn(async () => ({
      ok: true,
      entryId: DEEPSEEK_MODEL_KEY_TARGET.entryId,
      fieldNames: [DEEPSEEK_MODEL_KEY_TARGET.fieldName],
    }));
    const secretList = vi.fn(async () => {
      if (options.listFailure) throw options.listFailure;
      return { ok: true, grants: options.grants ?? [] };
    });
    const agentList = vi.fn(async () => ({ ok: true, agents: options.agents ?? [], agentsWithoutRoutine: [] }));
    const client = {
      agent: {
        list: agentList,
        secret: { list: secretList, providerWrite: write },
      },
    } as unknown as Pick<GeneratedClient, 'agent'>;
    return { client, write, secretList, agentList };
  }

  it('uses the DeepSeek provider mapping through the generated client', async () => {
    const { client, write, secretList } = connectedClient();
    const access = vi.fn(() => ({ available: true as const }));
    const actions = createConnectedModelKeyActions(client, access);

    await expect(actions.saveModelKey?.('synthetic-key')).resolves.toEqual({ status: 'saved' });

    expect(secretList).toHaveBeenCalledOnce();
    expect(write).toHaveBeenCalledOnce();
    expect(write).toHaveBeenCalledWith({
      ...DEEPSEEK_MODEL_KEY_TARGET,
      value: '',
    }, undefined);
    expect(access).toHaveBeenCalledWith('agent.secret.list', {});
    expect(access).toHaveBeenCalledWith('agent.secret.provider-write', DEEPSEEK_MODEL_KEY_TARGET);
  });

  it('exposes only saveModelKey without Squire setup lookup or localStorage identity access', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem');
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const { client, agentList } = connectedClient();
    const actions = createConnectedModelKeyActions(client);

    expect('getSetupState' in actions).toBe(false);
    expect('prepareSquire' in actions).toBe(false);
    await expect(actions.saveModelKey?.('synthetic-key')).resolves.toEqual({ status: 'saved' });
    expect(agentList).not.toHaveBeenCalled();
    expect(getItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
  });

  it('refuses to overwrite a target referenced by any existing grant', async () => {
    const { client, write } = connectedClient({
      grants: [{
        actor: 'other-agent', grantId: 'other-grant', source: 'user-provider',
        entryId: DEEPSEEK_MODEL_KEY_TARGET.entryId,
        fieldName: DEEPSEEK_MODEL_KEY_TARGET.fieldName,
        environmentNames: ['OTHER_KEY'], revision: 4, ownerActor: null, ownerEntityToken: null,
      }],
    });
    const actions = createConnectedModelKeyActions(client);

    await expect(actions.saveModelKey?.('replacement')).resolves.toMatchObject({
      status: 'failed',
      message: expect.stringMatching(/静默覆盖/),
    });
    expect(write).not.toHaveBeenCalled();
  });

  it('does not write when the grant inventory cannot be confirmed', async () => {
    const { client, write } = connectedClient({ listFailure: new Error('lost') });
    const actions = createConnectedModelKeyActions(client);

    await expect(actions.saveModelKey?.('synthetic-key')).resolves.toMatchObject({ status: 'failed' });
    expect(write).not.toHaveBeenCalled();
  });
});

describe('A7 account port separation', () => {
  it('leaves account actions to the account port, not the model-key adapter', async () => {
    const write = vi.fn();
    const client = { agent: { secret: { providerWrite: write } } } as unknown as Pick<GeneratedClient, 'agent'>;
    const actions = createConnectedModelKeyActions(client);
    expect(actions.getAccountState).toBeUndefined();
    expect(actions.logoutAccount).toBeUndefined();
    expect(actions.authenticateAccount).toBeUndefined();
    expect(actions.initializeDevice).toBeUndefined();
    expect(write).not.toHaveBeenCalled();
  });
});
