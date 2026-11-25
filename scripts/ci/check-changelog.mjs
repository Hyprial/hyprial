#!/usr/bin/env node
// Formal releases require real notes for the exact VERSION. No dependencies.
// --rev reads BOTH files from Git, so release-tag never checks dev's notes
// in place of the older commit it was asked to tag.
// Scope (Allen 2026-10-26: basic mistake prevention only): this catches a
// forgotten, empty, duplicated or misnamed section. It does not defend against
// deliberately forged Markdown (raw HTML blocks, Setext headings); CHANGELOG.md
// uses ATX headings only. Review findings: docs/notes/changelog-gate-review-2026-10-26/.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

function options(args) {
  const result = { root: resolve(fileURLToPath(new URL('../..', import.meta.url))) };
  for (let i = 0; i < args.length; i++) {
    const key = args[i];
    if (!['--root', '--rev', '--version', '--tag'].includes(key) || !args[i + 1]) {
      throw new Error(`usage: check-changelog.mjs [--version VERSION] [--tag vVERSION] [--rev SHA] [--root DIR]; invalid argument ${key}`);
    }
    if (Object.hasOwn(result, key.slice(2)) && key !== '--root') {
      throw new Error(`duplicate argument ${key}`);
    }
    result[key.slice(2)] = args[++i];
  }
  return result;
}

function read(root, rev, path) {
  if (!rev) return readFileSync(resolve(root, path), 'utf8');
  return execFileSync('git', ['-C', root, 'show', `${rev}:${path}`], {
    encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'pipe'],
  });
}

// Only visible Markdown counts. Ignore fenced/indented code and HTML comments.
// Keep line breaks in comments: deleting a comment must not join two headings.
function visibleLines(text) {
  text = text.replace(/<!--[\s\S]*?(?:-->|$)/g, match => match.replace(/[^\n]/g, ''));
  const lines = [];
  let fence = null;
  for (const line of text.split(/\r?\n/)) {
    if (fence) {
      const close = line.match(/^ {0,3}(`{3,}|~{3,})\s*$/);
      if (close && close[1][0] === fence[0] && close[1].length >= fence.length) fence = null;
      continue;
    }
    const open = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (open) { fence = open[1]; continue; }
    if (/^(?: {4}|\t)/.test(line)) continue;
    lines.push(line);
  }
  return lines;
}

function requireNotes(text, version) {
  let active = false;
  let sections = 0;
  let bullet = false;
  for (const line of visibleLines(text)) {
    const boundary = line.match(/^ {0,3}#{1,2}(?:\s|$)/);
    if (boundary) {
      const heading = line.match(/^ {0,3}## \[([^\]]+)\](?: - \d{4}-\d{2}-\d{2})?\s*$/);
      active = heading?.[1] === version;
      if (active) sections++;
    } else if (active && /^ {0,3}[-*+]\s+\S/.test(line)) {
      // A thematic break is not a list item, even when it contains spaces.
      if (!/^\s*(?:\*\s*){3,}$/.test(line) && !/^\s*(?:-\s*){3,}$/.test(line)) bullet = true;
    }
  }
  if (!sections) throw new Error(`missing section: CHANGELOG.md needs ## [${version}] (optional date suffix)`);
  if (sections > 1) throw new Error(`duplicate section: CHANGELOG.md has ${sections} headings for ${version}`);
  if (!bullet) throw new Error(`no non-empty bullet: CHANGELOG.md section [${version}] needs a release note`);
}

try {
  const opts = options(process.argv.slice(2));
  const declared = read(opts.root, opts.rev, 'VERSION').trim();
  const version = opts.version ?? declared;
  const versionPattern = /^\d+\.\d+\.\d+(?:[.\-+][0-9A-Za-z.\-+]+)?$/;
  if (!versionPattern.test(declared) || !versionPattern.test(version)) throw new Error('invalid VERSION or --version');
  if (opts.tag !== undefined) {
    if (!opts.tag.startsWith('v') || !versionPattern.test(opts.tag.slice(1))) {
      throw new Error(`invalid release tag: ${opts.tag}; expected vVERSION`);
    }
    if (opts.tag.slice(1) !== declared || version !== declared) {
      throw new Error(`tag/VERSION mismatch: tag=${opts.tag}, VERSION=${declared}, requested=${version}`);
    }
  }
  requireNotes(read(opts.root, opts.rev, 'CHANGELOG.md'), version);
  console.log(`changelog gate: PASS version=${version} source=${opts.rev ?? 'worktree'}`);
} catch (error) {
  console.error(`changelog gate: FAIL ${error.message}`);
  process.exitCode = 1;
}
