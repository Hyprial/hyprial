import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';

/*
 * Test-only CJK font setup for the isolated Linux browser acceptance.
 *
 * Decisions (2026-11-15, PR 1f/F6):
 * - Lookup order: DEV01_CJK_FONT, then the system font fontconfig matches for
 *   zh-cn (fc-match), then common Linux package paths (WenQuanYi, Noto CJK,
 *   Droid). No developer-machine path is listed.
 * - fc-match always answers with some font, CJK or not, so its answer counts
 *   only when `fc-list ':lang=zh' file` lists the same file. A DEV01_CJK_FONT
 *   that does not exist is an error, never silently replaced. The selection is
 *   the pure `selectCjkFont`, unit-tested with injected command output.
 * - The gate-gui workflow installs fonts-wqy-zenhei and fontconfig in an
 *   explicit step and asserts `fc-list ':lang=zh'` is non-empty before tests.
 * - A dedicated fontconfig instance points at the directory holding the chosen
 *   font; nothing is installed and no system permission or global font cache
 *   is changed. Other platforms keep their normal font stack.
 * - On Linux a missing font is an error naming what was checked, never a skip.
 */
const COMMON_FONT_PATHS = [
  '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
  '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
  '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
  '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',
  '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
  '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
  '/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc',
  '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
  '/mnt/c/Windows/Fonts/msyh.ttc',
  '/mnt/c/Windows/Fonts/simhei.ttf',
];

function runFontconfig(command, args) {
  try {
    return execFileSync(command, args, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 10_000,
    });
  } catch {
    return '';
  }
}

export function parseFcList(output) {
  return output
    .split('\n')
    .map((line) => line.trim().replace(/:$/, '').trim())
    .filter(Boolean);
}

export function selectCjkFont({
  envFont,
  matched,
  zhFonts,
  commonPaths = COMMON_FONT_PATHS,
  exists = existsSync,
}) {
  if (envFont) {
    if (!exists(envFont)) {
      throw new Error(`DEV01_CJK_FONT does not exist: ${envFont}`);
    }
    return { font: envFont, checked: [envFont] };
  }
  const zh = new Set(zhFonts);
  const fromFontconfig = matched && zh.has(matched) ? [matched] : [];
  const checked = [...fromFontconfig, ...commonPaths];
  return { font: checked.find((candidate) => exists(candidate)) ?? null, checked };
}

function systemSelection() {
  return selectCjkFont({
    envFont: process.env.DEV01_CJK_FONT,
    matched: runFontconfig('fc-match', ['-f', '%{file}', ':lang=zh-cn']).trim(),
    zhFonts: parseFcList(runFontconfig('fc-list', [':lang=zh', 'file'])),
  });
}

function xmlEscape(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

export function findCjkFont() {
  return systemSelection().font;
}

export async function createCjkFontEnvironment() {
  const { font: fontPath, checked } = systemSelection();
  if (!fontPath) {
    if (process.platform === 'linux') {
      throw new Error(
        `No CJK font found for browser screenshots. Checked: ${checked.join(', ')}`,
      );
    }
    return {
      env: process.env,
      fontPath: null,
      fontConfigPath: null,
      isolated: false,
    };
  }

  const root = await mkdtemp(resolve(tmpdir(), 'dev01-visual-font-'));
  const cacheDir = resolve(root, 'cache');
  const fontConfigPath = resolve(root, 'fonts.conf');
  await mkdir(cacheDir, { recursive: true });
  await writeFile(
    fontConfigPath,
    [
      '<?xml version="1.0"?>',
      '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">',
      '<fontconfig>',
      `  <dir>${xmlEscape(dirname(fontPath))}</dir>`,
      `  <cachedir>${xmlEscape(cacheDir)}</cachedir>`,
      '  <match target="pattern">',
      '    <edit name="family" mode="prepend" binding="strong">',
      '      <string>Noto Sans SC</string>',
      '    </edit>',
      '  </match>',
      '</fontconfig>',
      '',
    ].join('\n'),
    'utf8',
  );

  return {
    env: {
      ...process.env,
      FONTCONFIG_FILE: fontConfigPath,
      FONTCONFIG_PATH: root,
      XDG_CACHE_HOME: cacheDir,
    },
    fontPath,
    fontConfigPath,
    isolated: true,
  };
}
