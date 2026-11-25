// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { parseFcList, selectCjkFont } from './dev01-visual-font.mjs';

/*
 * Decision (2026-11-15, PR 1f/F6): the selection logic runs on injected
 * fontconfig output, so this file needs no fontconfig and no font on the host.
 * The case that motivated it: with only Latin fonts installed fc-match still
 * answers (LiberationMono) and used to be accepted as the CJK font.
 */
const present = (...files) => (file) => files.includes(file);

describe('CJK font selection', () => {
  it('parses fc-list file output', () => {
    expect(parseFcList('/a/wqy.ttc: \n\n/b/noto.otf:\n')).toEqual(['/a/wqy.ttc', '/b/noto.otf']);
  });

  it('rejects an fc-match answer that fc-list does not list for zh', () => {
    const result = selectCjkFont({
      envFont: undefined,
      matched: '/usr/share/fonts/liberation/LiberationMono-Regular.ttf',
      zhFonts: [],
      commonPaths: ['/missing/cjk.ttc'],
      exists: present('/usr/share/fonts/liberation/LiberationMono-Regular.ttf'),
    });
    expect(result.font).toBeNull();
    expect(result.checked).not.toContain('/usr/share/fonts/liberation/LiberationMono-Regular.ttf');
  });

  it('accepts an fc-match answer that fc-list lists for zh', () => {
    const font = '/usr/share/fonts/wqy/wqy-zenhei.ttc';
    const result = selectCjkFont({ envFont: undefined, matched: font, zhFonts: [font], commonPaths: [], exists: present(font) });
    expect(result.font).toBe(font);
  });

  it('falls through to the path list when fontconfig has no zh font', () => {
    const result = selectCjkFont({
      envFont: undefined, matched: '/x/latin.ttf', zhFonts: [],
      commonPaths: ['/a/none.ttc', '/b/noto.ttc'], exists: present('/b/noto.ttc'),
    });
    expect(result.font).toBe('/b/noto.ttc');
  });

  it('prefers an existing DEV01_CJK_FONT and rejects a missing one', () => {
    expect(selectCjkFont({ envFont: '/f.ttc', matched: '', zhFonts: [], exists: present('/f.ttc') }).font).toBe('/f.ttc');
    expect(() => selectCjkFont({ envFont: '/gone.ttc', matched: '', zhFonts: [], exists: present() }))
      .toThrow(/DEV01_CJK_FONT does not exist: \/gone\.ttc/);
  });
});
