#!/usr/bin/env python3
"""Harness Bridge -> Homepage public changelog sync.

Task 1 (pure contract): parse the reviewed JSON-shaped manifest, resolve the
immutable tag/source/metadata facts, verify the changelog section, and render
the canonical Homepage record as bytes. Task 2 (idempotent client): classify
the existing candidate, create it at most once, and reuse an identical open or
merged candidate; every write is gated on a sealed ``ValidatedRelease``.

Task 3 (dispatch): a candidate is always exactly ONE commit on ``draft``. A
retry or update rewrites the branch with ``git push --force-with-lease``
against the expected old head; it never appends a second commit. After the
push, the commit SHA from ``git rev-parse HEAD`` is the authoritative head: the
lease just confirmed it and the branch points at it. Do NOT re-read the pull
request to discover the head -- the API may still report the previous SHA and
the dispatch would carry a stale head; the guard reads compare by the SHA it is
given. Sync then dispatches ``homepage-release-origin-guard`` with that
head/base, and never reads the guard's status. It suppresses the dispatch only
when the diff touches ``.forgejo/workflows/**`` (either side of a rename) or
the file list cannot be read/is a full page. See
docs/guides/homepage-changelog-sync.md.

The sync never writes a commit status and never receives the guard-status
token. The trusted guard on Homepage's protected ``ci/release-origin-guard``
ref is the only identity allowed to post ``homepage-release-origin-guard``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Protocol

try:
    from scripts.git_environment import (
        hardened_git_environment,
        ignored_git_environment_warning,
    )
except ModuleNotFoundError:  # direct ``python scripts/release/homepage_changelog.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.git_environment import (
        hardened_git_environment,
        ignored_git_environment_warning,
    )

HOMEPAGE_REPO = "HyprialOS/homepage"
CANDIDATE_BASE_REF = "draft"
CANDIDATE_BRANCH_PREFIX = "automation/homepage-release/"
RELEASE_RECORD_DIR = "docs/changelog/releases/"
SOURCE_URL_BASE = "https://code.hyprial.com/HyprialOS/harness-bridge/src/tag"
GUARD_REF = "ci/release-origin-guard"
#: The PR gate owns "touches a workflow + looks like a candidate" (it emits
#: failure there).  The guard must stay silent on that commit, so sync skips
#: its dispatch.  One commit has exactly one status emitter.
#:
#: Cross-repo: the gate lives in HyprialOS/homepage at
#: ``.forgejo/workflows/homepage-release-gate.yml`` (PR #166), whose workflow
#: filter is ``startsWith(".forgejo/workflows/")``; the guard rules that gate
#: imports live at ``ci/release-origin-guard`` (``cd3e9a1``) in
#: ``scripts/changelog-release-origin-guard.mjs`` and
#: ``src/lib/changelog-origin.js``.  This definition must stay >= the gate's:
#: a path the gate fails must never be dispatched by sync.
WORKFLOW_PATH_PREFIX = ".forgejo/workflows/"
#: Identity of the single candidate commit.  The guard checks the *pull
#: request author* (the token's login), not the commit author, so this is only
#: about a stable, reviewable history line.
RELEASE_COMMIT_NAME = "homepage-release-bot"
RELEASE_COMMIT_EMAIL = "homepage-release-bot@users.noreply.example.com"
#: A candidate PR carries one record file, and 50 is the API's bounded page.
#: A full first page cannot certify silence (another page may carry a workflow
#: path), so it is treated like an unreadable diff and the guard stays silent.
GUARD_FILES_PAGE_LIMIT = 50

MANIFEST_KEYS = (
    "version",
    "released",
    "source_tag",
    "title",
    "title_zh",
    "summary",
    "summary_zh",
    "scope",
    "scope_zh",
)
PUBLIC_TEXT_LIMITS = {
    "title": 120,
    "title_zh": 120,
    "summary": 600,
    "summary_zh": 600,
    "scope": 120,
    "scope_zh": 120,
}
CANONICAL_FIELD_ORDER = (
    "version",
    "released",
    "source_tag",
    "source_commit",
    "metadata_commit",
    "title",
    "title_zh",
    "summary",
    "summary_zh",
    "scope",
    "scope_zh",
    "source_url",
)
UNQUOTED_FIELDS = frozenset({"source_commit", "metadata_commit"})
VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NEWLINE_RE = re.compile(r"[\r\n]")

_SEAL = object()


class ReleaseContractError(ValueError):
    """A release fact failed validation; never a silent skip."""


def _fail(message: str) -> None:
    raise ReleaseContractError(message)


# ---------------------------------------------------------------------------
# Task 1: pure contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    version: str
    released: str
    source_tag: str
    title: str
    title_zh: str
    summary: str
    summary_zh: str
    scope: str
    scope_zh: str


@dataclass(frozen=True)
class SourceFacts:
    tag: str
    tag_sha: str
    source_sha: str
    metadata_sha: str
    historical: bool


@dataclass(frozen=True, init=False)
class ValidatedRelease:
    facts: SourceFacts
    record: bytes
    _seal: object = field(repr=False, compare=False)

    def __init__(
        self,
        facts: SourceFacts,
        record: bytes,
        *,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _SEAL:
            _fail("ValidatedRelease must be produced by validate_release (validated)")
        object.__setattr__(self, "facts", facts)
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "_seal", _SEAL)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"manifest: duplicate key {key}")
        result[key] = value
    return result


def _valid_date(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def parse_manifest(path: Path) -> Manifest:
    """Parse the reviewed manifest: one JSON object in a ``.yml`` file.

    JSON is a YAML subset, so the stdlib parser rejects aliases, tags,
    duplicate loose syntax, and implicit dates. Exactly the nine reviewed
    string keys are allowed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"manifest: cannot read {path}: {exc}")
    if not raw.strip():
        _fail("manifest: empty")
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        _fail(f"manifest: JSON-shaped YAML expected: {exc}")
    if not isinstance(data, dict):
        _fail("manifest: must be a single JSON object")
    missing = [key for key in MANIFEST_KEYS if key not in data]
    if missing:
        _fail(f"manifest: missing {', '.join(missing)}")
    extra = [key for key in data if key not in MANIFEST_KEYS]
    if extra:
        _fail(f"manifest: unknown key {extra[0]}")
    values: dict[str, str] = {}
    for key in MANIFEST_KEYS:
        value = data[key]
        if not isinstance(value, str):
            _fail(f"manifest: {key} must be a string")
        if not value.strip():
            _fail(f"manifest: {key} is empty")
        if value != value.strip():
            _fail(f"manifest: {key} must be trimmed")
        limit = PUBLIC_TEXT_LIMITS.get(key)
        if limit is not None and len(value) > limit:
            _fail(f"manifest: {key} exceeds {limit} characters")
        if CONTROL_RE.search(value) or NEWLINE_RE.search(value):
            _fail(f"manifest: {key} contains a control character or newline")
        values[key] = value
    version = values["version"]
    if not VERSION_RE.fullmatch(version):
        _fail(f"manifest: version {version!r} is not a canonical X.Y.Z version")
    if values["source_tag"] != f"v{version}":
        _fail(f"manifest: source_tag must be v{version}")
    if not _valid_date(values["released"]):
        _fail(f"manifest: released {values['released']!r} is not a real YYYY-MM-DD date")
    if path.name != f"{version}.yml":
        _fail(f"manifest: filename must be {version}.yml")
    return Manifest(**values)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Argument arrays only: never shell interpolation.
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def resolve_source_facts(
    repo: Path,
    *,
    tag: str,
    source_sha: str,
    metadata_sha: str,
    historical: bool,
) -> SourceFacts:
    """Peel the tag, require the peeled SHA, and verify metadata ancestry."""
    if not TAG_RE.fullmatch(tag):
        _fail(f"tag: {tag!r} is not v<X.Y.Z>")
    if not COMMIT_RE.fullmatch(source_sha):
        _fail("source_sha: expected 40 lowercase hex characters")
    metadata = metadata_sha or source_sha
    if not COMMIT_RE.fullmatch(metadata):
        _fail("metadata_sha: expected 40 lowercase hex characters")
    peeled = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if peeled.returncode != 0:
        _fail(f"tag: {tag} is absent or unreadable: {peeled.stderr.strip()}")
    tag_sha = peeled.stdout.strip()
    if tag_sha != source_sha:
        _fail(f"tag: {tag} resolves to {tag_sha}, not {source_sha} (moved or mismatched)")
    exists = _git(repo, "cat-file", "-e", f"{metadata}^{{commit}}")
    if exists.returncode != 0:
        _fail(f"metadata_sha: {metadata} is not a commit")
    if historical:
        ancestor = _git(repo, "merge-base", "--is-ancestor", source_sha, metadata)
        if ancestor.returncode != 0:
            _fail(f"metadata_sha: {metadata} does not descend from {source_sha}")
    elif metadata != source_sha:
        _fail("metadata_sha: non-historical release requires metadata_sha == source_sha")
    return SourceFacts(
        tag=tag,
        tag_sha=tag_sha,
        source_sha=source_sha,
        metadata_sha=metadata,
        historical=historical,
    )


