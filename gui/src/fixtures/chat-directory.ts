/** Shared opt-in chat directory; no real host or model capability is implied. */
export const CHAT_FIXTURE_TARGET = 'agent:fixture@example.test:fixture-node:squire';
export const CHAT_FIXTURE_DIRECTORY = [
  { targetUri: CHAT_FIXTURE_TARGET, displayName: 'Fixture Squire', status: 'online' },
  { targetUri: 'agent:fixture@example.test:fixture-node:researcher', displayName: 'Fixture Researcher', status: 'online' },
  { targetUri: 'agent:fixture@example.test:fixture-node:writer', displayName: 'Fixture Writer', status: 'online' },
  { targetUri: 'agent:fixture@example.test:fixture-node:reviewer', displayName: 'Fixture Reviewer', status: 'offline' },
] as const;
export const CHAT_FIXTURE_DIRECTORY_TARGET = CHAT_FIXTURE_DIRECTORY[0];
