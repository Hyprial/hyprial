#!/usr/bin/env python3
"""Detect a "shell PR" -- a pull request whose work already landed by other means.

Design ruling (hq 2026-10-11, promoted to ``flow-gate-discipline`` §6.11; the
③-3 ownership ruling is enforced by construction here):

* The criterion is evaluated against **the PR's own ``base.ref``**.  Nothing in
  this file knows about ``intg/batch-1008`` or any other fixed branch: a lane
  base (``intg/<lane>``) is a first-class base.  Hardcoding a base would produce
  the #1324 false negative that motivated the rule.
* ``git cherry``'s polarity is pinned, because writing it backwards is the easy
  mistake:
    - empty output          -> the head is an ancestor of the base (FF / direct push);
    - every line starts '-' -> patch-equivalent: the change landed by squash or
      rebase, so the head tree is a shell;
    - any line starts '+'   -> NOT landed; this PR carries work that is not in
      the base, so it is not a shell and this tool does nothing.
* **fail-closed**: if the refs cannot be resolved or ``git cherry`` errors, the
  verdict is ``inconclusive`` and no label/comment is emitted.  A wrong label is
  worse than a missing one.
* **The close action is not here.**  This tool may add the label
  ``shell-pr-landed`` (a NAMED BENIGN state, non-red) and one comment; closing
  the PR belongs to a write-capable owner -- the PR's line owner, with hq as the
  backstop -- never to this detector.
* ``--apply`` is required to write anything; the default is a dry run that
  prints exactly what it would do.  The label is the signal; the mechanical part
  is the ancestry/patch check.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LABEL = "shell-pr-landed"
DEFAULT_SERVER = "http://git.internal.hyprial.com:3000"

# Verdicts.  Only SHELL_VERDICTS lead to a label/comment.
V_ANCESTOR = "ancestor"
V_PATCH_EQUIVALENT = "patch-equivalent"
V_NOT_LANDED = "not-landed"
V_INCONCLUSIVE = "inconclusive"
SHELL_VERDICTS = (V_ANCESTOR, V_PATCH_EQUIVALENT)


def git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run git and capture text.  ``check`` is opt-in: callers decide what a
    non-zero status means (an error is NEVER silently a skip)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def resolve_base_ref(base_ref: str, cwd: Path) -> tuple[str, str] | None:
    """Resolve the PR's own base ref to (ref, sha).

    ``base_ref`` arrives as the PR's ``base.ref`` (a short name such as ``dev``
    or ``intg/<lane>``).  The remote-tracking form is preferred so the answer is
    the branch the PR actually targets; the local form is the fallback for
    fixtures and detached checkouts.
    """
    candidates = []
    if base_ref and not base_ref.startswith("refs/"):
        candidates = [f"origin/{base_ref}", base_ref]
    elif base_ref:
        candidates = [base_ref, f"origin/{base_ref.removeprefix('refs/heads/')}"]
    for ref in candidates:
        done = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
        if done.returncode == 0 and done.stdout.strip():
            return ref, done.stdout.strip()
    return None


def head_is_ancestor_of_base(head: str, base_ref: str, cwd: Path) -> bool | None:
    """True/False, or None when git could not answer (fail-closed upstream).

    ``merge-base --is-ancestor A B`` == 0 means A is an ancestor of B.  We ask
    whether the PR head is already contained in the base: that is the FF/direct
    -push shape of a landed shell.
    """
    done = git(["merge-base", "--is-ancestor", head, base_ref], cwd)
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    return None


def cherry_polarity(base_ref: str, head: str, cwd: Path) -> tuple[str, list[str]] | None:
    """Return (classification, lines) for ``git cherry <base> <head>``.

    classification is one of "empty", "all-minus", "has-plus", "other".
    None means the command itself failed (fail-closed).
    """
    done = git(["cherry", base_ref, head], cwd)
    if done.returncode != 0:
        return None
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if not lines:
        return "empty", lines
    if all(line.startswith("-") for line in lines):
        return "all-minus", lines
    if any(line.startswith("+") for line in lines):
        return "has-plus", lines
    return "other", lines


def classify(base_ref: str, base_sha: str, head: str, cwd: Path) -> tuple[str, dict]:
    """Decide whether this PR is a landed shell.

    Returns (verdict, facts).  ``facts`` carries the raw readings so the comment
    can state them instead of asserting them.
    """
    facts: dict = {"base_ref": base_ref, "base_sha": base_sha, "head": head}
    ancestor = head_is_ancestor_of_base(head, base_ref, cwd)
    facts["head_is_ancestor"] = ancestor
    if ancestor is None:
        facts["reason"] = "merge-base --is-ancestor could not answer"
        return V_INCONCLUSIVE, facts

    cherry = cherry_polarity(base_ref, head, cwd)
    if cherry is None:
        facts["reason"] = "git cherry failed"
        return V_INCONCLUSIVE, facts
    polarity, lines = cherry
    facts["cherry_classification"] = polarity
    facts["cherry_lines"] = lines

    if polarity == "empty":
        # Empty output IS the ancestor case; the explicit check above is kept so
        # the comment can state the ancestry reading on its own.
        return V_ANCESTOR, facts
    if polarity == "all-minus":
        return V_PATCH_EQUIVALENT, facts
    if polarity == "has-plus":
        facts["reason"] = "the head carries commits the base does not contain"
        return V_NOT_LANDED, facts
    facts["reason"] = "git cherry produced an unexpected line shape"
    return V_INCONCLUSIVE, facts


def comment_body(facts: dict, verdict: str) -> str:
    """The fixed comment content: base@sha, the ancestor reading, the cherry result."""
    ancestor = facts.get("head_is_ancestor")
    ancestor_text = {True: "yes (the head is already contained in the base)",
                     False: "no", None: "unknown"}[ancestor]
    lines = facts.get("cherry_lines") or []
    cherry_text = facts.get("cherry_classification", "unknown")
    body = [
        f"`{LABEL}` — this PR looks like a **landed shell**: its work is already in "
        f"the base, so the branch has nothing left to merge.",
        "",
        f"- base: `{facts['base_ref']}@{facts['base_sha'][:12]}`",
        f"- head: `{facts['head'][:12]}`",
        f"- head is already contained in the base: **{ancestor_text}**",
        f"- `git cherry {facts['base_ref']} {facts['head'][:12]}`: "
        f"**{cherry_text}** ({len(lines)} line(s))",
        "",
        "This label is informational (non-red). It does **not** close anything: the "
        "close action belongs to this PR's line owner (hq is the backstop). If this "
        "is a false positive, reply here and the label will be reconsidered.",
        f"verdict: `{verdict}`",
    ]
    if facts.get("reason"):
        body.append(f"reason: {facts['reason']}")
    return "\n".join(body)


def _api(server: str, repo: str, token: str, path: str, payload: dict) -> tuple[bool, str]:
    url = f"{server.rstrip('/')}/api/v1/repos/{repo}{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}: {error.read()[:300].decode('utf-8', 'replace')}"
    except Exception as error:  # noqa: BLE001 -- reported, never swallowed
        return False, f"{type(error).__name__}: {error}"


def apply_label_and_comment(server: str, repo: str, pr: int, token: str, body: str) -> list[str]:
    """Emit the label and the comment.  Returns human-readable outcome lines.

    Both writes are attempted independently: a missing label must not suppress
    the comment, which is the part a human reads.
    """
    outcomes: list[str] = []
    ok, detail = _api(server, repo, token, f"/issues/{pr}/labels", {"labels": [LABEL]})
    outcomes.append(f"label {LABEL}: {'ok' if ok else 'FAILED'} ({detail})")
    ok, detail = _api(server, repo, token, f"/issues/{pr}/comments", {"body": body})
    outcomes.append(f"comment: {'ok' if ok else 'FAILED'} ({detail})")
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, default=os.environ.get("PR_NUMBER") or None)
    parser.add_argument("--base", default=os.environ.get("PR_BASE_REF") or None,
                        help="the PR's own base.ref (never a hardcoded branch)")
    parser.add_argument("--head", default=os.environ.get("PR_HEAD_SHA") or None)
    parser.add_argument("--apply", action="store_true",
                        help="actually write the label/comment (default: dry run)")
    parser.add_argument("--repo", default=os.environ.get("FORGEJO_REPO")
                        or os.environ.get("GITHUB_REPOSITORY") or None)
    parser.add_argument("--server", default=os.environ.get("FORGEJO_SERVER_URL") or DEFAULT_SERVER)
    parser.add_argument("--cwd", default=str(ROOT))
    args = parser.parse_args(argv)

    cwd = Path(args.cwd)
    if not args.base or not args.head:
        print("shell-pr: verdict=inconclusive reason=missing --base/--head (fail-closed)")
        return 0

    resolved = resolve_base_ref(args.base, cwd)
    if resolved is None:
        print(f"shell-pr: base-ref={args.base} verdict=inconclusive "
              f"reason=base ref cannot be resolved (looked for origin/{args.base} and {args.base})")
        return 0
    base_ref, base_sha = resolved

    verdict, facts = classify(base_ref, base_sha, args.head, cwd)
    print(f"shell-pr: base-ref={base_ref} base-sha={base_sha[:12]} head={args.head[:12]} "
          f"head_is_ancestor={facts.get('head_is_ancestor')} "
          f"cherry={facts.get('cherry_classification', 'n/a')} verdict={verdict}")

    if verdict not in SHELL_VERDICTS:
        print(f"shell-pr: action=none reason={facts.get('reason', 'work is not a landed shell')}")
        return 0

    body = comment_body(facts, verdict)
    if not args.apply:
        print("shell-pr: action=dry-run (would add label + comment)")
        print("---- comment body (verbatim) ----")
        print(body)
        print("---- end comment body ----")
        return 0

    token = os.environ.get("FORGEJO_TOKEN") or ""
    if not token or not args.pr or not args.repo:
        print("shell-pr: action=none reason=fail-closed: need FORGEJO_TOKEN, --pr and --repo "
              "to write; the detection above stands on its own")
        return 0
    for line in apply_label_and_comment(args.server, args.repo, args.pr, token, body):
        print(f"shell-pr: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