def _validate_changelog(changelog: str, manifest: Manifest) -> None:
    if not isinstance(changelog, str) or not changelog:
        _fail("changelog: empty")
    pattern = re.compile(
        rf"^## \[{re.escape(manifest.version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})[ \t]*$",
        re.MULTILINE,
    )
    dates = pattern.findall(changelog)
    if len(dates) != 1:
        _fail(f"changelog: expected exactly one heading for {manifest.version}, found {len(dates)}")
    if dates[0] != manifest.released:
        _fail(f"changelog: heading date {dates[0]} does not match manifest date {manifest.released}")


def verify_source(*, facts: SourceFacts, changelog: str, manifest: Manifest) -> None:
    """Fail closed on every tag/source/metadata/changelog mismatch."""
    if facts.tag != manifest.source_tag:
        _fail(f"tag: expected {manifest.source_tag}, got {facts.tag}")
    if facts.tag_sha != facts.source_sha:
        _fail("tag: peeled SHA does not equal source_sha")
    if facts.tag != f"v{manifest.version}":
        _fail(f"tag: {facts.tag} does not match manifest version {manifest.version}")
    if not facts.historical and facts.metadata_sha != facts.source_sha:
        _fail("metadata_sha: non-historical release requires metadata_sha == source_sha")
    _validate_changelog(changelog, manifest)


