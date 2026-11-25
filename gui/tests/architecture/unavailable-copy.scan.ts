/*
 * Decision record (scanner configuration, PR 1f/F0, 2026-11-14):
 * - This file is the whole configuration of the vitest scanner in
 *   unavailable-copy.test.ts: the vocabulary, the exclusions and the scan
 *   root, plus the ledger module that the scan is checked against. The test
 *   reads them from here only.
 * - scripts/ci/check_gui_unavailable_copy.mjs parses this file with the
 *   TypeScript compiler at HEAD and at the base ref and fails when the
 *   scanner was narrowed: a removed vocabulary term, a changed boundary or
 *   machine-token pattern, a new skipped file or directory, a removed
 *   scanned extension, a changed scan root, or a retargeted ledger or scan
 *   import. Added vocabulary terms, removed skips and added extensions pass.
 * - A legitimate change that the comparison cannot classify needs the
 *   reviewed override: the sha256 of the configuration (printed by the
 *   script with --print-scanner-digest) is pinned in the script constant
 *   REVIEWED_SCANNER_SHA256, so changing the scanner always edits the
 *   reviewed script in the same PR.
 * - Every export must be a literal constant (string, array of strings) so
 *   the script can read it without executing this file.
 * - The ledger is imported here and re-exported as LEDGER, so the module
 *   the gate checks against is visible as an import of this file.
 */
import * as ledger from './unavailable-copy.allow';

export const LEDGER = ledger;

export const SCAN_ROOT = 'src';

export const SOURCE_EXTENSIONS = ['.ts', '.tsx', '.mts', '.js', '.mjs', '.cjs', '.css'] as const;

export const SKIPPED_DIRECTORIES = [] as const;

export const SKIPPED_FILES = ['client/generated/index.ts', 'client/generated/types.ts'] as const;

export const VOCABULARY_TERMS = [
  '不可用',
  '暂不',
  '暂未',
  '尚未(?:开放|接入|接通)',
  '未接(?:通|线|入)',
  '未提供',
  '不支持',
  '未实现',
  '敬请期待',
  '开发中',
  '无法使用',
  'not implemented',
  'not wired',
  'not available',
  'coming soon',
] as const;

export const ENGLISH_TERMS = ['unavailable', 'unsupported'] as const;

export const ENGLISH_BOUNDARY_BEFORE = '(?<![A-Za-z0-9_])';

export const ENGLISH_BOUNDARY_AFTER = '(?![A-Za-z0-9_])';

export const MACHINE_TOKEN = '^[a-z0-9]+(?:[-_.:][a-z0-9]+)*$';
