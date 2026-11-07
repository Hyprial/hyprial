#!/usr/bin/env python3
"""Red when a published-field change leaves ``contract/`` unsynced.

Motivation (2026-09-15, hq #139 charter 5f): #519 (502427cf) changed
``diagnostics.meshHolders`` from "holders with a record" to "all responders".
``contract/e2e/01-two-machine-roundtrip.md:90-93,141`` depends on
that field and was not in the diff; neither review caught it, and E2E-001
phase6c went deterministically red until #528 fixed the scenario. The check
below is the implementation-side gate that makes that class of drift loud
instead of remembered.

Contract of this script:

- Increment = ``merge-base(base, head)..head`` -- the branch's OWN delta, not
  the direct two-head diff. A direct diff would attribute other people's
  changes (arriving via the base branch) to this PR and the gate would demand
  pointless re-reviews after every merge.
- Candidates are published-name literals on DELETED lines under ``src/``:
  dict/JSON keys (``"meshHolders": ...``), error codes (``code="..."``),
  dotted event names. Ruled 2026-09-15 (hyprial-developer, after the broader
  touched-lines draft measured 7/20 red on merged PRs): a purely ADDED key
  breaks no published contract -- what breaks one is an existing definition
  line being deleted or modified, and a modification already appears on the
  deleted side of its hunk. The added side is used ONLY to cancel a
  move/reformat (same name + same first value token re-added verbatim).
  #519's counter-shape stays red: same key, different value token.
- A candidate that ``git grep`` still finds under ``contract/`` at head is an
  impact. If EVERY hitting file was changed in the same increment the contract
  is considered synced (file granularity -- the v1 rule from the spec).
- A PR-body line ``Contract-Impact: <non-empty reason>`` downgrades every
  impact to a notice: an explicit, named release instead of a silent one.
- Fail-closed: anything that prevents measurement (unresolvable rev, no
  merge-base, git failure) exits 2. "Could not measure" is never folded into
  green. Green is only ever "measured, zero undeclared impacts".

Output always prints the denominators next to the conclusion -- a zero that
cannot show its scanned totals is indistinguishable from a scan that never
ran.

Decision (2026-11-03): the dataclasses below drop ``slots=`` so the script
runs on the ci-light host runner's system python3 (3.9.6, first supported
release for ``slots=`` is 3.10). The gate must stay installable-free: its
workflow runs ``python3`` directly and must not acquire uv or a managed
interpreter.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = Path.cwd()

# String-literal dict/JSON key:  "meshHolders": ...   'key' : ...
# Optional f/r/b/u prefix on the literal; the key text must be non-empty.
_KEY_RE = re.compile(
    r"""[fFrRbBuU]{0,2}(?P<q>["'])(?P<key>(?:(?!(?P=q)).)+)(?P=q)(?P<ws>\s*):"""
)
# Error-code keyword:  code="TARGET_OFFLINE"  -- the VALUE is the published name.
_CODE_RE = re.compile(
    r"""\bcode\s*=\s*(?P<q>["'])(?P<key>(?:(?!(?P=q)).)+)(?P=q)"""
)
# Dotted event/diagnostic name used as a bare string value:
#   log("message.status.holders_missing", ...)  -- the literal is the name.
_EVENT_RE = re.compile(
    r"""(?<![\w.])(?P<name>[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)(?<![\w.])"""
)

# A published name must be DISTINCTIVE: single common words (name, target,
# role, records, version) cannot be told apart from prose by `git grep -F`, so
# extracting them produces substring noise, not signal. Measured 2026-09-15
# on the last 20 merged PRs: every prose false positive came from a single
# lowercase word; every true finding (meshHolders, actorName, graphId,
# deadlineMs) was camelCase, dotted, SCREAMING_SNAKE, or multi-word snake.
# This is a documented recall gap, not an accident: a single-word published
# key whose semantics change is out of this gate's reach and needs the
# contract files changed in the same PR (or a Contract-Impact line) like any
# other change.
_DISTINCTIVE_RE = re.compile(
    r"^(?:"
    r"[a-z]+(?:[A-Z][a-z0-9]*)+"  # camelCase: meshHolders, graphId
    r"|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"  # SCREAMING_SNAKE: TARGET_OFFLINE
    r"|[a-z0-9]+(?:\.[a-z0-9_]+)+"  # dotted event: message.status.holders_missing
    r"|[a-z0-9]+(?:_[a-z0-9]+)+"  # multi-word snake: holders_missing
    r")$"
)


def _distinctive(name: str) -> bool:
    return bool(_DISTINCTIVE_RE.match(name))


def _value_token(rest: str) -> str:
    """The value's first token: up to a comma that is OUTSIDE brackets/quotes.

    Why not the whole rest-of-line: #500 rewrote
    `"deadlineMs": node["deadlineMs"]} if node["kind"] ...` into a multi-line
    conditional with the SAME key and value. Whole-line comparison called that
    a semantic change; the first token (`node["deadlineMs"]`) does not. The
    counter-shape stays caught: #519's `list(mesh.holders)` vs
    `list(mesh.responded_holders)` differ in the first token.
    """
    depth = 0
    quote: str | None = None
    for index, char in enumerate(rest):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return rest[:index]
            depth -= 1
        elif char == "," and depth == 0:
            return rest[:index]
    return rest
# PR-body declaration. Same leading-token shape as the Skill-Impact gate.
_DECLARE_RE = re.compile(r"^\s*Contract-Impact:\s*(?P<reason>\S.*?)\s*$", re.IGNORECASE)

EXIT_GREEN = 0
EXIT_RED = 1
EXIT_OPERATIONAL = 2


@dataclass(frozen=True)
class Hit:
    """One contract/ file that still references a candidate name."""

    name: str
    path: str
    line_no: int
    line_text: str


@dataclass(frozen=True)
class Candidate:
    """One published name extracted from a deleted source line."""

    name: str
    shape: str  # "key" | "code" | "event"
    src_path: str
    line_no: int
    line_text: str


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _rev_parse(repo: Path, rev: str) -> str:
    proc = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        print(
            f"contract-impact: OPERATIONAL: cannot resolve rev {rev!r}: "
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_OPERATIONAL)
    return proc.stdout.strip()


def _merge_base(repo: Path, base: str, head: str) -> str:
    proc = _git(repo, "merge-base", base, head, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        print(
            f"contract-impact: OPERATIONAL: no merge-base for {base}..{head} "
            "(unrelated histories or shallow fetch): "
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_OPERATIONAL)
    return proc.stdout.strip()


def _changed_files(repo: Path, base: str, head: str) -> set[str]:
    proc = _git(repo, "diff", "--name-only", base, head, check=False)
    if proc.returncode != 0:
        print(
            f"contract-impact: OPERATIONAL: git diff --name-only failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_OPERATIONAL)
    return set(proc.stdout.splitlines())


def _diff_src_lines(
    repo: Path, base: str, head: str
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Deleted and added (path, line_no, text) pairs for src/ files.

    ``-U0`` so every reported line is a genuine +/- line; context lines would
    otherwise let a moved key look deleted here and alive there.
    """
    proc = _git(repo, "diff", "-U0", base, head, "--", "src/", check=False)
    if proc.returncode != 0:
        print(
            f"contract-impact: OPERATIONAL: git diff -U0 failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_OPERATIONAL)
    deleted: list[tuple[str, int, str]] = []
    added: list[tuple[str, int, str]] = []
    path = ""
    old_path = ""
    line_no = 0
    for raw in proc.stdout.splitlines():
        if raw.startswith("--- a/"):
            # A deleted FILE reports `+++ /dev/null`; its real path lives on
            # the `--- a/` side. Without this, a whole-file deletion's impact
            # lines would cite `/dev/null` as the source location.
            old_path = raw[6:]
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("+++ b/"):
            path = raw[6:]
            continue
        if raw.startswith("+++ /dev/null"):
            path = old_path
            continue
        if raw.startswith("@@"):
            # @@ -l,s +l,s @@ -- take the '+' side hunk start.
            hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if hunk is not None:
                line_no = int(hunk.group(1))
            continue
        if raw.startswith("+"):
            added.append((path, line_no, raw[1:]))
            line_no += 1
        elif raw.startswith("-"):
            deleted.append((path, line_no, raw[1:]))
    return deleted, added


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _extract(line_text: str) -> list[tuple[str, str, str]]:
    """(name, shape, normalized_pair) pairs found on one source line."""
    pairs: list[tuple[str, str, str]] = []
    for match in _KEY_RE.finditer(line_text):
        key = match.group("key")
        if not key or key.strip() != key:
            continue
        if not _distinctive(key):
            continue
        rest = line_text[match.end():]
        pairs.append((key, "key", _normalize(f"{key}:{_value_token(rest)}")))
    for match in _CODE_RE.finditer(line_text):
        code = match.group("key")
        if not code or not _distinctive(code):
            continue
        pairs.append((code, "code", _normalize(f"code={code}")))
    for match in _EVENT_RE.finditer(line_text):
        event = match.group("name")
        pairs.append((event, "event", _normalize(f"event={event}")))
    return pairs


def _contract_hits(repo: Path, head: str, name: str) -> list[Hit]:
    # -F: the name is a literal, and POSIX ERE would eat \b/\s anyway.
    # git grep exits 1 for "no hits" -- that is a measured zero, not a
    # failure; anything above 1 is operational and must fail closed.
    proc = _git(repo, "grep", "-n", "-F", "-e", name, head, "--", "contract/",
                check=False)
    if proc.returncode not in (0, 1):
        print(
            f"contract-impact: OPERATIONAL: git grep {name!r} failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_OPERATIONAL)
    hits: list[Hit] = []
    for line in proc.stdout.splitlines():
        # <tree>:<path>:<line_no>:<text>
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        _tree, path, line_no, text = parts
        if not line_no.isdigit():
            continue
        hits.append(Hit(name=name, path=path, line_no=int(line_no), line_text=text))
    return hits


def _declaration(body: str) -> str | None:
    for line in body.splitlines():
        match = _DECLARE_RE.match(line)
        if match is not None:
            return match.group("reason")
    return None


def check(
    repo: Path,
    base_rev: str,
    head_rev: str,
    pr_body: str = "",
) -> int:
    base = _rev_parse(repo, base_rev)
    head = _rev_parse(repo, head_rev)
    merge_base = _merge_base(repo, base, head)
    changed = _changed_files(repo, merge_base, head)
    deleted, added = _diff_src_lines(repo, merge_base, head)

    # Deleted-side-only candidates, ruled 2026-09-15 (hyprial-developer): a
    # purely added key breaks no published contract; a modified definition
    # line already appears on the deleted side of its hunk. The added side
    # is used ONLY to cancel a move/reformat: the same name with the same
    # first value token re-added verbatim is not a semantic change. #519's
    # counter-shape stays caught -- same key, different value token.
    added_pairs = {
        pair for _p, _n, text in added for pair in [p2[2] for p2 in _extract(text)]
    }

    candidates: list[Candidate] = []
    seen_names: set[str] = set()
    src_files = {p for p, _n, _t in deleted} | {p for p, _n, _t in added}
    for path, line_no, text in deleted:
        for name, shape, pair in _extract(text):
            if pair in added_pairs or name in seen_names:
                continue
            seen_names.add(name)
            candidates.append(
                Candidate(name=name, shape=shape, src_path=path,
                          line_no=line_no, line_text=text.strip())
            )

    impacts: list[tuple[Candidate, list[Hit]]] = []
    synced_or_clean: list[tuple[Candidate, list[Hit]]] = []
    for candidate in candidates:
        hits = _contract_hits(repo, head, candidate.name)
        if hits and any(hit.path not in changed for hit in hits):
            impacts.append((candidate, hits))
        else:
            synced_or_clean.append((candidate, hits))

    reason = _declaration(pr_body)
    declared = reason is not None

    print(
        f"contract-impact: merge_base={merge_base[:12]} head={head[:12]} "
        f"src_files_scanned={len(src_files)} deleted_lines={len(deleted)} "
        f"added_lines={len(added)} candidates={len(candidates)} "
        f"impacts={len(impacts)} declared={'yes' if declared else 'no'}"
    )
    for candidate, _hits in synced_or_clean:
        print(
            f"  candidate {candidate.name!r} ({candidate.shape}) "
            f"at {candidate.src_path}:{candidate.line_no} -- contract synced or "
            "no contract reference"
        )
    for candidate, hits in impacts:
        for hit in hits:
            if hit.path in changed:
                continue
            print(
                f"  IMPACT {candidate.name!r} ({candidate.shape}) "
                f"{candidate.src_path}:{candidate.line_no} "
                f"{candidate.line_text!r} -> {hit.path}:{hit.line_no} "
                f"(contract file NOT changed in this increment)"
            )

    if impacts and declared:
        print(
            f"contract-impact: DECLARED RELEASE -- {reason!r}; "
            f"{len(impacts)} impact(s) downgraded to notice"
        )
    if impacts and not declared:
        print(
            "contract-impact: RED -- published field changed, contract/ still "
            "references it and was not synced. Fix the contract files above, or "
            "add a PR-body line 'Contract-Impact: <why this release is safe>'"
        )
        return EXIT_RED
    if not candidates:
        # Printed on the summary line too; repeated here so the empty case has
        # its own positive record instead of reading as "nothing to report".
        print("contract-impact: 0 candidate names -- check ran and found none")
    return EXIT_GREEN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base rev (e.g. PR base sha)")
    parser.add_argument("--head", required=True, help="head rev (e.g. PR head sha)")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="git repo path")
    parser.add_argument(
        "--pr-body-file",
        default=None,
        help="file containing the PR body ('-' for stdin); declaration line "
        "'Contract-Impact: <reason>' downgrades reds to notices",
    )
    args = parser.parse_args(argv)

    pr_body = ""
    if args.pr_body_file is not None:
        try:
            if args.pr_body_file == "-":
                pr_body = sys.stdin.read()
            else:
                pr_body = Path(args.pr_body_file).read_text(encoding="utf-8")
        except OSError as error:
            # A missing body file is an operational failure, not a verdict:
            # it must not crash into exit 1 and masquerade as RED.
            print(
                f"contract-impact: OPERATIONAL: cannot read PR body "
                f"{args.pr_body_file!r}: {error}",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_OPERATIONAL) from error

    return check(Path(args.repo), args.base, args.head, pr_body)


if __name__ == "__main__":
    sys.exit(main())