def render_homepage_record(
    manifest: Manifest,
    *,
    source_sha: str,
    metadata_sha: str,
) -> bytes:
    """Render the canonical Homepage record (12 keys, fixed order, LF only).

    The guard's ``src/lib/changelog-origin.js`` replicates this function:
    every value is a JSON string literal except ``source_commit`` and
    ``metadata_commit``; lines are joined by a single LF and the output ends
    with one LF. A format change makes the guard reject the candidate.
    """
    record = {
        "version": manifest.version,
        "released": manifest.released,
        "source_tag": manifest.source_tag,
        "source_commit": source_sha,
        "metadata_commit": metadata_sha,
        "title": manifest.title,
        "title_zh": manifest.title_zh,
        "summary": manifest.summary,
        "summary_zh": manifest.summary_zh,
        "scope": manifest.scope,
        "scope_zh": manifest.scope_zh,
        "source_url": f"{SOURCE_URL_BASE}/{manifest.source_tag}/CHANGELOG.md",
    }
    lines = ["---"]
    for key in CANONICAL_FIELD_ORDER:
        value = record[key] if key in UNQUOTED_FIELDS else json.dumps(record[key], ensure_ascii=False)
        lines.append(f"{key}: {value}")
    return ("\n".join([*lines, "---"]) + "\n").encode()


