"""Race-safe branch publication and bounded tag-push retry."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


def _ref_sha(stdout: str, ref: str) -> str:
    for line in stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return ""


def push_main_with_lease(
    *,
    mirror: Path,
    expected: str,
    candidate: str,
    staged_tree: str,
    env: dict[str, str],
    token: str,
    sleep: Callable[[float], None],
    backoff: tuple[float, ...],
    run: Callable[..., subprocess.CompletedProcess[str]],
    run_network: Callable[..., subprocess.CompletedProcess[str]],
    failure_detail: Callable[[str, str], str],
    error_type: type[Exception],
) -> tuple[str, bool]:
    """Push main only if it still equals ``expected``.

    A rejected lease gets one logical re-read.  If the winner published the
    same tree, its commit becomes the tag target.  Different content is a hard
    race failure; this function never rebases or blindly retries the branch.
    """

    lease = f"--force-with-lease=refs/heads/main:{expected}"
    pushed = run(
        ["git", "push", lease, "origin", "HEAD:refs/heads/main"],
        env=env,
        cwd=mirror,
    )
    if pushed.returncode == 0:
        print(f"pushed refs/heads/main -> {candidate}")
        return candidate, True

    reread = run_network(
        ["git", "-C", str(mirror), "fetch", "--no-tags", "origin", "refs/heads/main"],
        env=env,
        label="main lease re-read",
        token=token,
        sleep=sleep,
        backoff=backoff,
    )
    if reread.returncode != 0:
        raise error_type(
            "push of refs/heads/main failed and the one allowed re-read failed:\n"
            + failure_detail(pushed.stderr, token)
            + "\nre-read: "
            + failure_detail(reread.stderr, token)
        )
    actual = run(
        ["git", "rev-parse", "FETCH_HEAD"], env=env, cwd=mirror
    ).stdout.strip()
    actual_tree = run(
        ["git", "rev-parse", "FETCH_HEAD^{tree}"], env=env, cwd=mirror
    ).stdout.strip()
    if expected and actual == expected:
        # The remote did not move, so the lease was not lost: the push itself
        # failed (transport, auth, size...). Report git's own error.
        raise error_type(
            "push of refs/heads/main failed with the remote unchanged at "
            f"{actual}; candidate {candidate} was not pushed:\n"
            + failure_detail(pushed.stderr, token)
        )
    if actual_tree == staged_tree:
        print(
            f"main lease lost to {actual}, whose tree {actual_tree} matches the "
            "staged tree; skipping main push"
        )
        return actual, False
    raise error_type(
        "push of refs/heads/main rejected by concurrent publication: "
        f"expected remote {expected or '<absent>'}, now {actual}; "
        f"candidate {candidate} was not pushed (remote tree {actual_tree}, "
        f"staged tree {staged_tree})"
    )


def push_tag_with_retry(
    *,
    mirror: Path,
    target_remote: str,
    tag: str,
    intended: str,
    previous: str,
    movable: bool,
    env: dict[str, str],
    token: str,
    sleep: Callable[[float], None],
    backoff: tuple[float, ...],
    attempts: int,
    run: Callable[..., subprocess.CompletedProcess[str]],
    run_network: Callable[..., subprocess.CompletedProcess[str]],
    transient: Callable[[str], bool],
    backoff_for: Callable[[int, tuple[float, ...]], float],
    failure_detail: Callable[[str, str], str],
    error_type: type[Exception],
) -> None:
    """Push a tag with bounded retry and a remote-state check before retry."""

    ref = f"refs/tags/{tag}"
    argv = ["git", "push"]
    if movable:
        argv.append(f"--force-with-lease={ref}:{previous}")
    argv += ["origin", f"{intended}:{ref}"]
    for attempt in range(1, attempts + 1):
        result = run(argv, env=env, cwd=mirror)
        if result.returncode == 0:
            print(
                f"pushed {ref} -> {intended} "
                f"(force={str(movable).lower()}, attempt={attempt}/{attempts})"
            )
            return
        if attempt == attempts or not transient(result.stderr):
            raise error_type(
                f"push of {ref} failed after {attempt} attempt(s):\n"
                + failure_detail(result.stderr, token)
            )

        check = run_network(
            ["git", "ls-remote", target_remote, ref],
            env=env,
            label=f"{ref} retry check",
            token=token,
            sleep=sleep,
            backoff=backoff,
        )
        if check.returncode != 0:
            raise error_type(
                f"push of {ref} was ambiguous and retry check failed:\n"
                + failure_detail(check.stderr, token)
            )
        current = _ref_sha(check.stdout, ref)
        if current == intended:
            print(
                f"push of {ref} reported failure but remote already points at "
                f"{intended}; treating attempt {attempt}/{attempts} as succeeded"
            )
            return
        if current != previous:
            kind = "immutable v* tag" if not movable else "movable tag lease"
            raise error_type(
                f"refusing retry for {kind} {ref}: remote is "
                f"{current or '<absent>'}, expected {previous or '<absent>'}; "
                f"intended {intended}"
            )
        wait = backoff_for(attempt, backoff)
        detail = failure_detail(result.stderr, token).splitlines()[-1]
        print(
            f"push {ref}: attempt {attempt}/{attempts} failed ({detail}); "
            f"remote unchanged; retrying in {wait:g}s"
        )
        sleep(wait)
    raise AssertionError("unreachable")


def existing_immutable_tag(
    tag: str, mutability: str, tag_dest: str, main_head: str,
    error_type: type[Exception],
) -> bool:
    """True when an immutable tag already exists and may be an identical re-publish.

    It may only be "published again" when it is the target main AND that main
    carries exactly this snapshot's tree (``report_already_published`` checks
    the tree after staging).  Anything else is a rewrite and is refused.
    2026-10-30: v0.5.5.2.2's first run pushed the tag, a later step failed on a
    network blip, and every retry was refused, so the release could never
    reach its track step.
    """

    if mutability != "immutable" or not tag_dest:
        return False
    if tag_dest != main_head:
        raise error_type(
            f"refusing to push {tag}: already exists on the target at "
            f"{tag_dest}, not the target main {main_head or '<absent>'}; "
            "v* tags are immutable"
        )
    return True


def report_already_published(
    tag: str, tag_dest: str, staged_tree: str, head_tree: str,
    error_type: type[Exception],
) -> int:
    """Accept an identical re-publish of an immutable tag; refuse any other tree."""

    if staged_tree != head_tree:
        raise error_type(
            f"refusing to push {tag}: already exists on the target with tree "
            f"{head_tree}, this snapshot is {staged_tree}; v* tags are immutable"
        )
    print(
        f"already-published refs/tags/{tag} -> {tag_dest} "
        f"(identical tree {staged_tree}; immutable tag left unchanged)"
    )
    return 0
