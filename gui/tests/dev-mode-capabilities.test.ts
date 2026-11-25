import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { syntheticProviderLoginFiles } from '../src/dev/mock/agent-provider';
import { UNWIRED_SOURCE, type OrgDomainSource } from '../src/features/org';
import type { AppAction } from '../src/state/types';

const noopDispatch = (_action: AppAction) => undefined;

describe('fixture capabilities adapter', () => {
  it('builds a fixtureCapabilitiesFactory that injects firstRunWizardEnabled verbatim', async () => {
    const { createFixtureCapabilities } = await import('../src/dev/first-run-capabilities');
    const enabledFactory = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: true,
    });
    const disabledFactory = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: false,
    });
    expect(typeof enabledFactory).toBe('function');
    const enabled = enabledFactory(noopDispatch);
    const disabled = disabledFactory(noopDispatch);
    expect(enabled.firstRunWizardEnabled).toBe(true);
    expect(disabled.firstRunWizardEnabled).toBe(false);
    expect(Object.isFrozen(enabled)).toBe(true);
  });

  it('assembles fixture first-run actions and the refused-login scenario from scenario data', async () => {
    const { createFixtureCapabilities } = await import('../src/dev/first-run-capabilities');
    const unavailable = createFixtureCapabilities({
      firstRunMode: 'unavailable',
      firstRunWizardEnabled: true,
    })(noopDispatch);
    await expect(
      unavailable.firstRun.authenticateAccount!({ account: '', token: '' }, 'login'),
    ).resolves.toMatchObject({ status: 'unavailable', message: '模拟：账号服务无响应。' });

    const gated = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: false,
      accountLoginScenario: 'disabled',
    })(noopDispatch);
    await expect(gated.firstRun.authenticateAccount!({ account: '', token: '' }, 'login')).resolves
      .toEqual({ status: 'unavailable', refusal: { code: 'METHOD_NOT_FOUND', source: 'auth.login.begin' } });
    expect('accountLoginDisabled' in gated).toBe(false);

    const open = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: false,
      accountLoginScenario: 'success',
    })(noopDispatch);
    await expect(open.firstRun.authenticateAccount!({ account: '', token: '' }, 'login')).resolves
      .toMatchObject({ status: 'authenticated' });
  });

  it('supplies provider login files only when the scenario asks for it', async () => {
    const { createFixtureCapabilities } = await import('../src/dev/first-run-capabilities');
    const withoutProvider = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: true,
    })(noopDispatch);
    expect(withoutProvider.agentProviderLoginFiles).toBeUndefined();

    const withProvider = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: true,
      agentProviderScenario: 'success',
    })(noopDispatch);
    const files = await withProvider.agentProviderLoginFiles?.('actor', 'deepseek');
    expect(files).toEqual(syntheticProviderLoginFiles());
  });

  it('marks an injected org source as an isolated fixture source, never a host source', async () => {
    const { createFixtureCapabilities } = await import('../src/dev/first-run-capabilities');
    const orgSource: OrgDomainSource = UNWIRED_SOURCE;
    const caps = createFixtureCapabilities({
      firstRunMode: 'complete',
      firstRunWizardEnabled: true,
      orgSource,
    })(noopDispatch);
    expect(caps.orgSource).toBe(orgSource);
    expect(caps.orgSourceKind).toBe('isolated');
    expect(caps.orgSourceKind).not.toBe('host');
  });
});

describe('fixture source label ownership', () => {
  it('the adapter keeps unknown and failure semantics for every scenario', async () => {
    const { createAgentChatMock } = await import('../src/dev/mock/agent-chat');
    await expect(
      createAgentChatMock('unknown').call('sessions.send', {
        sessionId: 'fixture-session',
        request: { text: '查询', idempotencyKey: 'lost-key' },
      }),
    ).rejects.toThrow();
    await expect(
      createAgentChatMock('failure').call('sessions.send', {
        sessionId: 'fixture-session',
        request: { text: '查询', idempotencyKey: 'fail-key' },
      }),
    ).rejects.toThrow();
  });
});

describe('WP3 scope carries no build-mode product branches', () => {
  const scopedFiles = [
    '../src/dev/session/session-types.ts',
    '../src/dev/session/initial-session.ts',
    '../src/dev/session/session-reducer.ts',
    '../src/dev/session/session-selectors.ts',
    '../src/dev/session/linked-gateways.ts',
    '../src/dev/chat-scenarios.ts',
    '../src/dev/j2-scenarios.ts',
    '../src/dev/machine-scenarios.ts',
    '../src/dev/org-scenarios.ts',
    '../src/dev/remaining-scenarios.ts',
    '../src/dev/topology-scenarios.ts',
    '../src/dev/first-run-capabilities.ts',
    '../src/dev/legacy-org-source.ts',
    '../src/dev/fixtures/demo.ts',
    '../src/dev/org/OrgDevApp.tsx',
    '../src/dev/org/scenarios.ts',
    '../src/dev/org/fixture-data.ts',
    '../src/dev/org/fixture-gateway.ts',
    '../src/dev/mock/agent-chat.ts',
    '../src/dev/mock/agent-provider.ts',
  ];

  it.each(scopedFiles)('%s has no build-mode product branch', (relative) => {
    const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
    expect(source).not.toMatch(/import\.meta\.env\.DEV|__HYPRIAL_DEV_/);
  });

  it('org dev composition renders the product tree without dev navigation labels', () => {
    const orgDevAppSource = '../src/dev/org/OrgDevApp.tsx';
    const source = readFileSync(new URL(orgDevAppSource, import.meta.url), 'utf8');
    expect(source).not.toContain('DEV_MODULE_BY_PAGE');
    expect(source).not.toMatch(/devLabels=\{/);
    expect(source).not.toMatch(/<App[^>]*agentProviderScenario=/s);
    expect(source).not.toMatch(/<App[^>]*accountLoginScenario=/s);
    expect(source).not.toMatch(/<App[^>]*agentChatScenario=/s);
    expect(source).not.toMatch(/<App[^>]*fixtureBaseline=/s);
    expect(source).not.toMatch(/<App[^>]*fixtureCapabilitiesFactory=/s);
    expect(source).not.toContain('AGENT_CHAT_FIXTURE_SOURCE_LABEL');
  });
});
