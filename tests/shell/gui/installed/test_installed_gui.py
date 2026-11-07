"""Installed-wheel GUI resources and exact-SHA development bootstrap."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui import worktree as auto_worktree
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import dev_source
from hyprial.shell.impl.gui.install import launch as target_launch
from hyprial.shell.impl.gui.install import resources
from hyprial.shell.impl.gui.install import server as static_server
from hyprial.shell.impl.gui.project import GuiProject


def _assets(
    tmp_path: Path,
    *,
    commit: str = "a" * 40,
    remote: str = "https://example.invalid/hyprial",
) -> Path:
    root = tmp_path / "wheel-gui"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>installed</h1>\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "app.js").write_text("console.log('installed');\n", encoding="utf-8")
    manifest = resources.build_manifest(
        root,
        package_version="0.5.5.1",
        source_commit=commit,
        source_remote=remote,
        gui_version="0.0.0",
    )
    resources.write_manifest(root, manifest)
    return root


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    (source / "gui").mkdir()
    (source / "gui" / "package.json").write_text(
        json.dumps({"name": "@hyprial/gui-app", "scripts": {"dev": "vite"}}),
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "fixture")
    commit = _git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    return source, remote, commit


def _fake_project(home: Path) -> GuiProject:
    return GuiProject(
        home=home,
        worktree=home.parent,
        branch="(detached)",
        node="node",
        npm="npm",
        node_version="v24.0.0",
        npm_version="11.0.0",
        scripts={"dev": "vite", "stub": "stub"},
    )


def _no_source(**kwargs):
    raise GuiError(auto_worktree.SOURCE_CODE, "no source checkout")


class _Runner:
    def __init__(self) -> None:
        self.npm_calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        if argv and argv[0] == "npm":
            self.npm_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, **kwargs)


def _which(name: str):
    if name == "npm":
        return "npm"
    return shutil.which(name)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "HYPRIAL_HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "user-home"),
    }


def test_normal_installed_assets_use_python_server_without_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _assets(tmp_path)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: root.resolve()
    )

    plan = launcher.build_plan(
        "",
        environ={"HYPRIAL_DEV_GUI_HOME": str(tmp_path / "unused-dev-home")},
        cwd=tmp_path,
        inspect=lambda *a, **k: pytest.fail("installed normal mode must not inspect npm"),
    )

    assert plan.project.installed is True
    assert plan.project.source_commit == "a" * 40
    assert plan.installed_assets is not None
    assert plan.build_argv is None
    assert plan.backend_owned is False
    assert plan.frontend_argv == (
        sys.executable,
        "-m",
        "hyprial.shell.impl.gui.install.server",
        "--root",
        str(root.resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        "4173",
    )


def test_explicit_normal_home_serves_verified_build_without_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _assets(tmp_path)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)

    plan = launcher.build_plan(
        "",
        gui_home=root,
        environ={},
        cwd=tmp_path,
        inspect=lambda *a, **k: pytest.fail("built normal home must not inspect npm"),
    )

    assert plan.project.installed is True
    assert plan.project.home == root.resolve()
    assert plan.installed_assets is not None
    assert plan.frontend_argv[0] == sys.executable
    assert plan.backend_argv is None


def test_invalid_explicit_normal_home_does_not_fall_back_to_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={"HYPRIAL_GUI_HOME": str(empty)},
            cwd=tmp_path,
            inspect=lambda *a, **k: pytest.fail("invalid home reached inspect"),
        )

    assert caught.value.code == "GUI_HOME_INVALID"
    assert str(empty) in str(caught.value)


def test_explicit_normal_home_with_only_source_package_is_not_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps({"name": "@hyprial/gui-app", "scripts": {"start": "vite"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            gui_home=source,
            environ={},
            cwd=tmp_path,
            inspect=lambda *a, **k: pytest.fail(
                "explicit normal mode must require built assets, not source"
            ),
        )

    assert caught.value.code == "GUI_HOME_INVALID"
    assert "no built GUI" in str(caught.value)


def test_built_in_assets_are_verified_and_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _assets(tmp_path)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: root.resolve()
    )
    (root / "index.html").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={},
            cwd=tmp_path,
            inspect=lambda *a, **k: pytest.fail("tampered assets reached inspect"),
        )

    assert caught.value.code == "GUI_ASSETS_INVALID"


def test_dual_home_variables_are_independent_and_same_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    normal = tmp_path / "normal"
    dev = tmp_path / "dev"
    normal.mkdir()
    dev.mkdir()

    plan = launcher.build_plan(
        "dev",
        environ={
            "HYPRIAL_GUI_HOME": str(normal),
            "HYPRIAL_DEV_GUI_HOME": str(dev),
        },
        cwd=tmp_path,
        inspect=lambda home, **k: _fake_project(home),
    )
    assert plan.project.home == dev.resolve()
    assert plan.auto_worktree is None
    assert plan.env["HYPRIAL_GUI_HOME"] == str(dev.resolve())

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            environ={
                "HYPRIAL_GUI_HOME": str(normal),
                "HYPRIAL_DEV_GUI_HOME": str(normal),
            },
            cwd=tmp_path,
            inspect=lambda home, **k: _fake_project(home),
        )
    assert caught.value.code == "GUI_DEV_HOME_INVALID"


def test_built_in_asset_directory_is_not_a_dev_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _assets(tmp_path)
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: root.resolve()
    )

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            gui_home=root,
            environ={},
            cwd=tmp_path,
            inspect=lambda *a, **k: pytest.fail("asset directory reached inspect"),
        )
    assert caught.value.code == "GUI_DEV_HOME_INVALID"


def test_dev_ignores_hyprial_gui_home_and_uses_installed_exact_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets(tmp_path)
    normal = tmp_path / "normal-home"
    normal.mkdir()
    (normal / "package.json").write_text("{}", encoding="utf-8")
    auto_path = tmp_path / "managed" / "worktrees" / ("a" * 40)
    auto_home = auto_path / "gui"
    auto_home.mkdir(parents=True)
    install = auto_worktree.DependencyInstall(
        command=(), cwd=auto_home, ran=False, reason="node_modules already exists"
    )
    auto = auto_worktree.AutoGuiWorktree(
        source=tmp_path / "managed" / "source.git",
        path=auto_path,
        branch="(detached)",
        base="a" * 40,
        home=auto_home,
        created=True,
        install=install,
        managed=True,
        remote=str(tmp_path / "remote.git"),
    )
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: assets.resolve()
    )
    monkeypatch.setattr(
        target_launch,
        "ensure_installed_dev_worktree",
        lambda *a, **k: auto,
    )
    monkeypatch.setattr(launcher, "ensure_dev_worktree", _no_source)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    work = tmp_path / "empty-cwd"
    work.mkdir()

    plan = launcher.build_plan(
        "dev",
        environ={"HYPRIAL_GUI_HOME": str(normal)},
        cwd=work,
        inspect=lambda home, **k: _fake_project(home),
        which=_which,
    )

    assert plan.auto_worktree is auto
    assert plan.project.home == auto_home
    assert plan.env["HYPRIAL_GUI_HOME"] == str(auto_home)


def test_installed_dev_missing_git_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets(tmp_path)
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: assets.resolve()
    )
    monkeypatch.setattr(launcher, "ensure_dev_worktree", _no_source)
    work = tmp_path / "empty-cwd"
    work.mkdir()

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            environ=_env(tmp_path),
            cwd=work,
            inspect=lambda *a, **k: pytest.fail("missing Git reached inspect"),
            which=lambda name: None,
        )
    assert caught.value.code == "GUI_GIT_MISSING"
    assert "install Git" in str(caught.value)


def test_installed_dev_bootstraps_exact_sha_and_preserves_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, remote, commit = _source_repo(tmp_path)
    assets = _assets(tmp_path, commit=commit, remote=str(remote))
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: assets.resolve()
    )
    monkeypatch.setattr(launcher, "ensure_dev_worktree", _no_source)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    runner = _Runner()
    env = _env(tmp_path)
    work = tmp_path / "empty-cwd"
    work.mkdir()

    def inspect(home, **kwargs):
        return _fake_project(home)

    plan = launcher.build_plan(
        "dev",
        environ=env,
        cwd=work,
        inspect=inspect,
        which=_which,
        run=runner,
    )

    assert plan.auto_worktree is not None
    auto = plan.auto_worktree
    assert auto.managed is True
    assert auto.base == commit
    assert _git(auto.path, "rev-parse", "HEAD") == commit
    assert (auto.home / "package.json").is_file()
    assert auto.dirty is False
    assert auto.install.ran is True
    assert commit in plan.dev_prompt
    assert "does not auto-update" in plan.dev_prompt
    assert str(auto.path) in plan.dev_prompt
    assert f"git -C {auto.path} fetch origin" in plan.dev_prompt
    assert f"git -C {auto.path} status" in plan.dev_prompt
    assert f"git -C {auto.path} rebase origin/dev" in plan.dev_prompt

    (auto.path / "local-note.txt").write_text("keep me", encoding="utf-8")
    again = launcher.build_plan(
        "dev",
        environ=env,
        cwd=work,
        inspect=inspect,
        which=_which,
        run=runner,
    )
    assert again.auto_worktree is not None
    assert again.auto_worktree.created is False
    assert again.auto_worktree.dirty is True
    assert (again.auto_worktree.path / "local-note.txt").read_text() == "keep me"
    assert _git(again.auto_worktree.path, "rev-parse", "HEAD") == commit


def test_installed_dev_refuses_a_worktree_at_a_different_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, remote, commit = _source_repo(tmp_path)
    assets = _assets(tmp_path, commit=commit, remote=str(remote))
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: assets.resolve()
    )
    monkeypatch.setattr(launcher, "ensure_dev_worktree", _no_source)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    runner = _Runner()
    env = _env(tmp_path)
    work = tmp_path / "empty-cwd"
    work.mkdir()

    first = launcher.build_plan(
        "dev",
        environ=env,
        cwd=work,
        inspect=lambda home, **k: _fake_project(home),
        which=_which,
        run=runner,
    )
    tree = first.auto_worktree.path
    subprocess.run(
        [
            "git", "-C", str(tree), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit",
            "--allow-empty", "-qm", "local",
        ],
        check=True,
    )

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            environ=env,
            cwd=work,
            inspect=lambda home, **k: _fake_project(home),
            which=_which,
            run=runner,
        )
    assert caught.value.code == "GUI_DEV_SOURCE_CONFLICT"
    assert _git(tree, "rev-parse", "HEAD") != commit


def test_installed_dev_offline_missing_object_is_a_clear_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, remote, _commit = _source_repo(tmp_path)
    assets = _assets(tmp_path, commit="b" * 40, remote=str(remote))
    monkeypatch.setattr(
        target_launch, "installed_asset_root", lambda: assets.resolve()
    )
    monkeypatch.setattr(launcher, "ensure_dev_worktree", _no_source)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    env = _env(tmp_path)
    work = tmp_path / "empty-cwd"
    work.mkdir()

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            environ=env,
            cwd=work,
            inspect=lambda home, **k: _fake_project(home),
            which=_which,
            run=_Runner(),
        )
    assert caught.value.code == "GUI_DEV_SOURCE_MISSING"
    assert "never substitutes a latest branch" in str(caught.value)
    assert "b" * 40 in str(caught.value)


def _dev_session(plan: launcher.GuiLaunchPlan) -> launcher.GuiSession:
    return launcher.GuiSession(
        plan=plan,
        frontend=launcher.GuiChild(
            "frontend", SimpleNamespace(pid=321), None
        ),
        backend=None,
    )


@pytest.mark.parametrize(
    ("remote_url", "expected_display"),
    [
        (
            "https://user:s3cr3t@example.invalid/hyprial.git",
            "https://example.invalid/hyprial.git",
        ),
        (
            "https://example.invalid/hyprial.git?token=s3cr3t",
            "https://example.invalid/hyprial.git",
        ),
        (
            "https://example.invalid/hyprial.git#s3cr3t",
            "https://example.invalid/hyprial.git",
        ),
        (
            "git@example.invalid:hyprial.git?token=s3cr3t",
            "example.invalid:hyprial.git",
        ),
        (
            "ssh://git@example.invalid/hyprial.git",
            "ssh://git@example.invalid/hyprial.git",
        ),
    ],
)
def test_explicit_dev_remote_credentials_never_reach_prompt_or_json(
    tmp_path: Path, remote_url: str, expected_display: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Port availability belongs to test_gui_launcher's dedicated case; binding
    # 4173 here made the suite depend on whatever else runs on the machine.
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    source, _remote, _commit = _source_repo(tmp_path)
    _git(source, "remote", "add", "origin", remote_url)

    plan = launcher.build_plan(
        "dev",
        gui_home=source / "gui",
        environ=_env(tmp_path),
        cwd=tmp_path,
        inspect=lambda home, **k: _fake_project(home),
        which=_which,
    )

    assert expected_display in plan.dev_prompt
    payload = launcher.startup_payload(_dev_session(plan))
    human = launcher.render_startup(payload)
    assert "s3cr3t" not in plan.dev_prompt
    assert "s3cr3t" not in json.dumps(payload)
    assert "s3cr3t" not in human
    if remote_url != expected_display:
        assert "credentials redacted" in plan.dev_prompt


def test_startup_payload_redacts_source_and_auto_remote_credentials(
    tmp_path: Path,
) -> None:
    secret_remote = (
        "https://user:s3cr3t@example.invalid/hyprial.git?token=s3cr3t"
    )
    home = tmp_path / "gui"
    project = replace(
        _fake_project(home),
        source_remote=secret_remote,
        installed=True,
    )
    auto = auto_worktree.AutoGuiWorktree(
        source=tmp_path / "source",
        path=tmp_path / "auto",
        branch="(detached)",
        base="a" * 40,
        home=home,
        created=True,
        install=auto_worktree.DependencyInstall(
            command=(), cwd=home, ran=False, reason="test"
        ),
        managed=True,
        remote=secret_remote,
    )
    plan = launcher.GuiLaunchPlan(
        mode="dev",
        project=project,
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173",
        backend_endpoint=None,
        backend_owned=False,
        backend_argv=None,
        frontend_argv=("npm", "run", "dev"),
        env={},
        open_browser=False,
        auto_worktree=auto,
    )

    payload = launcher.startup_payload(_dev_session(plan))
    human = launcher.render_startup(payload)

    assert "s3cr3t" not in json.dumps(payload)
    assert "s3cr3t" not in human
    assert payload["source_remote"] == "https://example.invalid/hyprial.git"
    assert (
        payload["auto_worktree"]["remote"]
        == "https://example.invalid/hyprial.git"
    )


@pytest.mark.parametrize(
    "source_remote",
    [
        "https://user:s3cr3t@example.invalid/hyprial.git",
        "https://s3cr3t@example.invalid/hyprial.git",
        "git:s3cr3t@example.invalid:hyprial.git",
        "https://example.invalid/hyprial.git?token=s3cr3t",
        "https://example.invalid/hyprial.git#s3cr3t",
        "git@example.invalid:hyprial.git?token=s3cr3t",
    ],
)
def test_dev_source_rejects_credentials_without_echoing_them(
    source_remote: str,
) -> None:
    assets = resources.GuiAssets(
        root=Path("/nonexistent-gui"),
        manifest={"sourceCommit": "a" * 40, "sourceRemote": source_remote},
    )

    with pytest.raises(GuiError) as caught:
        dev_source.ensure_installed_dev_worktree(
            assets,
            environ={},
            which=lambda name: None,
            run=lambda *a, **k: pytest.fail("credentials reached Git"),
        )

    assert caught.value.code == "GUI_DEV_SOURCE_INVALID"
    for rendered in (str(caught.value), repr(caught.value), json.dumps(caught.value.data)):
        assert "s3cr3t" not in rendered
    assert "s3cr3t" not in caught.value.data["sourceRemote"]


def test_static_server_health_business_boundary_and_port_release(
    tmp_path: Path,
) -> None:
    root = _assets(tmp_path)
    server = static_server.create_server(root, "127.0.0.1", 0)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__gui/healthz", timeout=2
        ) as response:
            health = json.loads(response.read())
        assert health["ok"] is True
        assert health["assets"] == "installed"
        assert health["assetsVerified"] is True

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/gui/v1/messages",
            method="POST",
            data=b"{}",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        assert caught.value.code == 501
        assert caught.value.headers["X-Hyprial-Backend"] == "not-wired"
        body = json.loads(caught.value.read())
        assert body["code"] == "BACKEND_NOT_WIRED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def test_static_server_subprocess_sigint_exits_130_and_releases_port(
    tmp_path: Path,
) -> None:
    root = _assets(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hyprial.shell.impl.gui.install.server",
            "--root",
            str(root),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/__gui/healthz", timeout=1
                ) as response:
                    assert json.loads(response.read())["ok"] is True
                break
            except (OSError, urllib.error.URLError):
                if process.poll() is not None or time.monotonic() > deadline:
                    if process.poll() is None:
                        process.terminate()
                    _stdout, stderr = process.communicate(timeout=2)
                    pytest.fail(
                        f"static server did not become ready (exit "
                        f"{process.returncode}): {stderr[-1000:]}"
                    )
                time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 130
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def test_source_remote_allows_ssh_identity_but_rejects_secrets() -> None:
    assert dev_source._has_credentials(
        "ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git"
    ) is False
    assert dev_source._has_credentials(
        "git@git.internal.example.com:HyprialOS/harness-bridge.git"
    ) is False
    assert dev_source._has_credentials("/tmp/private/remote.git") is False
    assert dev_source._has_credentials("https://token@example.invalid/repo") is True
    assert dev_source._has_credentials(
        "ssh://git:secret@example.invalid/repo"
    ) is True
    assert dev_source._has_credentials("git:secret@example.invalid:repo") is True
    assert dev_source._has_credentials(
        "https://example.invalid/repo?token=secret"
    ) is True
    assert dev_source._has_credentials(
        "https://example.invalid/repo#secret"
    ) is True


def test_manifest_rejects_credentials_in_source_remote(tmp_path: Path) -> None:
    root = _assets(tmp_path)
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.build_manifest(
            root,
            package_version="1",
            source_commit="a" * 40,
            source_remote="https://token@example.invalid/hyprial",
            gui_version="1",
        )
    assert caught.value.code == "GUI_MANIFEST_INVALID"
    assert "token" not in str(caught.value)

    manifest = resources.read_manifest(root / resources.MANIFEST_NAME)
    manifest["sourceRemote"] = "https://token@example.invalid/hyprial"
    (root / resources.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.read_manifest(root / resources.MANIFEST_NAME)
    assert caught.value.code == "GUI_MANIFEST_INVALID"
    assert "token" not in str(caught.value)


@pytest.mark.parametrize(
    "source_remote",
    [
        "https://example.invalid/hyprial?token=s3cr3t",
        "https://example.invalid/hyprial#s3cr3t",
        "ssh://git@example.invalid/hyprial?token=s3cr3t",
    ],
)
def test_manifest_rejects_query_and_fragment_secrets(
    tmp_path: Path, source_remote: str
) -> None:
    root = _assets(tmp_path)
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.build_manifest(
            root,
            package_version="1",
            source_commit="a" * 40,
            source_remote=source_remote,
            gui_version="1",
        )
    assert caught.value.code == "GUI_MANIFEST_INVALID"
    assert "s3cr3t" not in str(caught.value)

    resources.validate_source_remote(
        "ssh://git@example.invalid/hyprial.git"
    )
    resources.validate_source_remote(
        "git@example.invalid:hyprial.git"
    )


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        "\\etc\\passwd",
        "C:\\temp\\evil.js",
        "C:evil.js",
        "\\\\server\\share\\evil.js",
        "//server/share/evil.js",
        "\\\\?\\C:\\evil.js",
        "../evil.js",
        "..\\evil.js",
        "assets/../../evil.js",
        "assets\\..\\evil.js",
        "assets/..\\evil.js",
        "a//../evil.js",
        ".. /evil.js",
        "...\\evil.js",
        "",
        ".",
        "..",
        "nul\x00evil.js",
    ],
)
def test_safe_relative_rejects_posix_and_windows_escapes(value: str) -> None:
    assert resources._safe_relative(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "index.html",
        "assets/app.js",
        "assets\\app.js",
        "./index.html",
        "a/b/c.txt",
        "a..b/c.txt",
        "dir/file-name_1.js",
    ],
)
def test_safe_relative_accepts_legitimate_relative_paths(value: str) -> None:
    assert resources._safe_relative(value) is True


def test_manifest_rejects_windows_style_escape_paths(tmp_path: Path) -> None:
    root = _assets(tmp_path)
    manifest = resources.read_manifest(root / resources.MANIFEST_NAME)
    entry = next(
        item for item in manifest["files"] if item["path"] == "app.js"
    )
    entry["path"] = "..\\evil.js"
    (root / resources.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(resources.GuiAssetError) as caught:
        resources.read_manifest(root / resources.MANIFEST_NAME)

    assert caught.value.code == "GUI_MANIFEST_INVALID"
    assert "unsafe file path" in str(caught.value)


def test_wheel_manifest_requires_full_sha_and_complete_files(tmp_path: Path) -> None:
    root = _assets(tmp_path)
    manifest = resources.read_manifest(root / resources.MANIFEST_NAME)
    assert len(manifest["sourceCommit"]) == 40
    assert manifest["files"]

    manifest["sourceCommit"] = "98b1042"
    (root / resources.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.read_manifest(root / resources.MANIFEST_NAME)
    assert caught.value.code == "GUI_MANIFEST_INVALID"

    manifest["sourceCommit"] = "a" * 40
    manifest["files"] = [
        entry for entry in manifest["files"] if entry["path"] != "index.html"
    ]
    (root / resources.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.read_manifest(root / resources.MANIFEST_NAME)
    assert caught.value.code == "GUI_MANIFEST_INVALID"

    root2 = _assets(tmp_path / "second")
    (root2 / "app.js").write_text("changed\n", encoding="utf-8")
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.load_assets(root2)
    assert caught.value.code == "GUI_ASSETS_INVALID"

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(resources.GuiAssetError) as caught:
        resources.build_manifest(
            empty,
            package_version="1",
            source_commit="a" * 40,
            source_remote="https://example.invalid/hyprial",
            gui_version="1",
        )
    assert caught.value.code == "GUI_ASSETS_MISSING"

    for field in ("package_version", "gui_version"):
        with pytest.raises(resources.GuiAssetError) as caught:
            resources.build_manifest(
                root,
                package_version="1" if field == "gui_version" else "",
                source_commit="a" * 40,
                source_remote="https://example.invalid/hyprial",
                gui_version="1" if field == "package_version" else "",
            )
        assert caught.value.code == "GUI_MANIFEST_INVALID"


@pytest.mark.parametrize("via_env", [False, True])
def test_explicit_build_without_manifest_is_unverified(tmp_path, monkeypatch, via_env):
    root = _assets(tmp_path)
    (root / resources.MANIFEST_NAME).unlink()
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    plan = launcher.build_plan(
        "", gui_home=None if via_env else root,
        environ={"HYPRIAL_GUI_HOME": str(root)} if via_env else {}, cwd=tmp_path,
        inspect=lambda *a, **k: pytest.fail("static build must not inspect npm"),
    )
    payload = launcher.startup_payload(_dev_session(plan))
    assert payload["installed"] is False
    assert payload["assets"] == "explicit-unverified"
    assert "verified built GUI" not in launcher.render_startup(payload)
    assert "unverified" in launcher.render_startup(payload)
    assert payload["source_commit"] == ""
    server = static_server.create_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/__gui/healthz", timeout=2
        ) as response:
            health = json.loads(response.read())
        assert health["assets"] == "explicit-unverified"
        assert health["assetsVerified"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_static_server_rejects_manifest_hash_mismatch(tmp_path):
    root = _assets(tmp_path)
    (root / "app.js").write_text("tampered", encoding="utf-8")
    with pytest.raises(static_server.GuiStaticServerError):
        static_server.create_server(root, "127.0.0.1", 0)


@pytest.mark.parametrize("via_env", [False, True])
def test_explicit_dev_prompt_does_not_claim_source_verified(tmp_path, monkeypatch, via_env):
    home = tmp_path / "gui"
    home.mkdir()
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    plan = launcher.build_plan(
        "dev", gui_home=None if via_env else home,
        environ={"HYPRIAL_DEV_GUI_HOME": str(home)} if via_env else {},
        cwd=tmp_path, inspect=lambda root, **k: _fake_project(root), which=_which,
    )
    assert plan.auto_worktree is None
    assert "Development source (this CLI does not auto-update it):" in plan.dev_prompt
    assert "verified" not in plan.dev_prompt.lower()
    payload = launcher.startup_payload(_dev_session(plan))
    assert "verified" not in payload["dev_prompt"].lower()
    assert "Development source (verified" not in launcher.render_startup(payload)
