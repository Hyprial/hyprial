import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAgentChatMock } from '../../src/dev/mock/agent-chat';
import { registerAgentChatMockPort, useChatContractRuntime } from '../../src/dev/mock/use-contract-runtime';

const ISOLATED_LABEL = '隔离契约 · Agent chat host · 不连接真实服务';

registerAgentChatMockPort(createAgentChatMock);

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('chat contract runtime is scenario input driven', () => {
  it('creates the contract runtime from an explicit scenario in production mode', () => {
    vi.stubEnv('DEV', false);
    const { result } = renderHook(() => useChatContractRuntime('success'));
    expect(result.current).not.toBeNull();
    expect(result.current?.status).toBe('ready');
    expect(result.current?.sourceLabel).toBe(ISOLATED_LABEL);
  });

  it('creates the same runtime shape in dev and production mode', () => {
    vi.stubEnv('DEV', false);
    const production = renderHook(() => useChatContractRuntime('empty')).result.current;
    vi.unstubAllEnvs();
    vi.stubEnv('DEV', true);
    const development = renderHook(() => useChatContractRuntime('empty')).result.current;
    expect(development?.status).toBe(production?.status);
    expect(development?.sourceLabel).toBe(production?.sourceLabel);
  });

  it('creates no runtime without a scenario in production mode', () => {
    vi.stubEnv('DEV', false);
    const { result } = renderHook(() => useChatContractRuntime());
    expect(result.current).toBeNull();
  });
});
