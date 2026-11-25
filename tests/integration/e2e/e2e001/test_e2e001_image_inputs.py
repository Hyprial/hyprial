"""Guard tests: the E2E-001 image reuse decision is keyed on build inputs.

spec-e2e001 (2026-09-14) + 追加 4: the fixed-tag image is reused ONLY while
its ``hyprial.e2e001.inputs`` label equals a hash computed from what the
Dockerfile actually consumes.  These tests lock in:

  * the copy-source parser returns every COPY/ADD source (the anti-drift
    guard 追加 4(a) asked for — the list is derived, so the guard is that
    derivation drops nothing);
  * a notes-only commit leaves the hash unchanged, a src edit changes it
    (验收 1, both cells);
  * unmodelled Dockerfile shapes fall back to the whole tree (追加 4(b),
    宁可多建);
  * ``build_image`` reuses on label match, rebuilds on mismatch / dirty /
    --rebuild, and stamps the label on every build;
  * the post-build sweep keeps the newest two labeled images and the
    protected current one (追加 1.3), touching nothing unlabeled.

The git-backed tests run against a disposable repository, never this one.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCENARIO_DIR = ROOT / "contract/e2e"
RUNNER_PATH = SCENARIO_DIR / "run-two-machine-roundtrip.py"
DOCKERFILE_PATH = SCENARIO_DIR / "docker/Dockerfile"


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def image_inputs():  # type: ignore[no-untyped-def]
    scenario_dir = str(SCENARIO_DIR)
    if scenario_dir not in sys.path:
        sys.path.insert(0, scenario_dir)
    return _load("e2e001_image_inputs", SCENARIO_DIR / "_image_inputs.py")


@pytest.fixture()
def runner():  # type: ignore[no-untyped-def]
    scenario_dir = str(SCENARIO_DIR)
    if scenario_dir not in sys.path:
        sys.path.insert(0, scenario_dir)
    return _load("e2e001_runner_for_inputs", RUNNER_PATH)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A miniature harness-bridge: the paths the real Dockerfile COPYs."""

    root = tmp_path / "bridge"
    (root / "src/hyprial").mkdir(parents=True)
    (root / "sidecar/hyprial-tailcat").mkdir(parents=True)
    (root / "contract/e2e/docker").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='hyprial'\n", encoding="utf-8")
    (root / "uv.lock").write_text("lock-v1\n", encoding="utf-8")
    (root / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    (root / "src/hyprial/__init__.py").write_text("version = '1'\n", encoding="utf-8")
    (root / "sidecar/hyprial-tailcat/main.go").write_text("package main\n", encoding="utf-8")
    (root / "contract/e2e/docker/entrypoint.sh").write_text(
        "#!/bin/sh\ntrue\n", encoding="utf-8"
    )
    (root / "contract/e2e/docker/machine_identity.py").write_text(
        (SCENARIO_DIR / "docker/machine_identity.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "contract/e2e/docker/Dockerfile").write_text(
        DOCKERFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "notes/notes.md").write_text("notes v1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


# ---------------------------------------------------------------------------
# Parser: every COPY/ADD source is derived, nothing dropped (追加 4(a)).
# ---------------------------------------------------------------------------


def test_real_dockerfile_sources_are_complete(image_inputs) -> None:  # type: ignore[no-untyped-def]
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert image_inputs.dockerfile_copy_sources(text) == (
        "pyproject.toml",
        "uv.lock",
        "VERSION",
        "src",
        "sidecar/hyprial-tailcat",
        "contract/e2e/docker/machine_identity.py",
        "contract/e2e/docker/entrypoint.sh",
    )


def test_parser_keeps_every_source_across_continuations(image_inputs) -> None:  # type: ignore[no-untyped-def]
    text = (
        "FROM python:3.12-slim\n"
        "# a comment\n"
        "COPY --chmod=755 a.sh \\\n    b.sh /dst/\n"
        "ADD dir /dst/dir\n"
        "RUN true\n"
    )
    assert image_inputs.dockerfile_copy_sources(text) == ("a.sh", "b.sh", "dir")


@pytest.mark.parametrize(
    "line",
    [
        "COPY . /app",
        "COPY src/*.py /app",
        "COPY --from=builder /out /app",
        "ADD https://example.com/x /app",
        "COPY /abs /app",
    ],
)
def test_unmodelled_shapes_are_refused(image_inputs, line: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(image_inputs.InputsHashError):
        image_inputs.dockerfile_copy_sources(f"FROM x\n{line}\n")


# ---------------------------------------------------------------------------
# Hash semantics: notes-only reuse cell and src-change rebuild cell (验收 1).
# ---------------------------------------------------------------------------


def test_hash_stable_and_exact_on_clean_repo(image_inputs, repo: Path) -> None:
    first, exact = image_inputs.inputs_hash(repo)
    second, exact_again = image_inputs.inputs_hash(repo)
    assert first == second and exact is True and exact_again is True
    assert image_inputs.dirty_inputs(repo) is False


def test_notes_only_commit_leaves_hash_unchanged(image_inputs, repo: Path) -> None:
    before, _ = image_inputs.inputs_hash(repo)
    (repo / "notes/notes.md").write_text("notes v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "notes only")
    after, _ = image_inputs.inputs_hash(repo)
    assert after == before


@pytest.mark.parametrize("changed", ["go-source", "go-toolchain", "machine-identity"])
def test_runtime_build_inputs_change_the_image_identity(image_inputs, repo, changed):
    before, exact = image_inputs.inputs_hash(repo)
    assert exact is True
    if changed == "go-source":
        (repo / "sidecar/hyprial-tailcat/main.go").write_text("package main\nfunc main() {}\n")
    elif changed == "go-toolchain":
        dockerfile = repo / "contract/e2e/docker/Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace("golang:1.27.1", "golang:1.27.2"))
    else:
        helper = repo / "contract/e2e/docker/machine_identity.py"
        helper.write_text(helper.read_text() + "\nCHANGED = True\n")
    assert image_inputs.dirty_inputs(repo) is True
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Go build input changed")
    after, exact = image_inputs.inputs_hash(repo)
    assert exact is True and after != before


def test_src_edit_changes_hash(image_inputs, repo: Path) -> None:
    before, _ = image_inputs.inputs_hash(repo)
    (repo / "src/hyprial/__init__.py").write_text("version = '2'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "src edit")
    after, _ = image_inputs.inputs_hash(repo)
    assert after != before


def test_dockerfile_edit_changes_hash(image_inputs, repo: Path) -> None:
    """The Dockerfile is its own build input: changing a RUN line (no COPY
    source moved) must still invalidate the image."""

    before, _ = image_inputs.inputs_hash(repo)
    dockerfile = repo / "contract/e2e/docker/Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(
            "ENV UV_LINK_MODE=copy", "ENV UV_LINK_MODE=hardlink"
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dockerfile edit")
    after, _ = image_inputs.inputs_hash(repo)
    assert after != before


def test_dirty_working_tree_inputs_flagged(image_inputs, repo: Path) -> None:
    assert image_inputs.dirty_inputs(repo) is False
    (repo / "src/hyprial/__init__.py").write_text("version = 'dirty'\n", encoding="utf-8")
    assert image_inputs.dirty_inputs(repo) is True


def test_unmodelled_dockerfile_falls_back_to_whole_tree(image_inputs, repo: Path) -> None:
    dockerfile = repo / "contract/e2e/docker/Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8") + "COPY . /everything\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "copy dot")
    whole_tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    value, exact = image_inputs.inputs_hash(repo)
    assert exact is False and value == whole_tree


# ---------------------------------------------------------------------------
# build_image decision + label stamping (monkeypatched docker).
# ---------------------------------------------------------------------------


class FakeDocker:
    def __init__(self, *, label: str | None, image_id: str = "sha256:current") -> None:
        self.label = label
        self.image_id = image_id
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, timeout: float = 60.0) -> str:
        self.calls.append(args)
        if args[:2] == ["image", "inspect"] and args[2] == "hyprial-e2e001:local":
            if "{index" in " ".join(args):  # label query
                return self.label or ""
            if "{{.Id}}" in " ".join(args):
                return self.image_id
            return "inspect-ok"
        if args[:2] == ["images", "-a"]:
            return "sha256:current\nsha256:old1\nsha256:old2\nsha256:old3"
        if args[:1] == ["build"]:
            return ""
        return ""


def _patched_hashes(monkeypatch, runner_module, repo: Path) -> str:  # type: ignore[no-untyped-def]
    scenario_dir = str(SCENARIO_DIR)
    if scenario_dir not in sys.path:
        sys.path.insert(0, scenario_dir)
    module = _load("e2e001_image_inputs_for_runner", SCENARIO_DIR / "_image_inputs.py")
    monkeypatch.setattr(
        runner_module,
        "inputs_hash",
        lambda root, rev="HEAD": (module.inputs_hash(repo), True),
    )
    monkeypatch.setattr(
        runner_module,
        "dirty_inputs",
        lambda root, rev="HEAD": module.dirty_inputs(repo),
    )
    return module.inputs_hash(repo)


def test_build_image_reuses_only_on_exact_label_match(runner, monkeypatch, repo, capsys) -> None:  # type: ignore[no-untyped-def]
    hash_value = _patched_hashes(monkeypatch, runner, repo)
    fake = FakeDocker(label=hash_value)
    monkeypatch.setattr(runner, "docker", fake)
    runner.build_image(force=False)
    out = capsys.readouterr().out
    assert not any(call[:1] == ["build"] for call in fake.calls)
    assert "present" in out and "matched" in out


def test_build_image_rebuilds_on_label_mismatch_and_stamps_label(runner, monkeypatch, repo, capsys) -> None:  # type: ignore[no-untyped-def]
    hash_value = _patched_hashes(monkeypatch, runner, repo)
    fake = FakeDocker(label="0" * 64)  # stale label from an older tree
    monkeypatch.setattr(runner, "docker", fake)
    runner.build_image(force=False)
    builds = [call for call in fake.calls if call[:1] == ["build"]]
    assert len(builds) == 1
    assert f"hyprial.e2e001.inputs={hash_value}" in builds[0]


def test_build_image_rebuilds_when_dirty_even_if_label_matches(runner, monkeypatch, repo) -> None:  # type: ignore[no-untyped-def]
    hash_value = _patched_hashes(monkeypatch, runner, repo)
    (repo / "src/hyprial/__init__.py").write_text("dirty\n", encoding="utf-8")
    fake = FakeDocker(label=hash_value)
    monkeypatch.setattr(runner, "docker", fake)
    runner.build_image(force=False)
    builds = [call for call in fake.calls if call[:1] == ["build"]]
    assert any(call[:1] == ["build"] for call in fake.calls)
    # ...and what it builds can never be reused as if it were clean-built:
    # the dirty build stamps a suffixed label that no clean computation of
    # the hash can ever match.
    assert any(
        f"hyprial.e2e001.inputs={hash_value}+dirty" in call for call in builds
    ), builds


def test_sweep_selects_only_inputs_labeled_images(runner, monkeypatch) -> None:
    """批复 §1b: selection must be delegated to the inputs-label filter —
    an image carrying only hyprial-e2e001=1 (pre-change build) is never a
    candidate."""

    captured: list[list[str]] = []

    def docker(args: list[str], *, timeout: float = 60.0) -> str:
        captured.append(args)
        if args[:2] == ["images", "-a"]:
            return "sha256:only"
        if args[:2] == ["image", "inspect"]:
            return "sha256:only 2026-09-14T10:00:00Z"
        return ""

    monkeypatch.setattr(runner, "docker", docker)
    runner.labeled_image_entries()
    listing = [call for call in captured if call[:2] == ["images", "-a"]]
    assert listing == [["images", "-a", "-q", "--filter", "label=hyprial.e2e001.inputs"]]


def test_sweep_keeps_newest_two_and_protected_current(runner, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ages = {
        "sha256:current": "2026-09-14T10:00:00Z",
        "sha256:old1": "2026-09-13T10:00:00Z",
        "sha256:old2": "2026-09-12T10:00:00Z",
        "sha256:old3": "2026-09-11T10:00:00Z",
    }
    fake = FakeDocker(label=None)
    fake.calls.clear()

    def docker(args: list[str], *, timeout: float = 60.0) -> str:
        fake.calls.append(args)
        if args[:2] == ["images", "-a"]:
            return "\n".join(ages)
        if args[:2] == ["image", "inspect"]:
            return f"{args[2]} {ages[args[2]]}"
        return ""

    monkeypatch.setattr(runner, "docker", docker)
    removed = runner.sweep_old_labeled_images(keep=2, protect="sha256:current")
    assert removed == ["sha256:old3"]
    rmi = [call for call in fake.calls if call[:2] == ["rmi", "-f"]]
    assert rmi == [["rmi", "-f", "sha256:old3"]]


# ---------------------------------------------------------------------------
# Proxy build-args: the image build reaches PyPI through the job's proxy.
# ---------------------------------------------------------------------------


def test_proxy_build_args_rewrite_loopback_and_pass_no_proxy(runner) -> None:  # type: ignore[no-untyped-def]
    """e2e-macos-isolated.yml exports a loopback proxy; inside the build container that is
    host.docker.internal.  NO_PROXY passes through unchanged."""

    args = runner.proxy_build_args(
        {
            "HTTPS_PROXY": "http://127.0.0.1:7896",
            "http_proxy": "http://localhost:7896/",
            "NO_PROXY": "localhost,127.0.0.1,.ts.net",
            "UNRELATED": "x",
        },
        platform="darwin",
    )
    assert args == [
        "--build-arg", "HTTPS_PROXY=http://host.docker.internal:7896",
        "--build-arg", "NO_PROXY=localhost,127.0.0.1,.ts.net",
        "--build-arg", "http_proxy=http://host.docker.internal:7896/",
    ]


def test_proxy_build_args_drop_loopback_off_macos(runner) -> None:  # type: ignore[no-untyped-def]
    """host.docker.internal only resolves by default on macOS Docker; elsewhere a
    loopback proxy is unreachable from the build, so the build keeps its direct path."""

    args = runner.proxy_build_args(
        {"HTTPS_PROXY": "http://127.0.0.1:7896", "NO_PROXY": "localhost"}, platform="linux"
    )
    assert args == ["--build-arg", "NO_PROXY=localhost"]


def test_proxy_build_args_keep_non_loopback_and_skip_unset(runner) -> None:  # type: ignore[no-untyped-def]
    assert runner.proxy_build_args({}) == []
    assert runner.proxy_build_args({"HTTP_PROXY": "", "HTTPS_PROXY": "http://proxy.corp:3128"}) == [
        "--build-arg", "HTTPS_PROXY=http://proxy.corp:3128",
    ]


def test_build_image_passes_proxy_build_args_and_keeps_label(runner, monkeypatch, repo) -> None:  # type: ignore[no-untyped-def]
    hash_value = _patched_hashes(monkeypatch, runner, repo)
    fake = FakeDocker(label="0" * 64)
    monkeypatch.setattr(runner, "docker", fake)
    for name in runner.PROXY_BUILD_ARGS:
        monkeypatch.delenv(name, raising=False)
    # Non-loopback, so the assertion holds on the Linux CI runner too.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    runner.build_image(force=False)
    (build,) = [call for call in fake.calls if call[:1] == ["build"]]
    assert "HTTPS_PROXY=http://proxy.corp:3128" in build
    assert f"hyprial.e2e001.inputs={hash_value}" in build


def test_build_image_mounts_optional_ca_without_copying_its_contents(runner, monkeypatch, repo, tmp_path):
    hash_value = _patched_hashes(monkeypatch, runner, repo)
    certificate = tmp_path / "public-ca.pem"
    certificate.write_text("UNIT_CA_CONTENT_STAYS_OUT_OF_ARGUMENTS")
    monkeypatch.setenv("HYPRIAL_E2E_DOCKER_CA_BUNDLE", str(certificate))
    fake = FakeDocker(label=None)
    monkeypatch.setattr(runner, "docker", fake)
    runner.build_image(force=True)
    (build,) = [call for call in fake.calls if call[:1] == ["build"]]
    assert build[build.index("--secret") + 1] == f"id=hyprial_e2e_ca,src={certificate}"
    assert f"hyprial.e2e001.inputs={hash_value}" in build
    assert "UNIT_CA_CONTENT" not in str(fake.calls)


@pytest.mark.parametrize("path_kind", ["missing", "directory", "comma"])
def test_build_image_rejects_unusable_ca_before_build(runner, monkeypatch, repo, tmp_path, path_kind):
    _patched_hashes(monkeypatch, runner, repo)
    certificate = tmp_path / ("ca,certificate.pem" if path_kind == "comma" else "ca.pem")
    if path_kind == "directory":
        certificate.mkdir()
    elif path_kind == "comma":
        certificate.write_text("CA")
    monkeypatch.setenv("HYPRIAL_E2E_DOCKER_CA_BUNDLE", str(certificate))
    fake = FakeDocker(label=None)
    monkeypatch.setattr(runner, "docker", fake)
    with pytest.raises(runner.DockerError, match="CA bundle"):
        runner.build_image(force=True)
    assert not any(call[:1] == ["build"] for call in fake.calls)


def test_failed_evidence_capture_preserves_the_original_build_error(runner, monkeypatch, capsys):
    def build_failure(**kwargs):
        raise runner.DockerError("BUILD_FAILURE_SENTINEL")

    def evidence_failure(*args):
        raise OSError("EVIDENCE_FAILURE_SENTINEL")

    monkeypatch.setattr(sys, "argv", [str(RUNNER_PATH)])
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: {})
    monkeypatch.setattr(runner, "run_docker", build_failure)
    monkeypatch.setattr(runner, "_capture_docker_state", lambda *args: None)
    monkeypatch.setattr(runner, "capture_failure", evidence_failure)
    monkeypatch.setattr(runner, "remove_resources", lambda: [])
    monkeypatch.setattr(runner, "self_check", lambda: [])
    assert runner.main() == 1
    output = capsys.readouterr().out
    assert "BUILD_FAILURE_SENTINEL" in output
    assert "EVIDENCE_FAILURE_SENTINEL" in output


# ---------------------------------------------------------------------------
# Proxy credentials never reach error text (card 4fc47853).
# ---------------------------------------------------------------------------


def test_redact_argv_masks_url_userinfo_by_value(runner) -> None:  # type: ignore[no-untyped-def]
    args = [
        "build",
        "--build-arg", "HTTPS_PROXY=http://alice:s3cret@proxy.corp:3128",
        "--build-arg", "NO_PROXY=localhost",
        "--label", "hyprial.e2e001.inputs=abc",
        "socks5://bob:pw@10.0.0.1:1080",
    ]
    redacted = runner.redact_argv(args)
    joined = " ".join(redacted)
    assert "s3cret" not in joined and "alice" not in joined and ":pw@" not in joined
    assert "HTTPS_PROXY=http://***@proxy.corp:3128" in redacted
    assert "socks5://***@10.0.0.1:1080" in redacted
    assert "hyprial.e2e001.inputs=abc" in redacted  # untouched


def test_failing_command_error_text_has_no_proxy_password(runner) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(runner.DockerError) as caught:
        runner.sh(["false", "--build-arg", "HTTPS_PROXY=http://alice:s3cret@proxy.corp:3128"])
    assert "s3cret" not in str(caught.value)
    assert "http://***@proxy.corp:3128" in str(caught.value)


def test_timeout_error_text_has_no_proxy_password(runner) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        runner.sh(
            [sys.executable, "-c", "import time; time.sleep(5)", "HTTPS_PROXY=http://alice:s3cret@p:1"],
            timeout=0.3,
        )
    assert "s3cret" not in str(caught.value)


def test_failing_command_output_tail_is_redacted_too(runner) -> None:  # type: ignore[no-untyped-def]
    """pip/uv/curl echo proxy URLs into their output; the stdout/stderr tail
    DockerError carries must be masked as well, not just the argv."""

    script = "import sys; sys.stderr.write('ProxyError: http://alice:s3cret@proxy.corp:3128 refused'); sys.exit(1)"
    with pytest.raises(runner.DockerError) as caught:
        runner.sh([sys.executable, "-c", script])
    assert "s3cret" not in str(caught.value)
    assert "http://***@proxy.corp:3128" in str(caught.value)


def test_timeout_output_is_redacted(runner) -> None:  # type: ignore[no-untyped-def]
    script = "import sys, time; print('via http://alice:s3cret@p:1', flush=True); time.sleep(5)"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        runner.sh([sys.executable, "-c", script], timeout=1.0)
    output = caught.value.output or b""
    text = output.decode() if isinstance(output, bytes) else output
    assert "s3cret" not in text


def test_unencoded_at_in_password_is_masked_in_full(runner) -> None:  # type: ignore[no-untyped-def]
    assert runner.redact_argv(["HTTPS_PROXY=http://alice:p@ss@proxy.corp:3128"]) == [
        "HTTPS_PROXY=http://***@proxy.corp:3128"
    ]
