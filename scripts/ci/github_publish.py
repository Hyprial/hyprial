"""github-publish: push a denylist-filtered harness-bridge snapshot to GitHub.

This module is the single home of every publish-side constant (spec
2026-09-14, deliverable 1):

* :data:`SNAPSHOT_DENYLIST` -- the ONE list of paths that must not leave
  the intranet, each with its reason (the table itself lives in
  ``github_publish_policy/denylist.py`` for the line budget).  Everything else is published (Allen
  2026-11-03; design: docs/design/public-snapshot-denylist-2026-11-03.md).
  Enforced by re-reading the extracted tree, not by trusting the archive
  pathspec.  The workflow file never repeats these paths; a test pins it.
* :data:`SNAPSHOT_EXCLUDE_GLOBS` -- the same role for paths only a
  wildcard can express; same two enforcement points as the denylist
  (git pathspec and fnmatch both let ``*`` cross ``/``).  It also keeps
  ``.gitleaks.toml`` / ``.gitleaksignore`` out at any depth: gitleaks reads
  them from the tree it scans, so a published one could disable the gate.
* :data:`SNAPSHOT_REQUIRED` -- paths whose absence makes the public tree
  unbuildable; a missing one is red.
* :data:`INTRANET_RULES` -- the intranet-marker patterns for the grep gate.
* :data:`TRACK_TAGS` / :data:`VERSION_TAG_RE` / :data:`RETIRED_TRACK_TAGS` --
  the tag-name policy (``v*`` immutable, ``*-track`` movable, the old
  ``dev``/``nightly``/``stable`` names refused; Allen 2026-09-14: the
  ambiguity must be resolved before the first push).
* :data:`BOT_COMMIT_IDENT` -- the fixed bot author for snapshot commits on
  the GitHub mirror (never a personal identity).
* ``guistamp.write_gui_stamp`` -- the GUI provenance stamp written into
  every snapshot carrying ``gui/``; the snapshot has no ``.git``, and the
  stamp is the only provenance ``gui_build_hook`` accepts there.

Design notes (why this is one module): the spec forbids copying the
path policy into multiple places, and the tag policy must be the same object
for the workflow's plan step and the publish step -- so every consumer
reaches for these names instead of restating them.  The pure helper
implementations live in the ``github_publish_policy`` package beside this
module (structural-limit split, 2026-10-08) and are re-exported below;
this module keeps every constant, the CLI surface and all historical
helper names.  The ``crates/`` entries carry the native engine's cargo
sources (build ADR: the public snapshot carries the full build sources);
``crates/hyprial-py/tests/`` holds Python helpers, never cargo inputs.

Only the Python standard library is used: in CI this runs under the
container's system python3 before any uv environment exists.

Exit codes: 0 green, 1 gate/policy failure (job red), 2 usage error.
Gates are fail-closed: a missing/unreadable input is a failure, never a
pass (a missing gitleaks report is "unmeasurable", and unmeasurable is
red on a publish path).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.ci.github_publish_policy import (
        proxy_env as _proxy_env,
        intranet as _intranet,
        manifest as _manifest,
        netretry as _netretry,
        push as _push,
        guistamp as _guistamp,
        denylist as _denylist,
    )
except ModuleNotFoundError:
    # Direct ``python scripts/ci/github_publish.py`` execution puts scripts/ci
    # rather than the repository root at the front of sys.path.
    from github_publish_policy import (
        proxy_env as _proxy_env,
        intranet as _intranet,
        manifest as _manifest,
        netretry as _netretry,
        push as _push,
        guistamp as _guistamp,
        denylist as _denylist,
    )

try:
    from scripts import git_environment as _git_environment
    from scripts.git_environment import (
        hardened_git_environment,
        ignored_git_environment_warning,
    )
except ModuleNotFoundError:  # same direct-execution path as above
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts import git_environment as _git_environment
    from scripts.git_environment import (
        hardened_git_environment,
        ignored_git_environment_warning,
    )

# --- publish-side constants (single source; see module docstring) ---------

VERSION_FILE: str = "VERSION"

SNAPSHOT_DENYLIST: dict[str, str] = _denylist.SNAPSHOT_DENYLIST

SNAPSHOT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "sidecar/hyprial-tailcat/*_test.go",
    "sidecar/hyprial-tailcat/testdata/*",
    "sidecar/hyprial-tailcat/*/testdata/*",
    "*.gitleaks.toml",
    "*.gitleaksignore",
)

SNAPSHOT_REQUIRED: tuple[str, ...] = (
    "src/",
    "gui/package.json",
    "gui/package-lock.json",
    "sidecar/hyprial-tailcat/go.mod",
    "sidecar/hyprial-tailcat/go.sum",
    "crates/Cargo.toml",
    "crates/Cargo.lock",
    "crates/hyprial-core/",
    "crates/hyprial-py/",
    "pyproject.toml",
    "uv.lock",
    VERSION_FILE,
    "README.md",
    "LICENSE",
)

INTRANET_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("intranet-git-host", re.compile(rb"(?i)git\.internal\.hyprial\.com")),
    ("tsnet-hostname", re.compile(rb"(?i)\.ts\.net")),
    ("credential-client-id", re.compile(rb"(?i)oc_[0-9a-f]{20,}")),
    ("cgnat-range", re.compile(rb"100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.")),
    # Public product domains (owner decision 2026-11-06): agent identities at
    # agents.hyprial.com and Forgejo noreply addresses at code.hyprial.com are
    # not staff mailboxes, so they are not hits.
    ("staff-email", re.compile(rb"(?i)[A-Za-z0-9._%+-]+(?:@|%40)(?!(?:agents\.hyprial\.com|(?:users\.noreply\.)?code\.hyprial\.com)(?![A-Za-z0-9.-]))(?:[A-Za-z0-9-]+\.)*hyprial\.(?:com|ai)(?![A-Za-z0-9-])")),
    ("mdns-device-name", re.compile(rb"(?i)(?<![A-Za-z0-9_.-])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\.local(?![A-Za-z0-9_(-])")),
    ("lark-app-id", re.compile(rb"(?i)(?<![A-Za-z0-9_])cli_[0-9a-f]{16}(?![0-9a-f])")),
)

TRACK_TAGS: tuple[str, ...] = ("internal", "nightly", "stable")

#: Retired channel-tag names, mapped to the current name that replaced them.
#:
#: There are now TWO retired generations, and the rejection has to tell them
#: apart: ``dev`` (gen 1, renamed because it collided with the dev BRANCH) and
#: the ``*-track`` set (gen 2, the suffix that rename introduced and that
#: 2026-09-18 removed again). ⚠️ Note that ``nightly``/``stable`` appear in
#: neither -- gen 1 used those exact names and they are current again, so a
#: flat "old names" list would now reject the CURRENT names.
RETIRED_TRACK_TAGS: dict[str, str] = {
    "dev": "internal",
    "dev-track": "internal",
    "nightly-track": "nightly",
    "stable-track": "stable",
}
VERSION_TAG_RE = re.compile(r"^v\d[0-9A-Za-z.+-]*$")
SHA1_HEX_RE = re.compile(r"^[0-9a-f]{40}$")

BOT_COMMIT_IDENT = ("hyprial-ci", "ci@hyprial.com")

# --- bounded retry for transient network failures -----------------------------

ASKPASS_USERNAME = "x-access-token"

DEFAULT_TARGET_REMOTE = "https://github.com/Hyprial/hyprial.git"
DEFAULT_API_ORIGIN = "https://api.github.com"


class PublishError(Exception):
    """A loud, non-secret failure: the message may reach the job log."""


def base_version(source: Path) -> str:
    """The unstamped release version a snapshot of ``source`` builds.

    The path belongs to SNAPSHOT_REQUIRED above, so a workflow asks for the value
    instead of restating the path (spec 2026-09-14, deliverable 1).
    """

    try:
        text = (Path(source) / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PublishError(f"{VERSION_FILE} unreadable: {exc}") from exc
    if not text:
        raise PublishError(f"{VERSION_FILE} is empty")
    return text


# --- tag policy -------------------------------------------------------------


def tag_policy(tag: str) -> str:
    """Return ``"immutable"`` for ``v*`` and ``"movable"`` for track tags.

    Refuses everything else, with distinct messages for the old track tag
    names (they must never reach GitHub, Allen 2026-09-14).
    """
    if tag in RETIRED_TRACK_TAGS:
        # Name the generation and the replacement: "expected ..." alone leaves
        # the reader unable to tell a typo from a name that was correct last
        # week, and the two need different fixes.
        raise PublishError(
            f"refusing tag {tag!r}: retired channel-tag name "
            f"(current name: {RETIRED_TRACK_TAGS[tag]!r}); the mirror accepts "
            f"{'/'.join(TRACK_TAGS)} plus v<version>"
        )
    if tag in TRACK_TAGS:
        return "movable"
    if VERSION_TAG_RE.match(tag):
        return "immutable"
    raise PublishError(
        f"refusing tag {tag!r}: expected v<version> or one of "
        f"{'/'.join(TRACK_TAGS)}"
    )


# --- snapshot ---------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotResult:
    out: Path
    files: tuple[tuple[str, int], ...]  # (relative path, size in bytes)
    denied_entries: tuple[str, ...]


def _git(source: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, check=True
    )


def missing_required_entries(paths: Iterable[str]) -> tuple[str, ...]:
    """Return the :data:`SNAPSHOT_REQUIRED` entries absent from ``paths``.

    A missing required entry means the public tree cannot build or install,
    so it is a red, not a silent skip.  Directory entries end in ``/``.
    """
    files = frozenset(paths)
    return tuple(
        entry
        for entry in SNAPSHOT_REQUIRED
        if not (
            any(f.startswith(entry) for f in files)
            if entry.endswith("/")
            else entry in files
        )
    )


def excluded_entries(paths: Iterable[str]) -> tuple[str, ...]:
    """Return the ``paths`` that the denylist forbids, sorted.

    Pure and separately tested (implementation in
    ``github_publish_policy.manifest``): answer "did anything forbidden land
    on disk" without running git, never trusting the archive step.
    """
    return _manifest.excluded_entries(
        paths, SNAPSHOT_DENYLIST, SNAPSHOT_EXCLUDE_GLOBS
    )


def build_snapshot(source: Path, rev: str, out: Path) -> SnapshotResult:
    """Materialize the tree of ``rev`` minus the denylist into ``out``.

    One ``git archive`` call with ``:(exclude)`` pathspecs; ``out`` must not
    exist, so a stale directory can never be mistaken for a fresh snapshot.
    The extracted tree is then re-read twice, fail-closed: nothing denied may
    be on disk, and everything required must be.

    When the extraction carries the ``gui/`` tree, the GUI provenance stamp
    is written before the manifest is listed: the snapshot has no ``.git``,
    and the stamp is the only provenance the wheel build accepts there.
    Stamping is a pure function of the pinned revision, so the publish
    step's pristine re-extraction reproduces the scanned stamp byte-for-byte.
    """
    out.mkdir(parents=True)
    archive = subprocess.run(
        [
            "git", "-C", str(source), "archive", "--format=tar", rev, "--", ".",
            *(f":(exclude){entry.rstrip('/')}" for entry in SNAPSHOT_DENYLIST),
            *(f":(exclude){pattern}" for pattern in SNAPSHOT_EXCLUDE_GLOBS),
        ],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise PublishError(
            "git archive failed: "
            + archive.stderr.decode("utf-8", "surrogateescape").strip()
        )
    import tarfile

    with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
        tmp.write(archive.stdout)
        tmp.flush()
        with tarfile.open(tmp.name) as tar:
            try:
                tar.extractall(out, filter="data")
            except TypeError:  # python < 3.11.4 has no extraction filters
                tar.extractall(out)
    if (out / "gui").is_dir():
        commit = subprocess.check_output(
            ["git", "-C", str(source), "show", "-s", "--format=%H", rev],
            text=True,
        ).strip()
        _guistamp.write_gui_stamp(
            out, commit=commit, remote=DEFAULT_TARGET_REMOTE, error=PublishError
        )
    entries: list[tuple[str, int]] = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = path.relative_to(out).as_posix()
        entries.append((rel, path.stat().st_size))
    if not entries:
        raise PublishError("snapshot is empty after extraction")
    # The pathspec above is the mechanism; THIS is the guard.  We do not rely
    # on it because ``:(exclude)`` is pathspec magic whose support varies by
    # git version, and a silently-ignored pathspec publishes the tree while
    # every other reading stays green -- the failure would be invisible right
    # up until the files are public.  Re-reading what actually landed on disk
    # is version-independent and fails closed.
    leaked = excluded_entries(rel for rel, _size in entries)
    if leaked:
        raise PublishError(
            "excluded paths present in snapshot (publish refused): "
            + ", ".join(leaked[:10])
            + (f" … and {len(leaked) - 10} more" if len(leaked) > 10 else "")
        )
    missing = missing_required_entries(rel for rel, _size in entries)
    if missing:
        raise PublishError(
            f"required snapshot entries missing from {rev}: {', '.join(missing)}"
        )
    return SnapshotResult(out, tuple(entries), tuple(SNAPSHOT_DENYLIST))


def print_manifest(result: SnapshotResult) -> None:
    for rel, size in result.files:
        print(f"snapshot-file\t{rel}\t{size}")
    total = sum(size for _, size in result.files)
    print(f"snapshot: {len(result.files)} files, {total} bytes")
    print(f"snapshot denylist: {', '.join(result.denied_entries)}")


def parse_snapshot_manifest(text: str) -> tuple[str, ...]:
    """Re-export: implementation is github_publish_policy.manifest."""
    return _manifest.parse_snapshot_manifest(text, PublishError)


def _read_scanned_manifest(manifest: Path) -> tuple[str, ...]:
    """Re-export: implementation is github_publish_policy.manifest."""
    return _manifest.read_scanned_manifest(manifest, PublishError)


# --- intranet identifier gate ----------------------------------------------


AllowlistEntry = _intranet.AllowlistEntry
IntranetScan = _intranet.IntranetScan


def parse_allowlist(text: str) -> tuple[AllowlistEntry, ...]:
    return _intranet.parse_allowlist(text, INTRANET_RULES, PublishError)


def scan_intranet_markers(
    tree: Path, allowlist: tuple[AllowlistEntry, ...], private_markers: str
) -> IntranetScan:
    rules = (*INTRANET_RULES, _intranet.private_marker_rule(private_markers, PublishError))
    return _intranet.scan_intranet_markers(tree, allowlist, rules)


def stale_allowlist_entries(
    allowlist: tuple[AllowlistEntry, ...], scan: IntranetScan, tree_paths: set[str]
) -> list[str]:
    return _intranet.stale_allowlist_entries(allowlist, scan, tree_paths)


def parse_leaks_report(raw: bytes) -> list[dict[str, object]]:
    return _intranet.parse_leaks_report(raw, PublishError)


def describe_finding(finding: dict[str, object]) -> str:
    return _intranet.describe_finding(finding)


# --- publish ----------------------------------------------------------------


def _write_askpass(directory: Path, token: str) -> Path:
    askpass = directory / "ghpub-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\n"
        f"case \"$1\" in\n"
        f"  *Username*) printf '%s\\n' {ASKPASS_USERNAME!r} ;;\n"
        f"  *Password*) printf '%s\\n' \"$GH_PUBLISH_TOKEN\" ;;\n"
        f"esac\n",
        encoding="utf-8",
    )
    os.chmod(askpass, 0o700)
    return askpass


# --- retry/network helpers (moved to github_publish_policy.netretry) ---------
# Pure-move split (2026-10-08, structural limit): the bodies live in the
# policy module and are re-exported here under their historical names so
# existing imports and monkeypatch seams keep working.  ⚠️ ``_run_git_network``
# resolves ``_run`` in netretry's namespace, so tests faking ``_run`` for the
# retry loop patch ``_netretry._run``; ``publish_snapshot`` keeps calling
# this module's ``_run`` alias, so faking ``github_publish._run`` still
# works on the publish path.
NETWORK_RETRY_ATTEMPTS = _netretry.NETWORK_RETRY_ATTEMPTS
NETWORK_RETRY_BACKOFF = _netretry.NETWORK_RETRY_BACKOFF
_URL_CREDENTIAL = _netretry._URL_CREDENTIAL
_run = _netretry._run
_git_failure_detail = _netretry._git_failure_detail
_backoff_for = _netretry._backoff_for
is_transient_git_network_error = _netretry.is_transient_git_network_error
_run_git_network = _netretry._run_git_network
_is_transient_network_error = _netretry._is_transient_network_error


def _backoff_schedule(env: dict[str, str]) -> tuple[float, ...]:
    """Re-export: implementation is github_publish_policy.netretry."""
    return _netretry.backoff_schedule(env, PublishError)


def api_credential_check(
    api_origin: str,
    repo_slug: str,
    env: dict[str, str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    backoff: tuple[float, ...] = NETWORK_RETRY_BACKOFF,
    opener: Callable[..., Any] | None = None,
) -> None:
    """Re-export: implementation is github_publish_policy.netretry."""
    _netretry.api_credential_check(
        api_origin, repo_slug, env,
        sleep=sleep, backoff=backoff, opener=opener, error_type=PublishError,
    )


def _proxy_keys_in_git_env(
    requested_env: dict[str, str], git_env: dict[str, str]
) -> list[str]:
    """代理是否真的进了 git 环境（实现在 github_publish_policy.proxy_env）。"""
    return _proxy_env.keys_in_git_env(requested_env, git_env, PublishError)


def _publish_git_env(
    source: dict[str, str], *, askpass: Path, token: str
) -> dict[str, str]:
    """Build the environment for authenticated GitHub mirror commands."""

    try:
        return hardened_git_environment(
            source,
            askpass=askpass,
            credential_variables={"GH_PUBLISH_TOKEN": token},
            # 到 github.com 只有经宿主 mihomo 这一条可用路径（直连实测 3/10 可用、其余挂死 40 s）。
            # 不带上代理变量，git 就是裸连 —— 2026-10-30 run 40868 的 clone 三连失败就是这个。
            propagate_proxy=True,
        )
    except _git_environment.ProxyCredentialError as error:
        # 加固层的拒绝要落到本模块的错误域，调用方只需处理 PublishError。
        raise PublishError(str(error)) from error


def _warn_ignored_git_environment(warning: str | None) -> None:
    if warning:
        print(warning, file=sys.stderr)


def publish_snapshot(
    source: Path,
    source_sha: str,
    manifest: Path,
    tag: str,
    *,
    dry_run: bool,
    target_remote: str,
    api_origin: str,
    repo_slug: str,
    env: dict[str, str],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Publish the scanned snapshot of ``source_sha``; never the scan dir.

    The publish source is a pristine RE-EXTRACTION of the denylist-filtered tree at
    ``source_sha`` (spec 2026-10-08).  The wheel build runs in the scanned
    snapshot dir and writes artefacts there -- ``__pycache__`` .pyc bytes, hook
    outputs -- AFTER the intranet and gitleaks gates have read it, so copying
    that directory wholesale ships bytes no gate ever saw (``internal``
    9e148506 and ``v0.5.3`` 40825d78 both did).  The re-extraction is a pure
    function of the immutable commit, so anything written into the scan dir
    between the scans and this call can never reach the mirror.  The
    pre-commit purity gate below then verifies the staged path set equals the
    scanned manifest, fail-closed.

    ``dry_run`` walks everything except the pushes (spec: dry-run must not
    push, and must still prove the credentials readable).
    """
    mutability = tag_policy(tag)
    if not SHA1_HEX_RE.match(source_sha):
        raise PublishError(
            f"--source-sha must be a 40-hex commit id, got {source_sha!r}"
        )
    if not env.get("GH_PUBLISH_TOKEN"):
        raise PublishError(
            "secret GH_PUBLISH_TOKEN is empty or missing; refusing to publish"
        )
    scanned_paths = _read_scanned_manifest(manifest)

    backoff = _backoff_schedule(env)
    workdir = Path(tempfile.mkdtemp(prefix="ghpub-mirror-"))
    askpass = _write_askpass(workdir, env["GH_PUBLISH_TOKEN"])
    git_env = hardened_git_environment(env)
    ignored_git_warning = ignored_git_environment_warning(env)
    authenticated_git_env = _publish_git_env(
        env,
        askpass=askpass,
        token=env["GH_PUBLISH_TOKEN"],
    )
    # Diagnostic state, defined before the try so the finally can always report
    # it.  The three outcomes we have to tell apart -- a new snapshot commit, a
    # tag move over an identical tree, and a failed push -- are distinguishable
    # only by these values, and the failing case is the one that raises before
    # any success-path print.
    new_commit = ""
    commit_created = False
    try:
        rev_ok = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--verify", "-q",
             f"{source_sha}^{{commit}}"],
            capture_output=True,
        )
        if rev_ok.returncode != 0:
            raise PublishError(
                f"source commit {source_sha} not found in {source}"
            )
        pristine = workdir / "pristine-snapshot"
        snapshot = build_snapshot(source, source_sha, pristine)
        print(
            f"publish source: pristine re-extraction of {source_sha} "
            f"({len(snapshot.files)} files)"
        )
        api_credential_check(
            api_origin, repo_slug, env, sleep=sleep, backoff=backoff
        )

        _warn_ignored_git_environment(ignored_git_warning)
        refs = _run_git_network(
            ["git", "ls-remote", target_remote, "refs/heads/main", f"refs/tags/{tag}"],
            env=authenticated_git_env,
            label="git ls-remote",
            token=env["GH_PUBLISH_TOKEN"],
            sleep=sleep,
            backoff=backoff,
        )
        if refs.returncode != 0:
            detail = _git_failure_detail(refs.stderr, env["GH_PUBLISH_TOKEN"])
            raise PublishError(f"git ls-remote failed against the target:\n{detail}")
        main_head = ""
        tag_dest = ""
        for line in refs.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == "refs/heads/main":
                main_head = sha
            elif ref == f"refs/tags/{tag}":
                tag_dest = sha
        print(f"target state: main={'<absent>' if not main_head else main_head} "
              f"tag={tag_dest or '<absent>'}")
        already_published = _push.existing_immutable_tag(tag, mutability, tag_dest, main_head, PublishError)

        mirror = workdir / "mirror"
        # 先**独立**做检查（缺失即 PublishError），再打印证据:
        # 如果把检查留在 print 的实参里，将来删掉这行日志就等于删掉了检查。
        proxy_keys = _proxy_keys_in_git_env(env, authenticated_git_env)
        print(f"git network: proxy_env={','.join(proxy_keys) or 'NONE'}")
        _warn_ignored_git_environment(ignored_git_warning)
        clone = _run_git_network(
            # 只要 main 的一个提交: 目标仓历史对本流程没有信息（提交由本次新建，
            # tag 只做移动）。浅克隆把 payload 从"全历史"压到"一棵树"，
            # 同时让一次 TLS 中断要重传的量小到 5 s 级（2026-10-30 实测 depth1 4-5 s）。
            ["git", "clone", "--depth", "1", "--single-branch", target_remote, str(mirror)],
            env=authenticated_git_env,
            label="git clone",
            token=env["GH_PUBLISH_TOKEN"],
            sleep=sleep,
            backoff=backoff,
            # A retried clone must start from nothing: git refuses to clone
            # into the partial directory the failed attempt left behind.
            cleanup=lambda: shutil.rmtree(mirror, ignore_errors=True),
        )
        if clone.returncode != 0:
            raise PublishError(
                "git clone of the target failed:\n"
                + _git_failure_detail(clone.stderr, env["GH_PUBLISH_TOKEN"])
            )

        if main_head:
            checkout = _run(
                ["git", "checkout", "-q", "-B", "publish", main_head],
                env=git_env, cwd=mirror,
            )
            if checkout.returncode != 0:
                raise PublishError(
                    "cannot check out target main head: "
                    + (checkout.stderr.strip() or "unknown")
                )
            head_tree = _run(
                ["git", "rev-parse", "HEAD^{tree}"], env=git_env, cwd=mirror
            ).stdout.strip()
        else:
            # First push into an empty mirror repo: the snapshot commit
            # becomes the root of main (parent = none by necessity).
            sym = _run(
                ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                env=git_env, cwd=mirror,
            )
            if sym.returncode != 0:
                raise PublishError(
                    "cannot prepare main on the empty target: "
                    + (sym.stderr.strip() or "unknown")
                )
            head_tree = ""

        # Replace the worktree with the pristine re-extraction, then compare
        # trees before deciding whether a commit is needed at all.
        for child in mirror.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for item in pristine.iterdir():
            if item.is_dir():
                shutil.copytree(item, mirror / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, mirror / item.name)
        add = _run(["git", "add", "-A"], env=git_env, cwd=mirror)
        if add.returncode != 0:
            raise PublishError("git add of the snapshot failed")
        # ⛔ Final purity gate (hq-adjutant 2026-10-08): the staged path set
        # must equal the scanned snapshot manifest EXACTLY -- an extra path is
        # a file no gate ever scanned, a missing one is a scanned file that
        # silently vanished.  Either fails the job, naming the paths, before
        # any commit or push: a future build hook that writes a new file into
        # the publish stream is blocked here, not shipped.
        listed = _run(["git", "ls-files", "-z"], env=git_env, cwd=mirror)
        if listed.returncode != 0:
            raise PublishError("git ls-files of the staged snapshot failed")
        staged_paths = {p for p in listed.stdout.split("\0") if p}
        purity_error = _manifest.staged_purity_error(staged_paths, scanned_paths)
        if purity_error:
            raise PublishError(purity_error)
        print(
            f"purity gate: staged tree == scanned manifest "
            f"({len(scanned_paths)} paths)"
        )
        staged_tree = _run(
            ["git", "write-tree"], env=git_env, cwd=mirror
        ).stdout.strip()

        if already_published:
            new_commit = tag_dest
            return _push.report_already_published(tag, tag_dest, staged_tree, head_tree, PublishError)

        if head_tree and staged_tree == head_tree:
            print(
                f"tree {staged_tree} identical to target main head; "
                "no new commit, tag move only"
            )
            new_commit = main_head
            commit_created = False
        else:
            commit_env = {
                **git_env,
                "GIT_AUTHOR_NAME": BOT_COMMIT_IDENT[0],
                "GIT_AUTHOR_EMAIL": BOT_COMMIT_IDENT[1],
                "GIT_COMMITTER_NAME": BOT_COMMIT_IDENT[0],
                "GIT_COMMITTER_EMAIL": BOT_COMMIT_IDENT[1],
            }
            message = (
                f"publish {tag} from HyprialOS/harness-bridge\n\n"
                f"source: HyprialOS/harness-bridge@{source_sha}\n"
                f"tag: {tag}\n"
            )
            commit = _run(
                ["git", "commit", "-q", "-m", message], env=commit_env, cwd=mirror
            )
            if commit.returncode != 0:
                raise PublishError(
                    "snapshot commit failed: "
                    + (commit.stderr.strip() or commit.stdout.strip() or "unknown")
                )
            new_commit = _run(
                ["git", "rev-parse", "HEAD"], env=git_env, cwd=mirror
            ).stdout.strip()
            commit_created = True
            print(
                f"snapshot commit {new_commit} "
                f"(parent {main_head or '<root>'}, tree {staged_tree})"
            )

        if dry_run:
            print(
                f"github-publish: mode=dry-run tag={tag} mutability={mutability} "
                f"commit_created={str(commit_created).lower()} "
                f"would_point_tag_at={new_commit} pushed=no"
            )
            return 0

        if commit_created:
            _warn_ignored_git_environment(ignored_git_warning)
            new_commit, commit_created = _push.push_main_with_lease(
                mirror=mirror, expected=main_head, candidate=new_commit,
                staged_tree=staged_tree, env=authenticated_git_env,
                token=env["GH_PUBLISH_TOKEN"], sleep=sleep, backoff=backoff,
                run=_run, run_network=_run_git_network,
                failure_detail=_git_failure_detail, error_type=PublishError,
            )

        _warn_ignored_git_environment(ignored_git_warning)
        _push.push_tag_with_retry(
            mirror=mirror, target_remote=target_remote, tag=tag,
            intended=new_commit, previous=tag_dest,
            movable=mutability == "movable", env=authenticated_git_env,
            token=env["GH_PUBLISH_TOKEN"], sleep=sleep, backoff=backoff,
            attempts=NETWORK_RETRY_ATTEMPTS, run=_run,
            run_network=_run_git_network,
            transient=is_transient_git_network_error,
            backoff_for=_backoff_for, failure_detail=_git_failure_detail,
            error_type=PublishError,
        )

        _warn_ignored_git_environment(ignored_git_warning)
        verify = _run_git_network(
            ["git", "ls-remote", target_remote, f"refs/tags/{tag}"],
            env=authenticated_git_env, label="post-push tag verification",
            token=env["GH_PUBLISH_TOKEN"], sleep=sleep, backoff=backoff,
        )
        moved = verify.stdout.split()[0] if verify.stdout.strip() else ""
        if moved != new_commit:
            raise PublishError(
                f"post-push verification: refs/tags/{tag} is "
                f"{moved or '<absent>'}, expected {new_commit}"
            )
        return 0
    finally:
        # ⛔ Do not move this back onto the success path.  A diagnostic printed
        # only when the run succeeds does not report *what happened*, it reports
        # *whether it went well* -- which we already know from the exit code.
        # "push of refs/heads/main failed" is exactly the outcome this line has
        # to name, and that path raises before any success-path print.
        # Nothing here may raise: a diagnostic that dies on the failing run is
        # no diagnostic at all, which is why new_commit/commit_created are
        # initialised above rather than assumed to exist.
        print(
            f"github-publish: outcome tag={tag} mutability={mutability} "
            f"dry_run={str(dry_run).lower()} "
            f"commit={new_commit or '<not reached>'} "
            f"commit_created={str(commit_created).lower()}"
        )
        if askpass.exists():
            askpass.unlink()
        shutil.rmtree(workdir, ignore_errors=True)
        print("credential cleanup: askpass removed; no git-config credentials written")


