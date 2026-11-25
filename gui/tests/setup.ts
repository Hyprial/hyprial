/**
 * Test-environment wiring only: unit tests exercise fixture chat through App,
 * so the mock port factory is registered here. Product compositions register
 * their own fixture factory in the dev composition (never in product code).
 */
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
import { createAgentChatMock } from '../src/dev/mock/agent-chat';
import { registerAgentChatMockPort } from '../src/dev/mock/use-contract-runtime';

registerAgentChatMockPort(createAgentChatMock);

afterEach(() => {
  cleanup();
});
