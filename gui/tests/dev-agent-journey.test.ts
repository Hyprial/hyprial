import { describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/react';
import { createAgentJourney } from '../src/dev/journey';
import { agentStatusCounts, platformBindingCounts } from '../src/pages/start/start-summary';
import { keyCompatibleWithHarness } from '../src/features/agents/impl/AgentForm';

async function configuredAgent(journey: ReturnType<typeof createAgentJourney>, name = 'squire') {
  await journey.sharedCredentials.addKey('deepseek', '模型 Key', 'synthetic-key');
  const entries = await journey.sharedCredentials.listKeys();
  await journey.agents.create({ name, preferredHarness: 'pi', provider: null, model: null });
  const agent = (await journey.agents.list()).find(row => row.actor === name)!;
  await journey.agents.allowProvider(agent.uri, 'deepseek', { keyId: entries[0].keyId });
  await journey.agents.start(name, { harness: 'pi', provider: 'deepseek', model: 'deepseek-flash' });
  return agent;
}

describe('one Agent journey state', () => {
  it.each(['claude', 'codex', 'pi', 'agy'])('opens a file chooser before importing %s Auth into the mock', async (harness) => {
    const journey = createAgentJourney();
    await journey.agents.create({ name: 'auth-agent', preferredHarness: harness, provider: null, model: null });
    const agent = (await journey.agents.list())[0];
    expect(agent.owner).toBe('preview@example.invalid');
    expect(agent.host).toBe('dev-local');
    expect(agent.cwd).toBe('/var/mock-agents/auth-agent/workspace');
    const pending = journey.agentProviderLoginFilePicker(harness);
    const input = document.querySelector('input[type="file"]');
    expect(input).not.toBeNull();
    expect((input as HTMLInputElement).multiple).toBe(harness === 'pi');
    const names = harness === 'claude' ? ['.credentials.json']
      : harness === 'codex' ? ['auth.json']
        : harness === 'agy' ? ['antigravity-oauth-token']
          : ['auth.json', 'models-store.json'];
    fireEvent.change(input!, { target: { files: names.map(name => new File(['{}'], name, { type: 'application/json' })) } });
    const files = await pending;
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(files.map(file => file.filename)).toEqual(names);
    const result = await journey.agents.loginNative(agent.uri, harness, files);
    expect(result.files).toHaveLength(files.length);
    await expect(journey.agents.loginNative(agent.uri, harness, files)).rejects.toMatchObject({ code: 'PROVIDER_CREDENTIAL_EXISTS' });
    const replaced = await journey.agents.loginNative(agent.uri, harness, files, { replace: true });
    expect(replaced.files.every(file => file.unchanged)).toBe(true);
    expect(await journey.sharedCredentials.listKeys()).toEqual([]);
    await journey.agents.start(agent.actor, { harness });
    expect((await journey.chatTargets('agent'))[0].status).toBe('online');
    await journey.agents.logoutNative(agent.uri, harness);
    expect((await journey.agents.loginNative(agent.uri, harness, files)).files.every(file => !file.replaced)).toBe(true);
  });
  it('does not infer runtime health from creation alone', async () => {
    const journey = createAgentJourney();
    await journey.agents.create({ name: 'new-agent', preferredHarness: 'pi', provider: null, model: null });
    const rows = await journey.agents.list();
    expect(rows[0].harness).toBeNull();
    expect(rows[0].runState.runtime).toBe('unknown');
    expect(agentStatusCounts(rows)).toEqual({ normal: null, abnormal: null });
  });
  it('keeps an explicitly chosen working directory across start and stop', async () => {
    const journey = createAgentJourney();
    await journey.agents.create({ name: 'project', preferredHarness: 'claude', provider: null, model: null, cwd: '/mock/project' });
    await journey.agents.start('project', { harness: 'claude' });
    await journey.agents.stop('project');
    expect((await journey.agents.list())[0].cwd).toBe('/mock/project');
  });
  it('does not expose unsupported Key deletion in the composed journey', () => {
    expect(createAgentJourney().sharedCredentials.deleteKey).toBeUndefined();
  });

  it('shares grants and revocation between credentials and Agent detail without deleting the Key', async () => {
    const journey = createAgentJourney();
    const agent = await configuredAgent(journey);
    let snapshot = await journey.sharedCredentials.listKeys();
    expect(snapshot[0].grants).toHaveLength(1);
    expect(snapshot[0].grants[0].agent).toBe(agent.uri);
    await journey.sharedCredentials.revokeGrant(agent.uri, 'deepseek');
    expect((await journey.agents.providerStatus(agent.uri))[0].state).toBe('revoked');
    expect(await journey.sharedCredentials.listKeys()).toEqual([{ ...snapshot[0], grants: [] }]);
    await journey.agents.allowProvider(agent.uri, 'deepseek', { keyId: snapshot[0].keyId, replace: true });
    snapshot = await journey.sharedCredentials.listKeys();
    expect(snapshot[0].grants).toHaveLength(1);
    await journey.agents.revokeProvider(agent.uri, 'deepseek');
    expect(await journey.sharedCredentials.listKeys()).toEqual([{ ...snapshot[0], grants: [] }]);
    expect((await journey.agents.providerStatus(agent.uri))[0].state).toBe('revoked');
    expect(agentStatusCounts(await journey.agents.list())).toEqual({ normal: 1, abnormal: 0 });
  });

  it('shares platform lifecycle and adapter bindings with all status readers', async () => {
    const journey = createAgentJourney();
    const agent = await configuredAgent(journey);
    const platforms = journey.platformIntegrations;
    await platforms.beginAdd!({ name: 'feishu-personal', provider: 'lark', mode: 'adopt', appId: 'cli_test', secret: 'synthetic' });
    expect(await journey.agentAdapterBindings.list()).toHaveLength(1);
    await journey.agentAdapterBindings.bind('feishu-personal', agent.uri);
    expect(platformBindingCounts(await platforms.list())).toEqual({ normal: 0, abnormal: 1 });
    expect((await platforms.removePreview!('feishu-personal')).boundAgent).toBe(agent.uri);
    const authorization = await platforms.beginAuthorization!('feishu-personal');
    await platforms.authorizationOperation!(authorization.operationId);
    await platforms.authorizationOperation!(authorization.operationId);
    await platforms.start('feishu-personal');
    expect(platformBindingCounts(await platforms.list())).toEqual({ normal: 1, abnormal: 0 });
    expect((await journey.agentAdapterBindings.list())[0]).toMatchObject({ online: true, processRunning: true, boundAgent: agent.uri });
    const routes = journey.agentAdapterBindings.routes.api!;
    const empty = await routes.list('feishu-personal');
    const [candidate] = await routes.candidates('feishu-personal');
    const saved = await routes.add('feishu-personal', 'main', candidate.chatId, true, empty.revision);
    expect(await routes.list('feishu-personal')).toMatchObject({ defaultRoute: 'main', routes: [{ name: 'main', nativeId: candidate.chatId }] });
    await expect(routes.remove('feishu-personal', 'main', true, empty.revision)).rejects.toThrow('配置已变化');
    await expect(routes.remove('feishu-personal', 'main', false, saved.adapter.revision)).rejects.toThrow('清除默认路由');
    await routes.remove('feishu-personal', 'main', true, saved.adapter.revision);
    expect((await routes.list('feishu-personal')).routes).toEqual([]);
    await platforms.beginAdd!({ name: 'second', provider: 'lark', mode: 'adopt', appId: 'cli_test2', secret: 'synthetic' });
    await journey.agentAdapterBindings.bind('second', agent.uri);
    expect((await journey.agentAdapterBindings.list()).map(row => [row.name, row.boundAgent])).toEqual([
      ['feishu-personal', agent.uri], ['second', agent.uri],
    ]);
    await platforms.stop('feishu-personal');
    expect(platformBindingCounts(await platforms.list())).toEqual({ normal: 0, abnormal: 2 });
    await journey.agentAdapterBindings.unbind('feishu-personal', agent.uri);
    expect(platformBindingCounts(await platforms.list())).toEqual({ normal: 0, abnormal: 1 });
    await platforms.remove!('feishu-personal', await platforms.removePreview!('feishu-personal'));
    await expect(routes.list('feishu-personal')).rejects.toThrow('平台接入不存在');
    expect((await journey.agentAdapterBindings.list()).map(row => row.name)).toEqual(['second']);
  });

  it('keeps model, directory, counts and chat names together across stop/restart/rename', async () => {
    const changed = vi.fn();
    const journey = createAgentJourney(changed);
    const agent = await configuredAgent(journey);
    expect(agentStatusCounts(await journey.agents.list())).toEqual({ normal: 1, abnormal: 0 });
    await journey.agents.stop(agent.actor);
    expect(agentStatusCounts(await journey.agents.list())).toEqual({ normal: 0, abnormal: 1 });
    await journey.agents.start(agent.actor, {});
    expect((await journey.agents.list())[0]).toMatchObject({ model: 'deepseek-flash', provider: 'deepseek', preferredHarness: 'pi' });
    const beforeRename = changed.mock.lastCall![0];
    await journey.agents.rename(agent.actor, '我的侍从');
    expect(changed.mock.lastCall![0]).not.toBe(beforeRename);
    expect((await journey.agents.list())[0].displayName).toBe('我的侍从');
    expect((await journey.chatTargets('agent')).map(row => row.targetUri)).toEqual([agent.uri]);
    expect((await journey.chatTargets('agent'))[0]).toMatchObject({ displayName: '我的侍从', status: 'online' });
    expect((await createAgentJourney().agents.list())).toEqual([]);
  });

  it.each([
    ['claude', 'hyprial', null], ['codex', 'deepseek', 'unsupported-model'],
    ['pi', 'deepseek', null],
  ])('rejects invalid %s/%s/%s mock starts without changing counts', async (harness, provider, model) => {
    const journey = createAgentJourney();
    await journey.agents.create({ name: 'invalid-model', preferredHarness: harness, provider, model: null });
    const before = await journey.agents.list();
    await expect(journey.agents.start('invalid-model', { harness, provider, model })).rejects.toThrow();
    expect(await journey.agents.list()).toEqual(before);
    expect(agentStatusCounts(await journey.agents.list())).toEqual(agentStatusCounts(before));
  });

  it('excludes custom endpoints, Codex shared OpenAI Key and unregistered Pi providers', () => {
    expect(keyCompatibleWithHarness('pi', 'hyprial')).toBe(false);
    expect(keyCompatibleWithHarness('claude', 'anthropic', 'https://example.invalid')).toBe(false);
    expect(keyCompatibleWithHarness('codex', 'openai', 'https://example.invalid')).toBe(false);
    expect(keyCompatibleWithHarness('pi', 'openai', 'https://example.invalid')).toBe(false);
    expect(keyCompatibleWithHarness('codex', 'openai')).toBe(false);
    expect(keyCompatibleWithHarness('pi', 'arbitrary-provider')).toBe(false);
    expect(keyCompatibleWithHarness('pi', 'kimi-coding')).toBe(true);
    expect(keyCompatibleWithHarness('claude', 'deepseek')).toBe(true);
  });
});


describe('Agent deletion in the isolated journey', () => {
  it('removes the Agent, grants, native files and adapter bindings while keeping shared Keys and app configuration', async () => {
    const changed = vi.fn();
    const journey = createAgentJourney(changed);
    const agent = await configuredAgent(journey);
    await configuredAgent(journey, 'keep');
    await journey.agents.loginNative(agent.uri, 'pi', [
      { filename: 'auth.json', contentBase64: 'e30=' },
      { filename: 'models-store.json', contentBase64: 'e30=' },
    ]);
    await journey.platformIntegrations.beginAdd!({ name: 'feishu-personal', provider: 'lark', mode: 'adopt', appId: 'cli_test', secret: 'synthetic' });
    await journey.agentAdapterBindings.bind('feishu-personal', agent.uri);
    expect(await journey.agentDelete(agent.actor)).toEqual({ kind: 'deleted', agentId: agent.actor });
    expect((await journey.agents.list()).map(row => row.actor)).toEqual(['keep']);
    expect((await journey.chatTargets('agent')).some(row => row.targetUri === agent.uri)).toBe(false);
    expect(agentStatusCounts(await journey.agents.list())).toEqual({ normal: 1, abnormal: 0 });
    expect(changed.mock.lastCall![0]).toHaveLength(1);
    const credentials = await journey.sharedCredentials.listKeys();
    expect(credentials.length).toBeGreaterThan(0);
    expect(credentials.flatMap(entry => entry.grants).some(grant => grant.agent === agent.uri)).toBe(false);
    expect((await journey.platformIntegrations.list())[0]).toMatchObject({ name: 'feishu-personal', boundAgent: null });
    expect(await journey.agentDelete(agent.actor)).toMatchObject({ kind: 'failed' });
    await journey.agents.create({ name: agent.actor, preferredHarness: 'pi', provider: null, model: null });
    expect(await journey.agents.providerStatus(agent.uri)).toEqual([]);
    await expect(journey.agents.loginNative(agent.uri, 'pi', [
      { filename: 'auth.json', contentBase64: 'e30=' },
      { filename: 'models-store.json', contentBase64: 'e30=' },
    ])).resolves.toBeDefined();
  });
});