# --- CLI --------------------------------------------------------------------


def _cmd_snapshot(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    rev = args.rev
    check = _git(source, "rev-parse", "--verify", f"{rev}^{{commit}}")
    if check.returncode != 0:
        print(f"revision {rev!r} not found in {source}", file=sys.stderr)
        return 1
    result = build_snapshot(source, check.stdout.decode().strip(), Path(args.out))
    print_manifest(result)
    return 0


def _cmd_intranet_scan(args: argparse.Namespace) -> int:
    tree = Path(args.tree)
    allowlist_path = Path(args.allowlist)
    try:
        allowlist_text = allowlist_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"allowlist unreadable: {exc}", file=sys.stderr)
        return 1
    try:
        allowlist = parse_allowlist(allowlist_text)
        markers = Path(args.private_markers).read_text(encoding="utf-8")
        scan = scan_intranet_markers(tree, allowlist, markers)
    except (PublishError, OSError) as exc:
        print(f"allowlist config error: {exc}", file=sys.stderr)
        return 1
    tree_paths = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    stale = stale_allowlist_entries(allowlist, scan, tree_paths)
    for line in stale:
        print(line)
    for line in scan.exemptions:
        print(f"intranet-gate exempt {line}")
    for hit in scan.hits:
        print(f"intranet-gate HIT {hit}")
    print(
        f"intranet-gate: {len(scan.hits)} non-exempt hit(s), "
        f"{len(scan.exemptions)} exempt hit(s), {len(stale)} stale entry(ies), "
        f"{scan.scanned} files scanned"
    )
    # Stale entries are red, not a printed note: the old behaviour put the
    # line in a GREEN job's log, where nothing ever made anyone read it.
    return 1 if (scan.hits or stale) else 0


