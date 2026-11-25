import { Buffer } from 'node:buffer';
import {
  PROVIDER_LOGIN_MAX_FILES,
  PROVIDER_LOGIN_MAX_PAYLOAD_BYTES,
  ProviderLoginFileError,
  providerLoginFileMessage,
  providerLoginFileLocation,
  createProviderLoginFiles,
} from '../src/features/agent-provider/impl/login-files';

it.each([
  ['claude', '~/.claude/.credentials.json'],
  ['codex', '~/.codex/auth.json'],
  ['pi', '~/.pi/agent/auth.json、~/.pi/agent/models-store.json'],
  ['agy', '~/.gemini/antigravity-cli/antigravity-oauth-token'],
])('shows the usual %s login file location', (provider, location) => {
  expect(providerLoginFileLocation(provider)).toContain(location);
});

Element.prototype.scrollIntoView = vi.fn();

function inputAfterPick(): HTMLInputElement {
  const input = document.body.querySelector('input[type="file"]');
  if (!(input instanceof HTMLInputElement)) throw new Error('file input was not opened');
  return input;
}

function selectFiles(input: HTMLInputElement, files: readonly File[]): void {
  Object.defineProperty(input, 'files', { configurable: true, value: files });
  input.dispatchEvent(new Event('change'));
}

function fakeFile(name: string, size: number): File {
  return { name, size } as File;
}

async function rejection(promise: Promise<unknown>): Promise<ProviderLoginFileError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ProviderLoginFileError) return error;
    throw error;
  }
  throw new Error('expected ProviderLoginFileError');
}

afterEach(() => {
  document.body.replaceChildren();
  localStorage.clear();
  sessionStorage.clear();
});

it('opens one explicit multiple file input and returns base64 documents without retaining them', async () => {
  const pick = createProviderLoginFiles();
  const pending = pick('agent:owner:node:worker', 'pi');
  const input = inputAfterPick();
  expect(input.multiple).toBe(true);
  expect(input.hidden).toBe(true);
  const files = [
    new File(['credential bytes'], 'auth.json', { type: 'application/json' }),
    new File(['model bytes'], 'models-store.json', { type: 'application/json' }),
  ];
  selectFiles(input, files);

  await expect(pending).resolves.toEqual([
    Object.freeze({ filename: 'auth.json', contentBase64: Buffer.from('credential bytes').toString('base64') }),
    Object.freeze({ filename: 'models-store.json', contentBase64: Buffer.from('model bytes').toString('base64') }),
  ]);
  expect(document.body.querySelector('input[type="file"]')).toBeNull();
  expect(input.value).toBe('');
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});

it('treats cancel and an empty selection as explicit no-file outcomes', async () => {
  const pick = createProviderLoginFiles();
  const cancelled = pick('agent:owner:node:worker', 'claude');
  inputAfterPick().dispatchEvent(new Event('cancel'));
  await expect(rejection(cancelled)).resolves.toMatchObject({ code: 'LOGIN_FILES_CANCELLED' });

  const empty = pick('agent:owner:node:worker', 'claude');
  selectFiles(inputAfterPick(), []);
  await expect(rejection(empty)).resolves.toMatchObject({ code: 'LOGIN_FILES_CANCELLED' });
});

it('rejects count, empty content, unsafe names, and the backend request budget before reading', async () => {
  const pick = createProviderLoginFiles();

  const tooMany = pick('agent:owner:node:worker', 'pi');
  selectFiles(inputAfterPick(), Array.from(
    { length: PROVIDER_LOGIN_MAX_FILES + 1 },
    (_, index) => fakeFile(`file-${index}.json`, 1),
  ));
  await expect(rejection(tooMany)).resolves.toMatchObject({ code: 'LOGIN_FILES_TOO_MANY' });

  const emptyFile = pick('agent:owner:node:worker', 'claude');
  selectFiles(inputAfterPick(), [fakeFile('.credentials.json', 0)]);
  await expect(rejection(emptyFile)).resolves.toMatchObject({ code: 'LOGIN_FILES_EMPTY_CONTENT' });

  const unsafe = pick('agent:owner:node:worker', 'claude');
  selectFiles(inputAfterPick(), [fakeFile('../credentials.json', 1)]);
  await expect(rejection(unsafe)).resolves.toMatchObject({ code: 'LOGIN_FILES_INVALID_NAME' });

  const oversized = pick('agent:owner:node:worker', 'pi');
  const half = Math.floor(PROVIDER_LOGIN_MAX_PAYLOAD_BYTES * 3 / 4 / 2) + 1;
  selectFiles(inputAfterPick(), [fakeFile('auth.json', half), fakeFile('models-store.json', half)]);
  await expect(rejection(oversized)).resolves.toMatchObject({ code: 'LOGIN_FILES_TOO_LARGE' });
});

it('explains a filename mismatch without confusing it with credential validation', async () => {
  const pending = createProviderLoginFiles()('worker', 'claude');
  selectFiles(inputAfterPick(), [new File(['{}'], 'auth.credentials.json')]);
  const error = await rejection(pending);
  expect(error.code).toBe('LOGIN_FILES_INVALID_NAME');
  expect(providerLoginFileMessage(error, 'claude')).toBe('文件名应为 .credentials.json');
});
