/**
 * Decision record (ordinary provider login file source, 2026-11-05)
- The ordinary browser entry has no trusted host path reader. The only source
  this module accepts is an explicit user file selection from a temporary file
  input created during the existing login click gesture.
- A cancel or empty selection rejects before returning documents. The caller's
  existing login flow records that as not dispatched; this module never calls
  the generated client, a fixture, a host path, storage or a log.
- The current backend provider credential sets are mirrored only for early
  count/name refusal. `agent.provider.login` remains authoritative for actor,
  provider, filename, content and installation semantics.
- The request-http backend accepts at most 262144 request-body bytes. This module
  reserves 16384 bytes for actor/provider/JSON framing and validates the base64
  expansion before reading files, so an oversized selection cannot be dispatched.
- File bytes exist only in the pending promise and FileReader. The input value is
  cleared and the element removed as soon as selection or cancellation settles.
 */

import type { ProviderLoginFile } from './api';

export const PROVIDER_LOGIN_MAX_REQUEST_BYTES = 262_144;
export const PROVIDER_LOGIN_ENVELOPE_RESERVE_BYTES = 16_384;
export const PROVIDER_LOGIN_MAX_PAYLOAD_BYTES =
  PROVIDER_LOGIN_MAX_REQUEST_BYTES - PROVIDER_LOGIN_ENVELOPE_RESERVE_BYTES;
export const PROVIDER_LOGIN_MAX_FILES = 2;

const PROVIDER_FILES: Readonly<Record<string, readonly string[]>> = Object.freeze({
  claude: Object.freeze(['.credentials.json']),
  codex: Object.freeze(['auth.json']),
  pi: Object.freeze(['auth.json', 'models-store.json']),
  agy: Object.freeze(['antigravity-oauth-token']),
});

export function providerLoginFileNames(provider: string): readonly string[] {
  return PROVIDER_FILES[provider] ?? [];
}

const PROVIDER_FILE_DIRECTORIES: Readonly<Record<string, string>> = Object.freeze({
  claude: '~/.claude',
  codex: '~/.codex',
  pi: '~/.pi/agent',
  agy: '~/.gemini/antigravity-cli',
});

export function providerLoginFileLocation(provider: string): string | null {
  const directory = PROVIDER_FILE_DIRECTORIES[provider];
  return directory === undefined
    ? null
    : `常见位置：${providerLoginFileNames(provider).map(name => `${directory}/${name}`).join('、')}。如果使用自定义配置目录，请到该目录查找并手动选择文件。`;
}

export function providerLoginFileMessage(error: unknown, provider: string): string {
  const code = error instanceof Error ? (error as { readonly code?: string }).code : undefined;
  if (code === 'LOGIN_FILES_CANCELLED') return '未选择登录文件';
  if (code === 'LOGIN_FILES_INVALID_NAME') {
    return `文件名应为 ${providerLoginFileNames(provider).join('、')}`;
  }
  if (code === 'LOGIN_FILES_WRONG_COUNT' || code === 'LOGIN_FILES_TOO_MANY') {
    return `请选择 ${providerLoginFileNames(provider).join('、')}`;
  }
  if (code === 'LOGIN_FILES_TOO_LARGE') return '登录文件过大';
  if (code === 'LOGIN_FILES_EMPTY_CONTENT') return '登录文件为空';
  return '登录文件读取失败';
}

export type ProviderLoginFileErrorCode =
  | 'LOGIN_FILES_BUSY'
  | 'LOGIN_FILES_CANCELLED'
  | 'LOGIN_FILES_EMPTY_CONTENT'
  | 'LOGIN_FILES_INPUT_UNAVAILABLE'
  | 'LOGIN_FILES_INVALID_CONTEXT'
  | 'LOGIN_FILES_INVALID_NAME'
  | 'LOGIN_FILES_READ_FAILED'
  | 'LOGIN_FILES_TOO_LARGE'
  | 'LOGIN_FILES_TOO_MANY'
  | 'LOGIN_FILES_UNSUPPORTED_PROVIDER'
  | 'LOGIN_FILES_WRONG_COUNT';

export class ProviderLoginFileError extends Error {
  readonly code: ProviderLoginFileErrorCode;

  constructor(code: ProviderLoginFileErrorCode, message: string) {
    super(message);
    this.name = 'ProviderLoginFileError';
    this.code = code;
  }
}

export interface ProviderLoginFilePickerOptions {
  readonly document?: Document;
}

export type ProviderLoginFiles = (
  actor: string,
  provider: string,
) => Promise<readonly ProviderLoginFile[]>;

export type ProviderLoginFilePicker = (
  provider: string,
) => Promise<readonly ProviderLoginFile[]>;

function base64Length(bytes: number): number {
  return 4 * Math.ceil(bytes / 3);
}

