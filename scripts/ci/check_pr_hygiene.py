#!/usr/bin/env python3
"""Merge-blocking PR-hygiene gates (standard library + git only).

Three rules, each its own subcommand, so a red CI step names the rule it failed
on instead of collapsing separate contracts into one anonymous failure:

1. ``merge-tree``      -- materialise the merge of HEAD with ``origin/<base>``
   in a worktree and run this repository's structural gates plus the fast
   CI-policy tests THERE.  PR CI checks out the PR head (``ci-plan.mjs``
   asserts ``rev-parse HEAD`` equals ``pr.head.sha``), so the composition of a
   merge is otherwise unobserved: a change can be green on the head tree and
   red on the merge tree (or the reverse).

2. ``registry-union``  -- for every registry in ``registry-union.json`` the
   MERGED content must be a superset of the base entries UNION the head
   entries.  A merge that silently drops one side's entry is a data loss, not
   a preference, and is red.  This is deliberately the ONLY invariant here: a
   legitimate shrink is each registry's own decision and needs its own ruling.

3. ``submitted-by``    -- the PR body's first non-empty line, after the BOM,
   whitespace and leading HTML comments, must be ``Submitted-by: agent:<URI>``.
   Forgejo's author is the shared account, so the body is the only carrier of
   the submitting agent's identity.

``source-branch`` remains a no-op compatibility command for older workflow
revisions. Allen's 2026-11-08 dev-first flow retires release-first admission;
completed feature, fix, and integration branches target dev directly.

Version note (measured 2026-10-09, git 2.50.1): the merge tree is built with
``git merge --no-commit --no-ff`` inside a worktree, NOT with
``git merge-tree --write-tree`` (whose machine-readable output changed in
2.38).  ``git merge-base --is-ancestor`` keeps its stable exit semantics
(0 ancestor, 1 not, >1 error) and an error is treated as a red, never a skip.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
try:
    from scripts.git_environment import (
        assert_no_credentials,
        hardened_git_environment,
        ignored_git_environment_warning,
        uncredentialed_subprocess_environment,
    )
except ModuleNotFoundError:  # direct ``python scripts/ci/check_pr_hygiene.py``
    sys.path.insert(0, str(ROOT))
    from scripts.git_environment import (
        assert_no_credentials,
        hardened_git_environment,
        ignored_git_environment_warning,
        uncredentialed_subprocess_environment,
    )

REGISTRY_CONFIG = ROOT / "scripts" / "ci" / "registry-union.json"
IDENTITY_CONFIG = ROOT / "scripts" / "ci" / "pr-identity.json"

# The structural gates this repository already ships.  Rule 2 runs them on the
# merge tree; it does not re-implement their logic.  The import ratchet is pure
# AST over the tree and its committed ledger, so it needs no locked environment;
# without it an unregistered CLI->backend import only surfaced in the full plan
# (#2187).
GATE_SCRIPTS = (
    "scripts/ci/check_structural_limits.py",
    "scripts/ci/check_module_boundaries.py",
    "scripts/ci/check_unbounded_waits.py",
    "scripts/ci/check_import_ratchet.py",
)
# Fast, dependency-free targeted tests: the CI selection/planning policy tests.
# glob is expanded by bash, so a newly added scripts/ci/*.test.mjs is covered.
FAST_GATE_COMMAND = "node --test --test-concurrency=2 scripts/ci/*.test.mjs"

# Two cheap TREE-LEVEL gates that are pure stdlib + pytest, so they run WITHOUT
# the locked project environment (the light intg plan never runs them; on the
# materialised merge tree they are the difference between green and red).  The
# command is fed to bash verbatim in the merge worktree, so it must stay
# shell-quotable and carry PYTHONPATH=src itself.
#
# `--noconftest` is REQUIRED, not cosmetic: the repo-level tests/conftest.py
# imports `hyprial.*` (the file-descriptor prewarm) and would drag pykka -- and
# therefore the whole locked environment -- in without the project env.  The
# price is that bypassing the conftest also skips its fixtures and that fd
# prewarm; these two files do not depend on either today, and the equivalence
# to a full-environment run STOPS holding the day they do.  The full-environment
# comparison is e2e's assembly step's job, NOT pr-hygiene's: running it here
# would need the locked environment and undo the cheapness that is the point.
#
# `tests/tooling/architecture/lint/test_boundaries.py` (the login tripwire) is
# deliberately OUT: it imports `hyprial.*` and needs the locked env, so it stays
# in e2e's assembly step, which runs it by name.
TREE_LEVEL_TESTS = (
    "tests/tooling/architecture/lint/test_provider_term_lint.py",
    "tests/integration/native_harnesses/test_native_resident_tripwires.py",
)
TREE_LEVEL_TEST_COMMAND = (
    "PYTHONPATH=src uv run --no-project --with pytest python -m pytest -q "
    "--noconftest -p no:cacheprovider " + " ".join(TREE_LEVEL_TESTS)
)
FAST_GATE_COMMANDS = (FAST_GATE_COMMAND, TREE_LEVEL_TEST_COMMAND)

OUTPUT_TAIL = 40


class RuleFailure(RuntimeError):
    """A gate is red.  The message is the operator-facing report."""


def _tail(text: str, lines: int = OUTPUT_TAIL) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


GIT_TOKEN_ENV = "PR_HYGIENE_GIT_TOKEN"

# The gate's OWN git operations carry an identity.  A CI checkout has no
# user.name/user.email, so `git merge` used to die with "Please tell me who you
# are" -- and that failure was reported as a merge CONFLICT, sending the author
# to resolve a conflict that did not exist.  The identity is the gate's, not the
# author's: it must never be inferred from the checkout.
GATE_IDENTITY = (
    "-c", "user.name=pr-hygiene",
    "-c", "user.email=pr-hygiene@hyprial.invalid",
)


def _git_env() -> dict[str, str]:
    """The environment for ONE git command.

    Credentials live here and nowhere else: this job runs the PR's own code, so a
    job-level export would hand the repository token to every later step.  The
    repository requires authentication (anonymous ``info/refs`` is 401), so the run
    -scoped token rides in a per-command ``http.<server>/.extraHeader`` -- the same
    mechanism gate-pr-declarations.yml uses.  ``GIT_TERMINAL_PROMPT=0`` keeps a missing
    credential a diagnosable error instead of a /dev/tty prompt.
    """

    warning = ignored_git_environment_warning(os.environ)
    if warning:
        print(warning, file=sys.stderr)
    token = os.environ.get(GIT_TOKEN_ENV)
    server = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    config_entry = None
    if token:
        config_entry = (
            f"http.{server}/.extraHeader",
            f"Authorization: token {token}",
        )
    return hardened_git_environment(
        os.environ,
        config_entry=config_entry,
        identity=("pr-hygiene", "pr-hygiene@hyprial.invalid"),
    )


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_git_env()
    )
    if check and completed.returncode != 0:
        raise RuleFailure(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{_tail(completed.stderr)}"
        )
    return completed


# --- rule 1: base freshness --------------------------------------------------


# Resolving the base tip.  ``fetch`` is what makes the comparison current, but
# the CI checkout's credentials are a transient GIT_ASKPASS the checkout step
# deletes, so a later fetch fails on a credential prompt in that container
# ("could not read Username") -- that is how this job's first real run went red
# (2026-10-09, task log 61522).  The checkout step already fetched
# ``refs/remotes/<remote>/<base_ref>``, so falling back to that ref keeps the
# comparison real (the ref is as old as the job) while the provenance is printed
# and a missing ref stays red.
#
# The degraded path exists ONLY for the credential-less container.  When a
# credential WAS available (``PR_HYGIENE_GIT_TOKEN`` is set) and the fetch still
# failed, that is RED: falling back there would silently soften rule 1 to a
# point-in-time comparison while the job believed it had compared the current
# base.  A ref that is neither fetchable nor present is red for the same reason.
FETCHED = "fetch"
CHECKOUT_REF = "checkout-ref"


def _measured(
    gate: str,
    base_ref: str,
    base_sha: str,
    source: str,
    cwd: Path,
    remote: str = "origin",
    **extra: object,
) -> None:
    """Print the refs a gate measured -- on green runs too.

    The question this answers is "which ref did the gate verify", so the log has
    to answer it; a silent green run leaves it to the reader's inference.  One
    grep-able line, space-separated ``k=v``, full 40-hex shas.
    """

    head = git(["rev-parse", "HEAD"], cwd, check=False).stdout.strip()
    fields = [
        f"gate={gate}",
        f"base={remote}/{base_ref}@{base_sha}",
        f"head=@{head}",
        f"base_source={source}",
        *(f"{name}={value}" for name, value in extra.items()),
    ]
    if source == CHECKOUT_REF:
        fields.append("base_freshness=point-in-time-at-job-start")
    print("pr-hygiene measured: " + " ".join(fields))
    if source == CHECKOUT_REF:
        print(
            "pr-hygiene note: base_source=checkout-ref (job-start; freshness is "
            "point-in-time) -- this run measured the base ref the checkout fetched "
            "when the job started, not the base tip at gate time."
        )


def resolve_base_tip(
    base_ref: str, cwd: Path, remote: str = "origin"
) -> tuple[str, str, str]:
    """Return ``(ref, sha, source)`` for ``remote/<base_ref>``, or raise.

    ``source`` is ``fetch`` when the ref was just fetched and ``checkout-ref``
    when the fetch was impossible but the tracking ref already exists.  A ref
    that is neither fetchable nor present is red: the rules compare, never
    assume.
    """

    refspec = f"+refs/heads/{base_ref}:refs/remotes/{remote}/{base_ref}"
    ref = f"refs/remotes/{remote}/{base_ref}"
    fetched = git(["fetch", "--no-tags", remote, refspec], cwd, check=False)
    if fetched.returncode == 0:
        source = FETCHED
    elif os.environ.get(GIT_TOKEN_ENV):
        # A credential was available, so a failing fetch is the fault -- not a
        # reason to fall back to a ref that is only as old as the job start.
        raise RuleFailure(
            f"base tip {remote}/{base_ref}: the fetch failed although a credential "
            f"was available, so no ref was refreshed:\n{_tail(fetched.stderr)}"
        )
    else:
        present = git(["rev-parse", "--verify", "--quiet", ref], cwd, check=False)
        if present.returncode != 0:
            raise RuleFailure(
                f"base tip {remote}/{base_ref} is unavailable: the fetch failed "
                f"and no local tracking ref exists:\n{_tail(fetched.stderr)}"
            )
        source = CHECKOUT_REF
    sha = git(["rev-parse", ref], cwd, check=False).stdout.strip()
    return ref, sha, source


def fetch_base(base_ref: str, cwd: Path, remote: str = "origin") -> str:
    """The tracking ref for ``base_ref`` (see :func:`resolve_base_tip`)."""

    return resolve_base_tip(base_ref, cwd, remote)[0]


# --- rule 2: merge-tree gates ------------------------------------------------


def _merge_failure_is_a_conflict(worktree: Path, output: str) -> bool:
    """True only when the failed merge carries conflict evidence.

    A non-zero ``git merge`` covers two different things: a real conflict (the
    author's business, and worth a re-review) and the gate's own operation
    failing -- a missing identity, a broken worktree, a lock (never the author's
    business).  Collapsing both into "conflict" is what sent authors looking for
    a conflict that did not exist, so the two are separated by evidence, not by
    assumption.
    """

    unmerged = git(["ls-files", "--unmerged"], worktree, check=False).stdout.strip()
    if unmerged:
        return True
    return "CONFLICT" in output or "Automatic merge failed" in output


def materialise_merge_tree(
    base_ref: str, cwd: Path, worktree: Path, remote: str = "origin"
) -> Path:
    """Create ``worktree`` as the staged merge of HEAD with ``origin/<base>``."""

    _, base_sha, source = resolve_base_tip(base_ref, cwd, remote)
    _measured("merge-tree", base_ref, base_sha, source, cwd, remote, worktree=worktree)
    worktree = Path(worktree)
    if worktree.exists():
        git(["worktree", "remove", "--force", str(worktree)], cwd, check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    git(["worktree", "prune"], cwd, check=False)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", "--force", str(worktree), "HEAD"], cwd)
    completed = git([*GATE_IDENTITY, "merge", "--no-commit", "--no-ff", base_sha], worktree, check=False)
    if completed.returncode != 0:
        output = completed.stdout + completed.stderr
        conflicted = _merge_failure_is_a_conflict(worktree, output)
        git(["merge", "--abort"], worktree, check=False)
        if not conflicted:
            # NOT the author's problem.  Say so, and do not ask for a rebase: a
            # rebase cannot fix a gate whose own operation failed.
            print(
                "pr-hygiene measured: "
                f"gate=merge-tree base={remote}/{base_ref}@{base_sha} state=gate-operation-failed"
            )
            raise RuleFailure(
                "merge-tree: this GATE's own operation failed -- that is not a conflict in "
                "your branch and rebasing will not fix it; it needs the CI owner "
                f"(scripts/ci/check_pr_hygiene.py, rule 2):\n{_tail(output)}"
            )
        print(
            "pr-hygiene measured: "
            f"gate=merge-tree base={remote}/{base_ref}@{base_sha} state=conflict"
        )
        raise RuleFailure(
            f"merge-tree: HEAD does not merge cleanly with {remote}/{base_ref}@{base_sha} "
            "(a merge conflict, or a base that moved); resolve it and push again:\n"
            f"{_tail(completed.stdout + completed.stderr)}"
        )
    # The staged merge's tree, reported so the composition that was gated is
    # named by value rather than inferred from the two commits.
    tree_sha = git(["write-tree"], worktree, check=False).stdout.strip()
    print(
        "pr-hygiene measured: "
        f"gate=merge-tree tree=@{tree_sha} state=merged worktree={worktree}"
    )
    return worktree


def merge_tree_gate_errors(
    worktree: Path,
    gates: tuple[str, ...] = GATE_SCRIPTS,
    fast_gate_commands: tuple[str, ...] = FAST_GATE_COMMANDS,
) -> list[str]:
    """Run the structural gates and the fast CI-policy tests in the merged tree.

    ``fast_gate_commands`` is injectable so a minimal repro can skip commands
    that need the full repository (the tree-level pytest command needs the two
    test files; the Node policy command needs scripts/ci/*.test.mjs).
    """

    errors: list[str] = []
    child_env = uncredentialed_subprocess_environment(os.environ)
    for script in gates:
        target = Path(worktree) / script
        assert_no_credentials(child_env)
        completed = subprocess.run(
            [sys.executable, str(target), "--root", str(worktree)],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=child_env,
        )
        if completed.returncode != 0:
            errors.append(
                f"merge-tree: {script} is red on the merge tree\n"
                f"{_tail(completed.stdout + completed.stderr)}"
            )
    for command in fast_gate_commands:
        assert_no_credentials(child_env)
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=child_env,
        )
        # Print the measured result on GREEN runs too: the point of these two
        # tree-level gates is the count, and a silent green run would leave the
        # reader to infer that they ran at all.
        summary = _tail(completed.stdout + completed.stderr, 1)
        print(
            "pr-hygiene measured: "
            f"gate=merge-tree command={command!r} "
            f"status={'red' if completed.returncode else 'green'} "
            f"last_line={summary!r}"
        )
        if completed.returncode != 0:
            errors.append(
                f"merge-tree: {command} is red on the merge tree\n"
                f"{_tail(completed.stdout + completed.stderr)}"
            )
    return errors


# --- rule 3: registry union --------------------------------------------------


def registry_entries(text: str, symbol: str) -> set[str]:
    """Module-level string collection ``symbol`` in a Python source text.

    Fail-closed: a non-literal or non-string element makes the union
    unverifiable, which is a red rather than a silent empty set.
    """

    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == symbol for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Dict):
            elements = list(value.keys)
        elif isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            elements = list(value.elts)
        else:
            raise RuleFailure(
                f"registry-union: {symbol} is not a literal collection; cannot verify the union"
            )
        entries: set[str] = set()
        for element in elements:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                entries.add(element.value)
            else:
                raise RuleFailure(
                    f"registry-union: {symbol} has a non-string entry "
                    f"({ast.dump(element)[:80]}); cannot verify the union"
                )
        return entries
    raise RuleFailure(f"registry-union: no module-level {symbol} in the registry file")


def registry_union_dropped(
    base: set[str], head: set[str], merged: set[str]
) -> list[str]:
    """Entries present on either side that the merge dropped, sorted."""

    return sorted((set(base) | set(head)) - set(merged))


def git_show(ref: str, path: str, cwd: Path) -> str | None:
    completed = git(["show", f"{ref}:{path}"], cwd, check=False)
    return completed.stdout if completed.returncode == 0 else None


def registry_union_errors(
    base_ref: str, cwd: Path, merged_root: Path, config_path: Path, remote: str = "origin"
) -> list[str]:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    ref, base_sha, source = resolve_base_tip(base_ref, cwd, remote)
    _measured(
        "registry-union", base_ref, base_sha, source, cwd, remote, merged_root=merged_root
    )
    errors: list[str] = []
    for registry in config["registries"]:
        path = registry["path"]
        symbol = registry["symbol"]
        base_text = git_show(base_sha, path, cwd)
        head_text = git_show("HEAD", path, cwd)
        merged_file = Path(merged_root) / path
        merged_text = (
            merged_file.read_text(encoding="utf-8") if merged_file.is_file() else None
        )
        base = registry_entries(base_text, symbol) if base_text is not None else set()
        head = registry_entries(head_text, symbol) if head_text is not None else set()
        merged = (
            registry_entries(merged_text, symbol) if merged_text is not None else set()
        )
        dropped = registry_union_dropped(base, head, merged)
        if dropped:
            errors.append(
                f"registry-union: {path}::{symbol} drops {len(dropped)} entry/entries "
                f"in the merge: {dropped}. merged must be a superset of base UNION head; "
                "restore every entry, or ship the shrink as its own change with a ruling"
            )
    return errors


# --- rule 4: Submitted-by identity -------------------------------------------


def strip_leading_comments(body: str) -> str | None:
    """Body after its BOM, whitespace and leading HTML comments.

    ``None`` means the leading comment never closes: the text cannot be read,
    which is a failure, never a skip.
    """

    text = (body or "").lstrip("\ufeff")
    while True:
        text = text.lstrip()
        if not text.startswith("<!--"):
            return text
        end = text.find("-->")
        if end == -1:
            return None
        text = text[end + 3 :]


def first_meaningful_line(body: str) -> str | None:
    """The first non-empty line after BOM, whitespace and leading HTML comments."""

    text = strip_leading_comments(body)
    if text is None:
        return None
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


# Rule 4 vocabulary -- the SAME seven verdicts e2e-verifier's classifier
# (instruments/submitted_by_classify.py, delivered 2026-10-09) emits, so this
# gate and the report-only survey stay comparable.  Before this, four distinct
# near-forms collapsed into one `missing` message, which sends the author
# looking for a line that is already there but wrong (#1344's measurement).
OK = "ok"
PRESENT_BUT_NOT_FIRST = "present-but-not-first-line"
MISSING_WRONG_CASE = "missing-wrong-case"
MISSING_NO_AGENT_PREFIX = "missing-no-agent-prefix"
MISSING_EMPTY_URI = "missing-empty-uri"
MISSING = "missing"
BLOCKED_UNCLOSED_COMMENT = "blocked-unclosed-comment"

_SUBMITTED_BY = "Submitted-by:"
_AGENT_PREFIX = "agent:"
_QUOTE_LIMIT = 160


def _bounded(text: str | None, limit: int = _QUOTE_LIMIT) -> str:
    """The offending line, quoted and bounded so the failure stays greppable."""

    if text is None:
        return "<none>"
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return repr(flat)
    return repr(flat[: limit - 1] + "\u2026")


def identity_state(body: str, required_line: str) -> tuple[str, str | None]:
    """The fine-grained state and the line the verdict is about.

    Deliberately mirrors e2e-verifier's classifier verdict-for-verdict
    (BOM/blank/leading-HTML-comment skipping, marker prefix, value checks, then
    the near-forms), with ONE deliberate divergence: `ok` still requires the
    configured ``required_line`` regex, so the template placeholder
    ``Submitted-by: agent:<URI>`` (which the prefix-level reader would accept)
    stays red.  The near-forms are reported by what was SEEN, so a wrong-case or
    empty-URI line is no longer indistinguishable from an absent one.
    """

    text = strip_leading_comments(body)
    if text is None:
        return BLOCKED_UNCLOSED_COMMENT, None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return MISSING, None
    pattern = re.compile(required_line)
    first = lines[0]
    if pattern.fullmatch(first):
        return OK, first
    if first.startswith(_SUBMITTED_BY):
        # The marker IS the first content line; name which part of the value is
        # wrong instead of reporting the whole line as absent.
        value = first[len(_SUBMITTED_BY) :].strip()
        if not value:
            return MISSING_EMPTY_URI, first
        if not value.startswith(_AGENT_PREFIX):
            return MISSING_NO_AGENT_PREFIX, first
        if not value[len(_AGENT_PREFIX) :].strip():
            return MISSING_EMPTY_URI, first
        # Prefix-level shape is fine, but required_line still refuses it (the
        # `<URI>` placeholder, or an internal space).  Keep it red.
        return MISSING, first
    if any(line.startswith(_SUBMITTED_BY) for line in lines[1:]):
        return PRESENT_BUT_NOT_FIRST, first
    if first.lower().startswith(_SUBMITTED_BY.lower()):
        return MISSING_WRONG_CASE, first
    return MISSING, first


def identity_reason(state: str, line: str | None, shape: str) -> str:
    """The operator-facing reason for a non-``ok`` state.

    Every near-form NAMES what was seen (the offending first line, quoted and
    bounded) and what was expected, so four distinct mistakes no longer produce
    one blind `missing` message.
    """

    if state == PRESENT_BUT_NOT_FIRST:
        return (
            f"present-but-not-first-line: a `{shape}` line exists later in the body, "
            f"but the first non-empty line is {_bounded(line)}; expected `{shape}` as "
            "the first non-empty line"
        )
    if state == MISSING_WRONG_CASE:
        return (
            f"missing-wrong-case: the first non-empty line is {_bounded(line)}; the "
            "marker is case-sensitive -- expected `Submitted-by:` (`submitted-by:` or "
            "`Submitted-By:` does not match)"
        )
    if state == MISSING_NO_AGENT_PREFIX:
        return (
            f"missing-no-agent-prefix: the first non-empty line is {_bounded(line)}; "
            "expected the value after `Submitted-by:` to start with `agent:`"
        )
    if state == MISSING_EMPTY_URI:
        return (
            f"missing-empty-uri: the first non-empty line is {_bounded(line)}; "
            "expected a non-empty agent URI after `agent:`"
        )
    if state == BLOCKED_UNCLOSED_COMMENT:
        return (
            "blocked-unclosed-comment: a leading HTML comment never closes, so the "
            f"identity line cannot be validated; expected `{shape}` as the first "
            "non-empty line (fail closed, never a skip)"
        )
    if line is not None and line.startswith(_SUBMITTED_BY):
        return (
            f"missing: the first non-empty line is {_bounded(line)}, which looks like a "
            f"`Submitted-by:` declaration but does not match `{shape}`; replace the "
            "placeholder (for example `<URI>`) with the submitting agent's URI"
        )
    return (
        f"missing: no `{shape}` line was found anywhere in the body "
        f"(the first non-empty line is {_bounded(line)})"
    )


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def identity_errors(
    body: str,
    config_path: Path,
    pr_number: str | None = None,
    created_at: str | None = None,
) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the Submitted-by rule.

    Deterministic transition, fixed by hq (2026-10-09):

    * created at/after ``cutoff``  -> hard red on ANY non-``ok`` state
      (``missing``, ``present-but-not-first-line`` and the near-forms);
    * created before ``cutoff``    -> **permanent** warn-only, never upgraded by
      a later push or new commit (the criterion is the PR creation time only);
    * ``upgrade_pr_numbers``       -> the only upgrade path, named explicitly by
      hq; ``exempt_pr_numbers`` is the surgical warn-only escape hatch and wins
      over an upgrade.
    """

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    state, line = identity_state(body, config["required_line"])
    if state == OK:
        return [], []
    shape = config["required_shape"]
    reason = identity_reason(state, line, shape)
    exempt = {str(value) for value in config.get("exempt_pr_numbers", [])}
    upgrade = {str(value) for value in config.get("upgrade_pr_numbers", [])}
    fix = (
        f"add `{shape}` as the body's first non-empty line (after leading "
        "HTML comments/BOM/whitespace). Forgejo's author is the shared account, "
        "so the body carries the identity."
    )
    if pr_number is not None and str(pr_number) in exempt:
        return [], [
            f"submitted-by: PR #{pr_number} is in the transition allowlist; "
            f"{reason}; warn only. {fix}"
        ]
    if pr_number is not None and str(pr_number) in upgrade:
        return [f"submitted-by: PR #{pr_number} is named for upgrade; {reason}. {fix}"], []
    cutoff = _aware(datetime.fromisoformat(str(config["cutoff"]).replace("Z", "+00:00")))
    created: datetime | None = None
    if created_at:
        try:
            created = _aware(datetime.fromisoformat(str(created_at).replace("Z", "+00:00")))
        except ValueError:
            created = None
    if created is not None and created < cutoff:
        return [], [
            f"submitted-by: PR created {created_at} is before the {config['cutoff']} "
            f"cutoff; {reason}; permanent warn-only (a later push does not upgrade "
            f"it). {fix}"
        ]
    return [f"submitted-by: {reason}. {fix}"], []


def source_branch_errors(base_ref: str, head_ref: str) -> list[str]:
    """Retain old workflow compatibility after release-first admission retires."""

    print(
        "source-branch: retired by dev-first flow; "
        f"base={base_ref!r} head={head_ref!r}; review and merge-tree gates still apply"
    )
    return []


def _print(errors: list[str], warnings: list[str]) -> int:
    for warning in warnings:
        print(f"WARNING {warning}")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="origin")
    sub = parser.add_subparsers(dest="rule", required=True)

    def with_root(rule: str):
        part = sub.add_parser(rule)
        part.add_argument("--base-ref", required=True)
        part.add_argument("--root", type=Path, default=Path.cwd())
        return part

    merge = with_root("merge-tree")
    merge.add_argument("--worktree", type=Path, required=True)
    union = with_root("registry-union")
    union.add_argument("--merged-root", type=Path, required=True)
    union.add_argument("--config", type=Path, default=REGISTRY_CONFIG)
    identity = sub.add_parser("submitted-by")
    identity.add_argument("--body-file", type=Path, required=True)
    identity.add_argument("--config", type=Path, default=IDENTITY_CONFIG)
    identity.add_argument("--pr-number", default=None)
    identity.add_argument("--created-at", default=None)
    source = sub.add_parser("source-branch")
    source.add_argument("--base-ref", required=True)
    source.add_argument("--head-ref", required=True)
    args = parser.parse_args(argv)

    try:
        if args.rule == "source-branch":
            return _print(source_branch_errors(args.base_ref, args.head_ref), [])
        if args.rule == "merge-tree":
            tree = materialise_merge_tree(args.base_ref, args.root, args.worktree, args.remote)
            return _print(merge_tree_gate_errors(tree), [])
        if args.rule == "registry-union":
            errors = registry_union_errors(
                args.base_ref, args.root, args.merged_root, args.config, args.remote
            )
            return _print(errors, [])
        if args.rule == "submitted-by":
            errors, warnings = identity_errors(
                args.body_file.read_text(encoding="utf-8"),
                args.config,
                args.pr_number,
                args.created_at,
            )
            return _print(errors, warnings)
    except RuleFailure as failure:
        return _print([str(failure)], [])
    raise RuleFailure(f"unhandled rule: {args.rule}")


if __name__ == "__main__":
    raise SystemExit(main())