def validate_release(
    *,
    facts: SourceFacts,
    changelog: str,
    manifest: Manifest,
) -> ValidatedRelease:
    verify_source(facts=facts, changelog=changelog, manifest=manifest)
    record = render_homepage_record(
        manifest,
        source_sha=facts.source_sha,
        metadata_sha=facts.metadata_sha,
    )
    return ValidatedRelease(facts, record, _seal=_SEAL)


# ---------------------------------------------------------------------------
# Task 2: deterministic, idempotent candidate creation
# ---------------------------------------------------------------------------


def candidate_key(*, tag: str, source_sha: str, record: bytes) -> str:
    digest = hashlib.sha256(record).hexdigest()
    return hashlib.sha256(f"{tag}\n{source_sha}\n{digest}".encode()).hexdigest()


def decide_existing(
    *,
    open_bytes: bytes | None,
    merged_bytes: bytes | None,
    closed: bool,
    branch_only: bool,
    branch_bytes: bytes | None = None,
    wanted: bytes,
) -> str:
    """Classify the existing candidate into the next sync action.

    ``update`` replaces the branch with one commit on the current draft head;
    ``open-pr`` only opens the missing PR (the bytes already match).  A closed
    unmerged candidate and a merged candidate with different bytes stay
    terminal: neither may be rewritten.
    """
    if open_bytes is not None:
        # The candidate is open but its bytes changed: rewrite the branch to a
        # new single commit instead of appending a second one.
        return "reuse-open" if open_bytes == wanted else "update"
    if merged_bytes is not None:
        return "no-op-merged" if merged_bytes == wanted else "error"
    if closed:
        return "error"
    if branch_only:
        # A previous run created the branch but not the PR.  Matching bytes
        # only need the PR; anything else is rewritten to one commit first.
        return "open-pr" if branch_bytes == wanted else "update"
    return "create"


@dataclass(frozen=True)
class BranchState:
    name: str
    exists: bool
    record: bytes | None = None
    head_sha: str | None = None


@dataclass(frozen=True)
class PullRequestState:
    number: int
    state: str
    merged: bool
    record: bytes | None = None
    head_sha: str | None = None
    base_sha: str | None = None


class HomepageClient(Protocol):
    def find_branch(self, name: str) -> BranchState: ...

    def find_pr(self, branch: str) -> PullRequestState | None: ...

    def create_branch(self, name: str, base_sha: str) -> None: ...

    def create_pr(self, branch: str, record: bytes) -> PullRequestState: ...

    def open_pr(self, branch: str) -> PullRequestState: ...

    def rewrite_candidate(
        self, branch: str, *, record: bytes, expected_head: str | None
    ) -> tuple[str, str]: ...


@dataclass(frozen=True)
class GuardDispatch:
    """Whether sync hands this candidate to the guard, and why."""

    dispatch: bool
    reason: str


class GuardDispatchClient(Protocol):
    def changed_paths(self, pr: PullRequestState) -> tuple[str, ...]: ...

    def dispatch_guard(self, pr: PullRequestState) -> None: ...


def _candidate_branch(tag: str) -> str:
    return f"{CANDIDATE_BRANCH_PREFIX}{tag.removeprefix('v')}"