def _cmd_leaks(args: argparse.Namespace) -> int:
    try:
        raw = Path(args.report).read_bytes()
    except OSError as exc:
        print(f"gitleaks report unreadable: {exc}", file=sys.stderr)
        return 1
    try:
        findings = parse_leaks_report(raw)
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for finding in findings:
        print(f"gitleaks HIT {describe_finding(finding)}")
    print(f"gitleaks-gate: {len(findings)} finding(s) (RuleID and file only; "
          "matched values are never printed)")
    return 1 if findings else 0


def _cmd_version(args: argparse.Namespace) -> int:
    try:
        print(base_version(Path(args.source)))
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    try:
        print(tag_policy(args.tag))
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    try:
        return publish_snapshot(
            Path(args.source).resolve(),
            args.source_sha,
            Path(args.manifest),
            args.tag,
            dry_run=args.dry_run,
            target_remote=args.target,
            api_origin=args.api_origin,
            repo_slug=args.repo_slug,
            env=env,
        )
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _cmd_project_version(args: argparse.Namespace) -> int:
    """Compatibility spelling retained for the integration branch's callers."""
    return _cmd_version(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github_publish.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="materialize the denylist-filtered tree")
    p.add_argument("--source", default=".", help="harness-bridge worktree/git dir")
    p.add_argument("--rev", required=True)
    p.add_argument("--out", required=True, help="directory to create (must not exist)")
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("intranet-scan", help="intranet identifier gate")
    p.add_argument("--tree", required=True)
    p.add_argument("--allowlist", required=True)
    p.add_argument("--private-markers", required=True, help="unpublished real-name list")
    p.set_defaults(func=_cmd_intranet_scan)

    p = sub.add_parser("leaks", help="gitleaks report gate (redacted)")
    p.add_argument("--report", required=True)
    p.set_defaults(func=_cmd_leaks)

    p = sub.add_parser("version", help="print the release version a snapshot stamps")
    p.add_argument("--source", default=".", help="harness-bridge worktree/git dir")
    p.set_defaults(func=_cmd_version)

    p = sub.add_parser("policy", help="print mutability for a tag, refuse invalid")
    p.add_argument("--tag", required=True)
    p.set_defaults(func=_cmd_policy)

    p = sub.add_parser("project-version", help="print the canonical project version")
    p.add_argument("--source", default=".")
    p.set_defaults(func=_cmd_project_version)

    p = sub.add_parser(
        "publish",
        help="publish the pristine re-extraction of --source-sha and move the tag",
    )
    p.add_argument(
        "--source", default=".",
        help="harness-bridge worktree/git dir holding the source commit",
    )
    p.add_argument(
        "--manifest", required=True,
        help="the scanned snapshot manifest (the snapshot step's teed output)",
    )
    p.add_argument("--tag", required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--dry-run", default="false")
    p.add_argument("--target", default=os.environ.get("GH_TARGET_REMOTE", DEFAULT_TARGET_REMOTE))
    p.add_argument("--api-origin", default=os.environ.get("GH_API_ORIGIN", DEFAULT_API_ORIGIN))
    p.add_argument("--repo-slug", default=os.environ.get("GH_TARGET_REPO", "Hyprial/hyprial"))
    p.set_defaults(func=_cmd_publish)

    args = parser.parse_args(argv)
    if getattr(args, "dry_run", None) is not None and isinstance(args.dry_run, str):
        args.dry_run = args.dry_run.strip().lower() in {"1", "true", "yes"}
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