function validateSelection(
  provider: string,
  files: readonly File[],
): readonly string[] {
  if (!provider) {
    throw new ProviderLoginFileError('LOGIN_FILES_INVALID_CONTEXT', 'A provider is required.');
  }
  const expected = PROVIDER_FILES[provider];
  if (expected === undefined) {
    throw new ProviderLoginFileError('LOGIN_FILES_UNSUPPORTED_PROVIDER', 'The selected provider is not supported.');
  }
  if (files.length === 0) {
    throw new ProviderLoginFileError('LOGIN_FILES_CANCELLED', 'No credential files were selected.');
  }
  if (files.length > PROVIDER_LOGIN_MAX_FILES) {
    throw new ProviderLoginFileError('LOGIN_FILES_TOO_MANY', 'Select no more than two credential files.');
  }
  if (files.length !== expected.length) {
    throw new ProviderLoginFileError('LOGIN_FILES_WRONG_COUNT', 'Select exactly the provider credential files.');
  }
  const allowed = new Set(expected);
  const names = files.map(file => file.name);
  if (names.some(name => !allowed.has(name)) || new Set(names).size !== names.length) {
    throw new ProviderLoginFileError('LOGIN_FILES_INVALID_NAME', 'The selected credential filenames do not match the provider.');
  }
  if (files.some(file => file.size === 0)) {
    throw new ProviderLoginFileError('LOGIN_FILES_EMPTY_CONTENT', 'Credential files must not be empty.');
  }
  if (files.some(file => !Number.isSafeInteger(file.size) || file.size < 0)) {
    throw new ProviderLoginFileError('LOGIN_FILES_READ_FAILED', 'Credential file size is unavailable.');
  }
  const encodedBytes = files.reduce((total, file) => total + base64Length(file.size), 0);
  if (encodedBytes > PROVIDER_LOGIN_MAX_PAYLOAD_BYTES) {
    throw new ProviderLoginFileError('LOGIN_FILES_TOO_LARGE', 'The selected credential files exceed the request budget.');
  }
  return names;
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const failed = () => reject(new ProviderLoginFileError(
      'LOGIN_FILES_READ_FAILED',
      'A selected credential file could not be read.',
    ));
    reader.onerror = failed;
    reader.onabort = failed;
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== 'string') {
        failed();
        return;
      }
      const marker = result.indexOf(';base64,');
      if (marker < 0) {
        failed();
        return;
      }
      resolve(result.slice(marker + ';base64,'.length));
    };
    try {
      reader.readAsDataURL(file);
    } catch {
      failed();
    }
  });
}

export function createProviderLoginFilePicker(
  options: ProviderLoginFilePickerOptions = {},
): ProviderLoginFilePicker {
  let active = false;
  return async (provider) => {
    if (active) {
      throw new ProviderLoginFileError('LOGIN_FILES_BUSY', 'Another credential file selection is already open.');
    }
    const document = options.document ?? globalThis.document;
    if (!document?.body) {
      throw new ProviderLoginFileError('LOGIN_FILES_INPUT_UNAVAILABLE', 'File selection is available only in the browser.');
    }
    active = true;
    let input: HTMLInputElement;
    try {
      input = document.createElement('input');
      input.type = 'file';
      if (provider !== 'agy') input.accept = '.json,application/json';
      input.multiple = providerLoginFileNames(provider).length > 1;
      input.hidden = true;
      input.tabIndex = -1;
      document.body.append(input);
    } catch {
      active = false;
      throw new ProviderLoginFileError('LOGIN_FILES_INPUT_UNAVAILABLE', 'The credential file chooser could not be opened.');
    }

    return new Promise<readonly ProviderLoginFile[]>((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        input.removeEventListener('change', changed);
        input.removeEventListener('cancel', cancelled);
        input.value = '';
        input.remove();
        active = false;
      };
      const fail = (error: ProviderLoginFileError) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      };
      const finish = (files: readonly ProviderLoginFile[]) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(files);
      };
      const cancelled = () => fail(new ProviderLoginFileError(
        'LOGIN_FILES_CANCELLED',
        'Credential file selection was cancelled.',
      ));
      const changed = () => {
        const files = Array.from(input.files ?? []);
        input.value = '';
        try {
          validateSelection(provider, files);
        } catch (error) {
          fail(error instanceof ProviderLoginFileError ? error : new ProviderLoginFileError(
            'LOGIN_FILES_READ_FAILED',
            'Credential file selection could not be validated.',
          ));
          return;
        }
        void Promise.all(files.map(async file => Object.freeze({
          filename: file.name,
          contentBase64: await readBase64(file),
        }))).then(
          result => finish(Object.freeze(result)),
          error => fail(error instanceof ProviderLoginFileError ? error : new ProviderLoginFileError(
            'LOGIN_FILES_READ_FAILED',
            'Credential files could not be read.',
          )),
        );
      };
      input.addEventListener('change', changed);
      input.addEventListener('cancel', cancelled);
      try {
        input.click();
      } catch {
        fail(new ProviderLoginFileError(
          'LOGIN_FILES_INPUT_UNAVAILABLE',
          'The credential file chooser could not be opened.',
        ));
      }
    });
  };
}

export function createProviderLoginFiles(
  options: ProviderLoginFilePickerOptions = {},
): ProviderLoginFiles {
  const pick = createProviderLoginFilePicker(options);
  return async (actor, provider) => {
    if (!actor) {
      throw new ProviderLoginFileError(
        'LOGIN_FILES_INVALID_CONTEXT',
        'A target agent is required.',
      );
    }
    return pick(provider);
  };
}