def _default_git_url(api_url: str, repo: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    return f"{base}/{repo}.git"


def _query(client: HomepageClient, branch: str) -> tuple[BranchState, PullRequestState | None]:
    return client.find_branch(branch), client.find_pr(branch)


def _decision_for(
    branch_state: BranchState,
    pr_state: PullRequestState | None,
    wanted: bytes,
) -> str:
    open_bytes = pr_state.record if pr_state is not None and pr_state.state == "open" and not pr_state.merged else None
    merged_bytes = pr_state.record if pr_state is not None and pr_state.merged else None
    closed = pr_state is not None and pr_state.state == "closed" and not pr_state.merged
    branch_only = branch_state.exists and pr_state is None
    return decide_existing(
        open_bytes=open_bytes,
        merged_bytes=merged_bytes,
        closed=closed,
        branch_only=branch_only,
        branch_bytes=branch_state.record,
        wanted=wanted,
    )


def _retry_after_timeout(
    client: HomepageClient,
    branch: str,
    wanted: bytes,
) -> PullRequestState | None:
    """Re-query once; return a candidate if a concurrent run created it."""
    branch_state, pr_state = _query(client, branch)
    if _decision_for(branch_state, pr_state, wanted) in {"reuse-open", "no-op-merged"}:
        return pr_state
    return None


def sync_candidate(
    client: HomepageClient,
    *,
    release: ValidatedRelease,
) -> PullRequestState:
    if not isinstance(release, ValidatedRelease) or getattr(release, "_seal", None) is not _SEAL:
        _fail("release must be validated before any Homepage write")
    branch = _candidate_branch(release.facts.tag)
    wanted = release.record

    branch_state, pr_state = _query(client, branch)
    decision = _decision_for(branch_state, pr_state, wanted)
    if decision == "error":
        _fail(f"candidate state for {branch} is not resumable (closed or conflicting merged bytes)")
    if decision == "update":
        head, base = client.rewrite_candidate(
            branch, record=wanted, expected_head=branch_state.head_sha
        )
        pr = pr_state if pr_state is not None else client.open_pr(branch)
        return replace(pr, record=wanted, head_sha=head, base_sha=base)
    if decision == "open-pr":
        return replace(
            client.open_pr(branch),
            record=wanted,
            head_sha=branch_state.head_sha or None,
        )
    if decision in {"reuse-open", "no-op-merged"}:
        assert pr_state is not None
        return pr_state

    try:
        client.create_branch(branch, "")
    except TimeoutError:
        existing = _retry_after_timeout(client, branch, wanted)
        if existing is not None:
            return existing
        try:
            client.create_branch(branch, "")
        except TimeoutError:
            existing = _retry_after_timeout(client, branch, wanted)
            if existing is not None:
                return existing
            _fail(f"timeout creating {branch}; state remains unknown after re-query")

    try:
        return client.create_pr(branch, wanted)
    except TimeoutError:
        existing = _retry_after_timeout(client, branch, wanted)
        if existing is not None:
            return existing
        try:
            return client.create_pr(branch, wanted)
        except TimeoutError:
            existing = _retry_after_timeout(client, branch, wanted)
            if existing is not None:
                return existing
            _fail(f"timeout creating the pull request for {branch}; state remains unknown")


# ---------------------------------------------------------------------------
# Forgejo implementation and CLI
# ---------------------------------------------------------------------------


class ForgejoHomepageClient:
    """The only Homepage-write implementation (Task 2).

    The candidate token creates the branch/PR; a distinct guard-dispatch token
    is used only for the guard dispatch call. The guard-status token is never
    present here.
    """

    def __init__(
        self,
        *,
        api_url: str,
        repo: str,
        candidate_token: str,
        guard_dispatch_token: str | None = None,
        timeout: float = 20.0,
        git_url: str | None = None,
    ) -> None:
        if not candidate_token:
            _fail("missing candidate-writer token")
        self.api_url = api_url.rstrip("/")
        self.repo = repo
        self.candidate_token = candidate_token
        self.guard_dispatch_token = guard_dispatch_token or candidate_token
        self.timeout = timeout
        # The REST API in this Forgejo has no ref-update endpoint, so the
        # single-commit rewrite uses git's own --force-with-lease instead.
        self.git_url = git_url or _default_git_url(self.api_url, repo)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"token {token or self.candidate_token}",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 404:
                raise FileNotFoundError(path) from exc
            _fail(f"{method} {path} -> HTTP {exc.code}: {detail}")
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            _fail(f"{method} {path} -> {exc.reason}")
        raise AssertionError("unreachable")

    def _read_record(self, ref: str, version: str) -> bytes | None:
        path = f"{RELEASE_RECORD_DIR}{version}.md"
        try:
            payload = self._request("GET", f"/repos/{self.repo}/contents/{path}?ref={ref}")
        except FileNotFoundError:
            return None
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            _fail(f"contents payload for {path} is not base64")
        return base64.b64decode(payload["content"])

    def find_branch(self, name: str) -> BranchState:
        try:
            payload = self._request("GET", f"/repos/{self.repo}/branches/{name}")
        except FileNotFoundError:
            return BranchState(name=name, exists=False)
        version = name.removeprefix(CANDIDATE_BRANCH_PREFIX)
        head = (payload.get("commit") or {}).get("id") if isinstance(payload, dict) else None
        return BranchState(
            name=name,
            exists=True,
            record=self._read_record(name, version),
            head_sha=str(head) if head else None,
        )

    def find_pr(self, branch: str) -> PullRequestState | None:
        payload = self._request("GET", f"/repos/{self.repo}/pulls?state=all&limit=50")
        for item in payload if isinstance(payload, list) else []:
            head = item.get("head") or {}
            if head.get("ref") != branch:
                continue
            number = int(item["number"])
            detail = self._request("GET", f"/repos/{self.repo}/pulls/{number}")
            head_sha = str((detail.get("head") or {}).get("sha", ""))
            base_sha = str((detail.get("base") or {}).get("sha", ""))
            version = branch.removeprefix(CANDIDATE_BRANCH_PREFIX)
            return PullRequestState(
                number=number,
                state=str(detail.get("state", item.get("state", ""))),
                merged=bool(detail.get("merged", item.get("merged", False))),
                record=self._read_record(head_sha, version) if head_sha else None,
                head_sha=head_sha or None,
                base_sha=base_sha or None,
            )
        return None

    def create_branch(self, name: str, base_sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.repo}/branches",
            {
                "new_branch_name": name,
                "old_ref_name": base_sha or CANDIDATE_BASE_REF,
            },
        )

    def create_pr(self, branch: str, record: bytes) -> PullRequestState:
        version = branch.removeprefix(CANDIDATE_BRANCH_PREFIX)
        path = f"{RELEASE_RECORD_DIR}{version}.md"
        # The record is committed on the candidate branch, then the PR opens.
        # ``record`` is the only source of the file bytes.
        self._request(
            "POST",
            f"/repos/{self.repo}/contents/{path}",
            {
                "content": base64.b64encode(record).decode(),
                "message": f"docs(changelog): add release {version}",
                "branch": branch,
            },
        )
        return replace(self.open_pr(branch), record=record)

    def open_pr(self, branch: str) -> PullRequestState:
        version = branch.removeprefix(CANDIDATE_BRANCH_PREFIX)
        payload = self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {
                "head": branch,
                "base": CANDIDATE_BASE_REF,
                "title": f"changelog: release {version}",
                "body": (
                    f"Generated public changelog record for `v{version}`.\n\n"
                    "Created by the Harness Bridge release sync; the trusted "
                    "`ci/release-origin-guard` status must pass before merge."
                ),
            },
        )
        if not isinstance(payload, dict) or not payload.get("number"):
            _fail("pull request response is missing number")
        head_sha = str((payload.get("head") or {}).get("sha", ""))
        base_sha = str((payload.get("base") or {}).get("sha", ""))
        return PullRequestState(
            number=int(payload["number"]),
            state=str(payload.get("state", "open")),
            merged=bool(payload.get("merged")),
            head_sha=head_sha or None,
            base_sha=base_sha or None,
        )

    def _git_env(self) -> dict[str, str]:
        warning = ignored_git_environment_warning(os.environ)
        if warning:
            print(warning, file=sys.stderr)
        config_entry = None
        if self.git_url.startswith(("http://", "https://")):
            # Keep the token out of the URL and argv: it lives only in this
            # child process's command-scope configuration.
            config_entry = (
                "http.extraHeader",
                f"Authorization: token {self.candidate_token}",
            )
        return hardened_git_environment(
            os.environ,
            config_entry=config_entry,
            identity=(RELEASE_COMMIT_NAME, RELEASE_COMMIT_EMAIL),
        )

    def _git(self, cwd: Path, *args: str) -> str:
        if shutil.which("git") is None:
            _fail("git is required to rewrite the candidate branch")
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                env=self._git_env(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            _fail(f"git {args[0]} failed to run: {error}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:300]
            # The environment can hold the token; never echo it.
            _fail(f"git {args[0]} failed: {detail}")
        return completed.stdout.strip()

    def rewrite_candidate(
        self, branch: str, *, record: bytes, expected_head: str | None
    ) -> tuple[str, str]:
        """Replace the candidate branch with one commit on the current draft.

        The REST API has no ref-update endpoint, so this is a real
        ``git push --force-with-lease`` against the expected old head: a
        branch that moved after the state read fails loudly and is not
        overwritten.  The new commit's only parent is the current draft head,
        so the guard's compare sees exactly one added record file.
        """

        version = branch.removeprefix(CANDIDATE_BRANCH_PREFIX)
        relative = f"{RELEASE_RECORD_DIR}{version}.md"
        if expected_head is None:
            _fail("candidate branch exists but its head is unknown; refusing to rewrite")
        with tempfile.TemporaryDirectory(prefix="hyprial-homepage-candidate-") as work:
            checkout = Path(work)
            self._git(checkout, "init", "-q")
            self._git(checkout, "remote", "add", "origin", self.git_url)
            self._git(checkout, "fetch", "--depth=1", "origin", CANDIDATE_BASE_REF)
            self._git(checkout, "checkout", "-q", "--detach", "FETCH_HEAD")
            base = self._git(checkout, "rev-parse", "HEAD")
            target = checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(record)
            self._git(checkout, "add", "--", relative)
            self._git(
                checkout,
                "commit",
                "-q",
                "-m",
                f"docs(changelog): add release {version}",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            self._git(
                checkout,
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_head}",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
        return head, base

    def changed_paths(self, pr: PullRequestState) -> tuple[str, ...]:
        payload = self._request(
            "GET",
            f"/repos/{self.repo}/pulls/{pr.number}/files"
            f"?limit={GUARD_FILES_PAGE_LIMIT}",
        )
        if not isinstance(payload, list):
            _fail("pull request files payload is not a list")
        # A rename reports the NEW path as ``filename`` and the OLD path as
        # ``previous_filename`` (ChangedFile in the instance swagger), so a
        # file renamed out of the workflow dir is only visible through the
        # latter.  Both sides count as touching.
        paths = tuple(
            str(value)
            for item in payload
            if isinstance(item, dict)
            for value in (item.get("filename"), item.get("previous_filename"))
            if isinstance(value, str) and value
        )
        if len(payload) >= GUARD_FILES_PAGE_LIMIT:
            # The next page may carry a workflow path; refuse to certify
            # silence from a truncated list.
            raise ReleaseContractError("pull request file list is truncated")
        return tuple(path for path in paths if path)

    def dispatch_guard(self, pr: PullRequestState) -> None:
        if not pr.head_sha or not pr.base_sha:
            _fail("candidate pull request is missing head/base SHA")
        self._request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/homepage-release-origin-guard.yml/dispatches",
            {
                "ref": GUARD_REF,
                "inputs": {
                    "pr_number": str(pr.number),
                    "head_sha": pr.head_sha,
                    "base_sha": pr.base_sha,
                },
            },
            token=self.guard_dispatch_token,
        )


def is_workflow_path(path: str) -> bool:
    """Whether a changed path is inside the workflow directory.

    Keep this predicate >= the #166 gate's ``startsWith`` check (see the
    comment on ``WORKFLOW_PATH_PREFIX``); the gate is the failure emitter, so
    sync must suppress at least the same set.
    """

    return path == WORKFLOW_PATH_PREFIX.rstrip("/") or path.startswith(
        WORKFLOW_PATH_PREFIX
    )


def guard_dispatch_decision(
    client: GuardDispatchClient, pr: PullRequestState
) -> GuardDispatch:
    """Decide whether the trusted guard may be dispatched for this candidate.

    The new PR gate emits failure for "touches .forgejo/workflows/** + looks
    like a candidate"; the guard must stay silent on that commit, so sync does
    not dispatch it.  Uncertainty (unreadable or truncated file list) is also
    treated as touching: a duplicate emitter is worse than a missing guard run,
    because the PR gate still judges the PR and sync never writes a status.
    """

    try:
        paths = client.changed_paths(pr)
    except (FileNotFoundError, TimeoutError, ReleaseContractError):
        return GuardDispatch(False, "files-unavailable")
    if any(is_workflow_path(path) for path in paths):
        return GuardDispatch(False, "workflow-diff")
    return GuardDispatch(True, "candidate")


def publish_candidate(
    client: GuardDispatchClient, *, release: ValidatedRelease
) -> tuple[PullRequestState, GuardDispatch]:
    """Create/reuse the candidate, then dispatch the guard only when allowed.

    A merged candidate needs no dispatch (the status cannot unblock anything).
    """

    pr = sync_candidate(client, release=release)
    if pr.merged:
        return pr, GuardDispatch(False, "merged")
    decision = guard_dispatch_decision(client, pr)
    if decision.dispatch:
        client.dispatch_guard(pr)
    return pr, decision


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness Bridge -> Homepage changelog sync")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=".")
        p.add_argument("--manifest", required=True)
        p.add_argument("--changelog", required=True)
        p.add_argument("--tag", required=True)
        p.add_argument("--source-sha", required=True)
        p.add_argument("--metadata-sha", default="")
        p.add_argument("--historical", default="false")

    validate = sub.add_parser("validate")
    common(validate)
    validate.add_argument("--out")

    publish = sub.add_parser("publish")
    common(publish)
    publish.add_argument("--dry-run", default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    historical = str(args.historical).lower() == "true"
    manifest = parse_manifest(Path(args.manifest))
    facts = resolve_source_facts(
        Path(args.repo),
        tag=args.tag,
        source_sha=args.source_sha,
        metadata_sha=args.metadata_sha,
        historical=historical,
    )
    release = validate_release(facts=facts, changelog=_read(args.changelog), manifest=manifest)
    summary = {
        "version": manifest.version,
        "path": f"{RELEASE_RECORD_DIR}{manifest.version}.md",
        "branch": _candidate_branch(facts.tag),
        "bytes": len(release.record),
        "key": candidate_key(tag=facts.tag, source_sha=facts.source_sha, record=release.record),
    }
    if args.command == "validate":
        if args.out:
            Path(args.out).write_bytes(release.record)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    if str(args.dry_run).lower() == "true":
        summary["dryRun"] = True
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    client = ForgejoHomepageClient(
        api_url=os.environ.get("FORGEJO_API_URL", "https://code.hyprial.com/api/v1"),
        repo=os.environ.get("HOMEPAGE_REPO", HOMEPAGE_REPO),
        candidate_token=os.environ.get("HOMEPAGE_CANDIDATE_TOKEN", ""),
        guard_dispatch_token=os.environ.get("HOMEPAGE_GUARD_DISPATCH_TOKEN", ""),
        git_url=os.environ.get("HOMEPAGE_GIT_URL", ""),
    )
    pr, guard = publish_candidate(client, release=release)
    summary.update(
        {
            "pr": pr.number,
            "head": pr.head_sha,
            "base": pr.base_sha,
            "merged": pr.merged,
            "guardDispatch": guard.reason,
        }
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseContractError as exc:
        print(f"homepage_changelog: {exc}", file=sys.stderr)
        raise SystemExit(1)
