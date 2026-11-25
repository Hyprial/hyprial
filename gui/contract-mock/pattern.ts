/**
 * Python `re` semantics for JSON-Schema `pattern`, evaluated by an ECMAScript engine.
 *
 * Decision record (1e-2 after review, 2026-11-15)
 * - The daemon validates params with Python `jsonschema` (Draft 2020-12,
 *   `re.search` over str). The first comparison probed no input where the two
 *   engines differ, and the review found two: `$` also matches before one
 *   trailing newline in Python, and `\s` is a different set in each engine.
 *   The mock must answer like the daemon, so patterns are translated at load.
 * - Translation is a tokeniser, not a rewrite of the source text. Outside a
 *   class `$` becomes `(?=\n?$)`; `.` becomes `[^\n]` (Python excludes only
 *   `\n`, ECMAScript also `\r`, U+2028 and U+2029); `\s` and `\S` become
 *   explicit sets inside and outside classes. The whitespace set is the one
 *   `str.isspace()` and `re` `\s` agree on, measured over all code points:
 *   U+0009-000D, U+001C-0020, U+0085, U+00A0, U+1680, U+2000-200A, U+2028,
 *   U+2029, U+202F, U+205F, U+3000. U+FEFF is not in it (ECMAScript has it).
 * - A construct whose two meanings differ and is not translated here throws
 *   "unsupported pattern construct" at load: `\d \D \w \W \b \B` (Unicode
 *   aware in Python, ASCII in ECMAScript), `\p \P \k`, back references,
 *   `\u{...}`, group forms other than `(`, `(?:`, `(?=`, `(?!`, a `[` inside
 *   a class, and `{,n}`. Nothing is therefore silently different.
 * - `scripts/verify/gen_pattern_parity.py` records Python's verdict for every
 *   distinct contract pattern over an edge-character input set in
 *   `gui/tests/contract-mock/pattern-parity.fixture.json`; a vitest replays it
 *   and a pytest runs the generator with `--check`, so the fixture cannot drift
 *   from Python.
 */

const WHITESPACE: readonly (readonly [number, number])[] = [
  [0x09, 0x0d],
  [0x1c, 0x20],
  [0x85, 0x85],
  [0xa0, 0xa0],
  [0x1680, 0x1680],
  [0x2000, 0x200a],
  [0x2028, 0x2029],
  [0x202f, 0x202f],
  [0x205f, 0x205f],
  [0x3000, 0x3000],
];

const MAX_CODE_POINT = 0x10ffff;
const PASS_THROUGH_LETTERS = 'nrtfvx0u';
const UNSUPPORTED_LETTERS = 'dDwWbBpPk123456789cN';

function escapeOf(point: number): string {
  return `\\u{${point.toString(16)}}`;
}

function rangesText(ranges: readonly (readonly [number, number])[]): string {
  return ranges
    .map(([low, high]) => (low === high ? escapeOf(low) : `${escapeOf(low)}-${escapeOf(high)}`))
    .join('');
}

function complement(ranges: readonly (readonly [number, number])[]): (readonly [number, number])[] {
  const out: [number, number][] = [];
  let next = 0;
  for (const [low, high] of ranges) {
    if (low > next) out.push([next, low - 1]);
    next = high + 1;
  }
  if (next <= MAX_CODE_POINT) out.push([next, MAX_CODE_POINT]);
  return out;
}

const SPACE = rangesText(WHITESPACE);
const NOT_SPACE = rangesText(complement(WHITESPACE));

function unsupported(construct: string, where: string): Error {
  return new Error(`${where}: unsupported pattern construct ${construct}`);
}

export function pythonPattern(source: string, where = '<pattern>'): string {
  let out = '';
  let inClass = false;
  let index = 0;
  while (index < source.length) {
    const char = source[index];
    if (char === '\\') {
      const letter = source[index + 1];
      if (letter === undefined) throw unsupported('trailing backslash', where);
      index += 2;
      if (letter === 's') {
        out += inClass ? SPACE : `[${SPACE}]`;
      } else if (letter === 'S') {
        out += inClass ? NOT_SPACE : `[${NOT_SPACE}]`;
      } else if (UNSUPPORTED_LETTERS.includes(letter)) {
        throw unsupported(`\\${letter}`, where);
      } else if (letter === 'u' && source[index] === '{') {
        throw unsupported('\\u{', where);
      } else if (/[A-Za-z]/.test(letter) && !PASS_THROUGH_LETTERS.includes(letter)) {
        throw unsupported(`\\${letter}`, where);
      } else {
        out += `\\${letter}`;
      }
      continue;
    }
    index += 1;
    if (inClass) {
      if (char === ']') inClass = false;
      else if (char === '[') throw unsupported('[ inside a class', where);
      out += char;
      continue;
    }
    if (char === '[') {
      inClass = true;
      out += char;
      if (source[index] === '^') {
        out += '^';
        index += 1;
      }
      if (source[index] === ']') {
        throw unsupported('a class that starts with ]', where);
      }
    } else if (char === '$') {
      out += '(?=\\n?$)';
    } else if (char === '.') {
      out += '[^\\n]';
    } else if (char === '(' && source[index] === '?') {
      const kind = source.slice(index, index + 2);
      if (kind !== '?:' && kind !== '?=' && kind !== '?!') throw unsupported(`(${source.slice(index, index + 2)}`, where);
      out += char;
    } else if (char === '{' && source[index] === ',') {
      throw unsupported('{,n}', where);
    } else {
      out += char;
    }
  }
  if (inClass) throw unsupported('an unterminated class', where);
  return out;
}

const compiled = new Map<string, RegExp>();

export function compilePattern(source: string, where = '<pattern>'): RegExp {
  let regex = compiled.get(source);
  if (regex === undefined) {
    regex = new RegExp(pythonPattern(source, where), 'u');
    compiled.set(source, regex);
  }
  return regex;
}
