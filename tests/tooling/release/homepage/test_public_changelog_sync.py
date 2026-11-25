"""Contract and idempotency tests for the Homepage changelog sync.

The canonical byte expectations here mirror HyprialOS/homepage's
``src/lib/changelog-origin.js``. The guard recomputes these bytes before it
posts a status; a format change makes it reject the candidate.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "release" / "homepage_changelog.py"


def _module():
    spec = importlib.util.spec_from_file_location("homepage_changelog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _module()

SOURCE = "a" * 40
METADATA = "b" * 40
VERSION = "0.4.6"
MANIFEST = {
    "version": VERSION,
    "released": "2026-09-16",
    "source_tag": f"v{VERSION}",
    "title": "Public release and GUI integration",
    "title_zh": "公网发布与 GUI 集成",
    "summary": "A concise, reviewed description of what users can notice.",
    "summary_zh": "一句经审阅的中文用户可感知变化说明。",
    "scope": "HyprialOS",
    "scope_zh": "HyprialOS",
}
CHANGELOG = f"# Changelog\n\n## [{VERSION}] - 2026-09-16\n\n### Added\n- x\n"
EXPECTED_RECORD = (
    "---\n"
    f'version: "{VERSION}"\n'
    'released: "2026-09-16"\n'
    f'source_tag: "v{VERSION}"\n'
    f"source_commit: {SOURCE}\n"
    f"metadata_commit: {METADATA}\n"
    'title: "Public release and GUI integration"\n'
    'title_zh: "公网发布与 GUI 集成"\n'
    'summary: "A concise, reviewed description of what users can notice."\n'
    'summary_zh: "一句经审阅的中文用户可感知变化说明。"\n'
    'scope: "HyprialOS"\n'
    'scope_zh: "HyprialOS"\n'
    'source_url: "https://code.hyprial.com/HyprialOS/harness-bridge/src/tag/v0.4.6/CHANGELOG.md"\n'
    "---\n"
).encode()


def _write_manifest(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / f"{VERSION}.yml"
    path.write_text(text if text is not None else json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    return path


def _manifest(tmp_path: Path) -> object:
    return SYNC.parse_manifest(_write_manifest(tmp_path))


def _facts() -> object:
    return SYNC.SourceFacts(
        tag=f"v{VERSION}",
        tag_sha=SOURCE,
        source_sha=SOURCE,
        metadata_sha=METADATA,
        historical=True,
    )


def _release(tmp_path: Path) -> object:
    return SYNC.validate_release(facts=_facts(), changelog=CHANGELOG, manifest=_manifest(tmp_path))


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return completed.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "source")
    source = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", f"v{VERSION}", "-m", "release")
    (repo / "metadata.txt").write_text("metadata\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "metadata")
    metadata = _git(repo, "rev-parse", "HEAD")
    return repo, source, metadata


def test_manifest_is_one_json_object_with_nine_strings(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert manifest.version == VERSION
    assert manifest.source_tag == f"v{VERSION}"


def test_manifest_rejects_non_json_yaml_and_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="JSON-shaped YAML|unknown key"):
        SYNC.parse_manifest(_write_manifest(tmp_path, 'released: 2026-09-16\nextra: no\n'))
    with pytest.raises(ValueError, match="unknown key"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "extra": "x"})))


def test_manifest_rejects_duplicate_and_missing_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate key"):
        SYNC.parse_manifest(_write_manifest(tmp_path, '{"version": "0.4.6", "version": "0.4.6"}'))
    missing = {key: value for key, value in MANIFEST.items() if key != "summary"}
    with pytest.raises(ValueError, match="missing summary"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps(missing)))


def test_manifest_rejects_non_strings_and_unsafe_text(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a string"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "version": 0.46})))
    with pytest.raises(ValueError, match="control character"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "title": "a\u0001b"})))
    with pytest.raises(ValueError, match="control character"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "summary": "a\nb"})))
    with pytest.raises(ValueError, match="exceeds 120"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "title": "x" * 121})))


def test_manifest_version_tag_date_and_filename(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canonical X.Y.Z"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "version": "00.4.6"})))
    with pytest.raises(ValueError, match="source_tag"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "source_tag": "v0.4.5"})))
    with pytest.raises(ValueError, match="real YYYY-MM-DD"):
        SYNC.parse_manifest(_write_manifest(tmp_path, json.dumps({**MANIFEST, "released": "2026-02-30"})))
    wrong = tmp_path / "0.4.5.yml"
    wrong.write_text(json.dumps(MANIFEST), encoding="utf-8")
    with pytest.raises(ValueError, match="filename"):
        SYNC.parse_manifest(wrong)


def test_resolve_source_facts_accepts_descendant_and_rejects_bad_history(
    git_repo: tuple[Path, str, str],
) -> None:
    repo, source, metadata = git_repo
    facts = SYNC.resolve_source_facts(repo, tag=f"v{VERSION}", source_sha=source, metadata_sha=metadata, historical=True)
    assert facts.tag_sha == source
    assert facts.metadata_sha == metadata
    with pytest.raises(ValueError):
        SYNC.resolve_source_facts(repo, tag="v9.9.9", source_sha=source, metadata_sha=metadata, historical=True)
    with pytest.raises(ValueError):
        SYNC.resolve_source_facts(repo, tag=f"v{VERSION}", source_sha="a" * 40, metadata_sha=metadata, historical=True)


def test_resolve_source_facts_rejects_non_descendant(
    git_repo: tuple[Path, str, str],
    tmp_path: Path,
) -> None:
    repo, source, _metadata = git_repo
    _git(repo, "checkout", "-q", "--orphan", "other")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "orphan")
    orphan = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="descend"):
        SYNC.resolve_source_facts(repo, tag=f"v{VERSION}", source_sha=source, metadata_sha=orphan, historical=True)


def test_source_verification_and_sealed_release(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    facts = _facts()
    SYNC.verify_source(facts=facts, changelog=CHANGELOG, manifest=manifest)
    release = SYNC.validate_release(facts=facts, changelog=CHANGELOG, manifest=manifest)
    assert release.record == EXPECTED_RECORD
    assert len(release.record) == 537
    with pytest.raises(ValueError, match="validated"):
        SYNC.ValidatedRelease(facts, b"")


def test_verify_source_rejects_mismatches(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(ValueError, match="tag"):
        SYNC.verify_source(facts=SYNC.SourceFacts("v0.4.5", SOURCE, SOURCE, METADATA, True), changelog=CHANGELOG, manifest=manifest)
    with pytest.raises(ValueError, match="exactly one"):
        SYNC.verify_source(facts=_facts(), changelog="# Changelog\n", manifest=manifest)
    with pytest.raises(ValueError, match="does not match"):
        SYNC.verify_source(facts=_facts(), changelog=CHANGELOG, manifest=SYNC.Manifest(**{**MANIFEST, "released": "2026-09-17"}))


def test_render_homepage_record_is_bytes_with_fixed_keys(tmp_path: Path) -> None:
    rendered = SYNC.render_homepage_record(_manifest(tmp_path), source_sha=SOURCE, metadata_sha=METADATA)
    assert rendered == EXPECTED_RECORD
    assert rendered.endswith(b"---\n")
    assert b"source_commit: " + SOURCE.encode() in rendered
    assert b"metadata_commit: " + METADATA.encode() in rendered


def test_candidate_key_binds_tag_source_and_record() -> None:
    key = SYNC.candidate_key(tag=f"v{VERSION}", source_sha=SOURCE, record=EXPECTED_RECORD)
    assert key == SYNC.candidate_key(tag=f"v{VERSION}", source_sha=SOURCE, record=EXPECTED_RECORD)
    assert key != SYNC.candidate_key(tag=f"v{VERSION}", source_sha=METADATA, record=EXPECTED_RECORD)


@pytest.mark.parametrize(
    ("open_bytes", "merged_bytes", "closed", "branch_only", "expected"),
    [
        (b"same", None, False, False, "reuse-open"),
        (None, b"same", False, False, "no-op-merged"),
        (None, None, False, False, "create"),
        (None, None, True, False, "error"),
        # Branch-only without matching bytes is recoverable: rewrite to one
        # commit, then open the PR.
        (None, None, False, True, "update"),
        # An open candidate whose bytes changed is rewritten, not abandoned.
        (b"other", None, False, False, "update"),
    ],
)
def test_decide_existing(open_bytes, merged_bytes, closed, branch_only, expected) -> None:
    assert (
        SYNC.decide_existing(
            open_bytes=open_bytes,
            merged_bytes=merged_bytes,
            closed=closed,
            branch_only=branch_only,
            wanted=b"same",
        )
        == expected
    )


def test_decide_existing_branch_only_with_matching_bytes_opens_the_pr() -> None:
    assert (
        SYNC.decide_existing(
            open_bytes=None,
            merged_bytes=None,
            closed=False,
            branch_only=True,
            branch_bytes=b"same",
            wanted=b"same",
        )
        == "open-pr"
    )


class _RecordingClient:
    def __init__(self, branch: object, pr: object = None) -> None:
        self.branch = branch
        self.pr = pr
        self.calls: list[str] = []
        self.writes: list[tuple] = []

    def find_branch(self, name: str):
        self.calls.append("find_branch")
        return self.branch

    def find_pr(self, branch: str):
        self.calls.append("find_pr")
        return self.pr

    def create_branch(self, name: str, base_sha: str) -> None:
        self.calls.append("create_branch")
        self.writes.append(("create_branch", name))
        self.branch = SYNC.BranchState(name=name, exists=True)

    def create_pr(self, branch: str, record: bytes):
        self.calls.append("create_pr")
        self.writes.append(("create_pr", branch, record))
        return SYNC.PullRequestState(17, "open", False, record, "d" * 40, "c" * 40)


class _TimeoutThenExistingClient(_RecordingClient):
    def __init__(self, branch: str, record: bytes) -> None:
        super().__init__(SYNC.BranchState(branch, False), None)
        self.record = record
        self.first = True

    def find_branch(self, name: str):
        self.calls.append("find_branch")
        return SYNC.BranchState(name, False) if self.first else SYNC.BranchState(name, True, self.record)

    def find_pr(self, branch: str):
        self.calls.append("find_pr")
        return None if self.first else SYNC.PullRequestState(17, "open", False, self.record, "d" * 40, "c" * 40)

    def create_branch(self, name: str, base_sha: str) -> None:
        self.calls.append("create_branch")
        self.first = False
        raise TimeoutError("slow")


def test_sync_candidate_creates_exactly_once(tmp_path: Path) -> None:
    release = _release(tmp_path)
    client = _RecordingClient(SYNC.BranchState(SYNC._candidate_branch(release.facts.tag), False))
    pr = SYNC.sync_candidate(client, release=release)
    assert pr.number == 17
    assert client.calls == ["find_branch", "find_pr", "create_branch", "create_pr"]
    assert len(client.writes) == 2


def test_sync_candidate_reuses_open_and_merged(tmp_path: Path) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    open_client = _RecordingClient(
        SYNC.BranchState(branch, True, release.record),
        SYNC.PullRequestState(7, "open", False, release.record, "d" * 40, "c" * 40),
    )
    assert SYNC.sync_candidate(open_client, release=release).number == 7
    assert open_client.writes == []
    merged_client = _RecordingClient(
        SYNC.BranchState(branch, True, release.record),
        SYNC.PullRequestState(7, "closed", True, release.record, "d" * 40, "c" * 40),
    )
    assert SYNC.sync_candidate(merged_client, release=release).number == 7
    assert merged_client.writes == []


def test_sync_candidate_rejects_a_closed_candidate(tmp_path: Path) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    client = _RecordingClient(
        SYNC.BranchState(branch, True),
        SYNC.PullRequestState(7, "closed", False),
    )
    with pytest.raises(ValueError):
        SYNC.sync_candidate(client, release=release)
    assert client.writes == []


def test_sync_candidate_requeries_once_after_timeout(tmp_path: Path) -> None:
    release = _release(tmp_path)
    client = _TimeoutThenExistingClient(SYNC._candidate_branch(release.facts.tag), release.record)
    assert SYNC.sync_candidate(client, release=release).number == 17
    assert client.calls == ["find_branch", "find_pr", "create_branch", "find_branch", "find_pr"]


def test_forged_release_never_reaches_a_write(tmp_path: Path) -> None:
    release = _release(tmp_path)
    forged = object.__new__(SYNC.ValidatedRelease)
    object.__setattr__(forged, "facts", release.facts)
    object.__setattr__(forged, "record", release.record)
    object.__setattr__(forged, "_seal", object())
    client = _RecordingClient(SYNC.BranchState(SYNC._candidate_branch(release.facts.tag), False))
    with pytest.raises(ValueError, match="validated"):
        SYNC.sync_candidate(client, release=forged)
    assert client.writes == []


class _MockForgejo:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str, str]] = []
        self.branch: str | None = None
        self.contents: str | None = None
        self.pr: dict | None = None
        # None means "the files endpoint is unavailable" (500), which must
        # keep the guard silent rather than certify a diff nobody read.
        self.files: list[str | dict[str, str]] | None = None
        self.branch_head: str | None = None
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _send(self, status: int, payload: object | None = None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                mock.requests.append((self.command, self.path, body, self.headers.get("Authorization", "")))
                base = "/api/v1/repos/HyprialOS/homepage"
                path = self.path.split("?")[0]
                if self.command == "GET" and path.startswith(f"{base}/branches/"):
                    if mock.branch:
                        return self._send(
                            200,
                            {
                                "name": mock.branch,
                                "commit": {"id": mock.branch_head or "d" * 40},
                            },
                        )
                    return self._send(404, {"message": "not found"})
                if self.command == "POST" and path == f"{base}/branches":
                    mock.branch = json.loads(body)["new_branch_name"]
                    return self._send(201, {"name": mock.branch})
                if self.command == "POST" and path.startswith(f"{base}/contents/"):
                    mock.contents = json.loads(body)["content"]
                    return self._send(201, {"content": {"path": path}})
                if self.command == "GET" and path.startswith(f"{base}/contents/"):
                    if mock.contents:
                        return self._send(200, {"encoding": "base64", "content": mock.contents})
                    return self._send(404, {"message": "not found"})
                if self.command == "GET" and path == f"{base}/pulls":
                    return self._send(200, [mock.pr] if mock.pr else [])
                if self.command == "POST" and path == f"{base}/pulls":
                    mock.pr = {
                        "number": 7,
                        "state": "open",
                        "merged": False,
                        "head": {"sha": "d" * 40, "ref": mock.branch},
                        "base": {"sha": "c" * 40, "ref": "draft"},
                    }
                    return self._send(201, mock.pr)
                if self.command == "GET" and path == f"{base}/pulls/7":
                    return self._send(200, mock.pr)
                if self.command == "GET" and path == f"{base}/pulls/7/files":
                    if mock.files is None:
                        return self._send(500, {"message": "files unavailable"})
                    return self._send(
                        200,
                        [
                            {"filename": name} if isinstance(name, str) else name
                            for name in mock.files
                        ],
                    )
                if self.command == "POST" and path.endswith("/homepage-release-origin-guard.yml/dispatches"):
                    return self._send(204)
                return self._send(500, {"message": f"unexpected {self.command} {self.path}"})

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def client(self, *, git_url: str | None = None):
        kwargs = {}
        if git_url is not None:
            kwargs["git_url"] = git_url
        return SYNC.ForgejoHomepageClient(
            api_url=f"http://127.0.0.1:{self.server.server_address[1]}/api/v1",
            repo="HyprialOS/homepage",
            candidate_token="test-token",
            guard_dispatch_token="guard-dispatch-token",
            **kwargs,
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_forgejo_client_commits_once_and_dispatches_with_the_guard_token(tmp_path: Path) -> None:
    mock = _MockForgejo()
    try:
        release = _release(tmp_path)
        client = mock.client()
        pr = SYNC.sync_candidate(client, release=release)
        client.dispatch_guard(pr)
        assert pr.number == 7
        assert base64.b64decode(mock.contents) == EXPECTED_RECORD
        content_writes = [r for r in mock.requests if r[0] == "POST" and "/contents/" in r[1]]
        assert len(content_writes) == 1
        assert content_writes[0][3] == "token test-token"
        dispatch = [r for r in mock.requests if r[1].endswith("/homepage-release-origin-guard.yml/dispatches")]
        assert len(dispatch) == 1
        assert dispatch[0][3] == "token guard-dispatch-token"
        assert json.loads(dispatch[0][2]) == {
            "ref": "ci/release-origin-guard",
            "inputs": {"pr_number": "7", "head_sha": "d" * 40, "base_sha": "c" * 40},
        }
        assert not [r for r in mock.requests if "/statuses/" in r[1]]
    finally:
        mock.close()


def _publish(mock: "_MockForgejo", tmp_path: Path):
    return SYNC.publish_candidate(mock.client(), release=_release(tmp_path))


def _dispatches(mock: "_MockForgejo") -> list[tuple[str, str, str, str]]:
    return [r for r in mock.requests if r[1].endswith("/dispatches")]


def test_publish_keeps_the_guard_silent_when_the_diff_touches_a_workflow(
    tmp_path: Path,
) -> None:
    """Only one emitter per commit: the PR gate owns this row, not the guard."""

    mock = _MockForgejo()
    mock.files = [
        f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md",
        ".github/workflows/release.yml",
    ]
    try:
        pr, decision = _publish(mock, tmp_path)

        assert pr.number == 7
        assert decision.dispatch is False
        assert decision.reason == "workflow-diff"
        assert _dispatches(mock) == []
        assert not [r for r in mock.requests if "/statuses/" in r[1]]
    finally:
        mock.close()


def test_publish_dispatches_the_guard_for_a_record_only_candidate(
    tmp_path: Path,
) -> None:
    mock = _MockForgejo()
    mock.files = [f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md"]
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is True
        assert decision.reason == "candidate"
        dispatch = _dispatches(mock)
        assert len(dispatch) == 1
        assert dispatch[0][3] == "token guard-dispatch-token"
    finally:
        mock.close()


def test_publish_stays_silent_when_the_file_list_cannot_be_read(
    tmp_path: Path,
) -> None:
    """An unreadable diff is treated as touching: never risk a second emitter."""

    mock = _MockForgejo()
    mock.files = None
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is False
        assert decision.reason == "files-unavailable"
        assert _dispatches(mock) == []
    finally:
        mock.close()


def test_a_merged_candidate_never_queries_the_diff_or_dispatches(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    mock = _MockForgejo()
    mock.branch = branch
    mock.contents = base64.b64encode(release.record).decode()
    mock.pr = {
        "number": 7,
        "state": "closed",
        "merged": True,
        "head": {"sha": "d" * 40, "ref": branch},
        "base": {"sha": "c" * 40, "ref": "draft"},
    }
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is False
        assert decision.reason == "merged"
        assert _dispatches(mock) == []
        assert not [r for r in mock.requests if r[1].endswith("/files")]
    finally:
        mock.close()


def test_publish_treats_a_rename_out_of_the_workflow_dir_as_touching(
    tmp_path: Path,
) -> None:
    """Forgejo lists the new path as `filename`; a rename out of
    `.github/workflows/` is only visible through `previous_filename`."""

    mock = _MockForgejo()
    mock.files = [
        {"filename": f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md", "status": "added"},
        {
            "filename": "docs/moved-workflow.md",
            "previous_filename": ".github/workflows/release.yml",
            "status": "renamed",
        },
    ]
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is False
        assert decision.reason == "workflow-diff"
        assert _dispatches(mock) == []
    finally:
        mock.close()


def test_publish_does_not_treat_an_unrelated_rename_as_touching(
    tmp_path: Path,
) -> None:
    mock = _MockForgejo()
    mock.files = [
        {"filename": f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md", "status": "added"},
        {
            "filename": "docs/new-name.md",
            "previous_filename": "docs/old-name.md",
            "status": "renamed",
        },
    ]
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is True
        assert decision.reason == "candidate"
    finally:
        mock.close()


def test_workflow_path_definition_is_shared_and_pinned() -> None:
    """One definition for the dispatcher, and it must be >= the #166 gate.

    The gate (HyprialOS/homepage `.github/workflows/homepage-release-gate.yml`
    at #166) filters compare paths with startsWith(".github/workflows/").
    Cross-repo code cannot import, so both sides name each other and this test
    pins the literal the two implementations share.
    """

    assert SYNC.WORKFLOW_PATH_PREFIX == ".github/workflows/"
    assert SYNC.GUARD_FILES_PAGE_LIMIT == 50
    assert SYNC.is_workflow_path(".github/workflows/release.yml")
    assert SYNC.is_workflow_path(".github/workflows")
    assert not SYNC.is_workflow_path(".github/workflowsx/release.yml")
    assert not SYNC.is_workflow_path("docs/changelog/releases/0.4.6.md")


class _TimeoutFilesClient:
    def changed_paths(self, pr):  # type: ignore[no-untyped-def]
        raise TimeoutError("files endpoint timed out")

    def dispatch_guard(self, pr):  # type: ignore[no-untyped-def]
        raise AssertionError("a timeout must never dispatch the guard")


def test_a_files_timeout_is_fail_closed() -> None:
    pr = SYNC.PullRequestState(7, "open", False, b"record", "d" * 40, "c" * 40)

    decision = SYNC.guard_dispatch_decision(_TimeoutFilesClient(), pr)

    assert decision.dispatch is False
    assert decision.reason == "files-unavailable"


def test_exactly_a_full_page_is_fail_closed(tmp_path: Path) -> None:
    """A full page may hide a workflow path on the next page."""

    mock = _MockForgejo()
    mock.files = [f"docs/page/{index}.md" for index in range(50)]
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is False
        assert decision.reason == "files-unavailable"
        assert _dispatches(mock) == []
    finally:
        mock.close()


def _seed_homepage_remote(
    tmp_path: Path,
    *,
    branch: str,
    record: bytes | None,
) -> tuple[Path, Path, str, str | None]:
    """A bare Homepage remote with ``draft`` and one candidate branch.

    ``record is None`` leaves the candidate branch pointing at the draft base
    (the state after ``create_branch`` succeeded and the write did not).
    """

    bare = tmp_path / "homepage.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True
    )
    work = tmp_path / "homepage-work"
    subprocess.run(
        ["git", "init", "-q", "-b", "draft", str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "test")
    (work / "README.md").write_text("home\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "draft")
    if record is None:
        _git(work, "push", "-q", "origin", f"draft:refs/heads/{branch}")
        return bare, work, base, None
    _git(work, "checkout", "-q", "-b", branch)
    path = work / SYNC.RELEASE_RECORD_DIR
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{VERSION}.md").write_bytes(record)
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "old record")
    _git(work, "push", "-q", "origin", branch)
    return bare, work, base, _git(work, "rev-parse", "HEAD")


def _open_pr_mock(
    mock: "_MockForgejo", *, branch: str, head: str, base: str, record: bytes | None
) -> None:
    mock.branch = branch
    mock.branch_head = head
    mock.contents = base64.b64encode(record).decode() if record is not None else None
    mock.pr = {
        "number": 7,
        "state": "open",
        "merged": False,
        "head": {"sha": head, "ref": branch},
        "base": {"sha": base, "ref": "draft"},
    }
    mock.files = [f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md"]


def _blob(bare: Path, ref: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(bare), "show", f"{ref}:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def test_an_update_rewrites_the_candidate_to_one_commit(tmp_path: Path) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    old_record = b"old record\n"
    bare, _work, base, old_head = _seed_homepage_remote(
        tmp_path, branch=branch, record=old_record
    )
    assert old_head is not None
    mock = _MockForgejo()
    _open_pr_mock(mock, branch=branch, head=old_head, base=base, record=old_record)
    try:
        pr, decision = SYNC.publish_candidate(
            mock.client(git_url=str(bare)), release=release
        )

        new_head = _git(bare, "rev-parse", branch)
        assert new_head != old_head
        assert pr.head_sha == new_head
        assert pr.base_sha == base
        assert _git(bare, "rev-list", "--count", f"draft..{branch}") == "1"
        assert _git(bare, "rev-parse", f"{branch}^") == base
        assert _blob(bare, branch, f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md") == release.record
        assert decision.reason == "candidate"
        dispatch = _dispatches(mock)
        assert len(dispatch) == 1
        assert json.loads(dispatch[0][2])["inputs"]["head_sha"] == new_head
    finally:
        mock.close()


def test_a_retry_after_the_update_keeps_the_single_commit(tmp_path: Path) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    bare, _work, base, old_head = _seed_homepage_remote(
        tmp_path, branch=branch, record=b"old record\n"
    )
    mock = _MockForgejo()
    _open_pr_mock(mock, branch=branch, head=old_head, base=base, record=b"old record\n")
    try:
        _pr, _decision = SYNC.publish_candidate(
            mock.client(git_url=str(bare)), release=release
        )
        head = _git(bare, "rev-parse", branch)
        mock.branch_head = head
        mock.contents = base64.b64encode(release.record).decode()
        assert mock.pr is not None
        mock.pr["head"]["sha"] = head

        _pr, decision = SYNC.publish_candidate(
            mock.client(git_url=str(bare)), release=release
        )

        assert _git(bare, "rev-parse", branch) == head
        assert _git(bare, "rev-list", "--count", f"draft..{branch}") == "1"
        assert decision.reason == "candidate"
    finally:
        mock.close()


def test_a_stale_lease_fails_loudly_and_never_moves_the_branch(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    bare, _work, base, old_head = _seed_homepage_remote(
        tmp_path, branch=branch, record=b"old record\n"
    )
    mock = _MockForgejo()
    _open_pr_mock(mock, branch=branch, head=old_head, base=base, record=b"old record\n")
    # The branch moved after the state was read: the lease must refuse.
    mock.branch_head = "e" * 40
    try:
        with pytest.raises(SYNC.ReleaseContractError, match="git push"):
            SYNC.publish_candidate(mock.client(git_url=str(bare)), release=release)

        assert _git(bare, "rev-parse", branch) == old_head
        assert _dispatches(mock) == []
    finally:
        mock.close()


def test_a_branch_only_retry_rewrites_once_and_opens_the_pr(tmp_path: Path) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    bare, _work, base, _head = _seed_homepage_remote(
        tmp_path, branch=branch, record=None
    )
    mock = _MockForgejo()
    mock.branch = branch
    mock.branch_head = base
    mock.pr = None
    mock.files = [f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md"]
    try:
        pr, _decision = SYNC.publish_candidate(
            mock.client(git_url=str(bare)), release=release
        )

        head = _git(bare, "rev-parse", branch)
        assert pr.head_sha == head
        assert _git(bare, "rev-list", "--count", f"draft..{branch}") == "1"
        assert mock.pr is not None
        assert any(r[0] == "POST" and r[1].endswith("/pulls") for r in mock.requests)
        assert _blob(bare, branch, f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md") == release.record
    finally:
        mock.close()


def test_a_branch_only_retry_with_matching_bytes_only_opens_the_pr(
    tmp_path: Path,
) -> None:
    release = _release(tmp_path)
    branch = SYNC._candidate_branch(release.facts.tag)
    bare, _work, _base, head = _seed_homepage_remote(
        tmp_path, branch=branch, record=release.record
    )
    assert head is not None
    mock = _MockForgejo()
    mock.branch = branch
    mock.branch_head = head
    mock.contents = base64.b64encode(release.record).decode()
    mock.pr = None
    mock.files = [f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md"]
    try:
        pr, _decision = SYNC.publish_candidate(
            mock.client(git_url=str(bare)), release=release
        )

        assert pr.head_sha == head
        assert _git(bare, "rev-parse", branch) == head
        assert _git(bare, "rev-list", "--count", f"draft..{branch}") == "1"
        assert mock.pr is not None
    finally:
        mock.close()


def test_publish_rename_into_the_workflow_dir_suppresses_dispatch(
    tmp_path: Path,
) -> None:
    mock = _MockForgejo()
    mock.files = [
        {"filename": f"{SYNC.RELEASE_RECORD_DIR}{VERSION}.md", "status": "added"},
        {
            "filename": ".github/workflows/release.yml",
            "previous_filename": "docs/release.yml",
            "status": "renamed",
        },
    ]
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is False
        assert decision.reason == "workflow-diff"
        assert _dispatches(mock) == []
    finally:
        mock.close()


def test_publish_49_entries_is_below_the_page_limit_and_dispatches(
    tmp_path: Path,
) -> None:
    mock = _MockForgejo()
    mock.files = [f"docs/page/{index}.md" for index in range(49)]
    try:
        _pr, decision = _publish(mock, tmp_path)

        assert decision.dispatch is True
        assert decision.reason == "candidate"
        assert len(_dispatches(mock)) == 1
    finally:
        mock.close()


def test_the_candidate_token_travels_only_in_the_hardened_child_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mechanism for the documented claim, not the production push itself.

    A real HTTPS push with the production token stays UNKNOWN (PR body): this
    pins *how* the token is handed to git, so the documented "never in the URL
    or argv" cannot silently rot.  The remote path needs no credential at all.
    """

    for key in tuple(os.environ):
        if key.startswith("GIT_") or key == "XDG_CONFIG_HOME":
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://attacker.invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://code.hyprial.com/")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "attacker.cfg"))
    monkeypatch.setenv("GIT_DIR", "/attacker/repo")
    monkeypatch.setenv("GIT_ASKPASS", "/attacker/askpass")
    monkeypatch.setenv("GIT_PAGER", "/attacker/pager")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/attacker/xdg")
    client = SYNC.ForgejoHomepageClient(
        api_url="https://code.hyprial.com/api/v1",
        repo="HyprialOS/homepage",
        candidate_token="secret-token",
    )
    assert client.git_url == "https://code.hyprial.com/HyprialOS/homepage.git"
    assert "secret-token" not in client.git_url
    env = client._git_env()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: token secret-token"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in env
    assert "GIT_ASKPASS" not in env
    assert "GIT_PAGER" not in env
    assert "XDG_CONFIG_HOME" not in env
    warning = capsys.readouterr().err
    assert warning == (
        "warning: ignored inherited git environment: GIT_ASKPASS, "
        "GIT_CONFIG_COUNT, GIT_CONFIG_GLOBAL, GIT_CONFIG_KEY_0, "
        "GIT_CONFIG_VALUE_0, GIT_DIR, GIT_PAGER, XDG_CONFIG_HOME\n"
    )
    assert "/attacker/askpass" not in warning
    assert "https://attacker.invalid/" not in warning
    assert "https://code.hyprial.com/" not in warning
    assert "/attacker/repo" not in warning
    assert "/attacker/xdg" not in warning
    with pytest.raises(SYNC.ReleaseContractError, match="git rev-parse"):
        client._git(tmp_path, "rev-parse", "--verify", "no-such-revision")

    capsys.readouterr()
    for key in tuple(os.environ):
        if key.startswith("GIT_") or key == "XDG_CONFIG_HOME":
            monkeypatch.delenv(key)
    local = SYNC.ForgejoHomepageClient(
        api_url="https://code.hyprial.com/api/v1",
        repo="HyprialOS/homepage",
        candidate_token="secret-token",
        git_url=str(tmp_path / "homepage.git"),
    )
    local_env = local._git_env()
    assert "GIT_CONFIG_COUNT" not in local_env
    assert "secret-token" not in " ".join(local_env.values())
    assert capsys.readouterr().err == ""
    with pytest.raises(SYNC.ReleaseContractError) as error:
        local._git(tmp_path, "rev-parse", "--verify", "no-such-revision")
    assert "secret-token" not in str(error.value)
