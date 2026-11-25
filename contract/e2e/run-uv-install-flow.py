#!/usr/bin/env python3
"""Exercise production-form uv tool install/upgrade/uninstall in isolation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    free_tcp_endpoint,
    refuse_unisolatable_daemon,
    write_project_version,
)

ROOT = Path(__file__).resolve().parents[2]
GIT_SOURCE = "ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git"
OFFICIAL_GIT_SOURCE = "https://github.com/Hyprial/hyprial.git"
INITIAL_VERSION = "0.4.0.dev0"
ONE_SHOT_VERSION = "0.4.0rc1"
UPGRADED_VERSION = "0.4.0"


def run(
    *command: str | Path,
    env: dict[str, str],
    cwd: Path | None = None,
) -> str:
    rendered = [str(item) for item in command]
    result = subprocess.run(
        rendered,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        tail = result.stdout[-4000:]
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(rendered)}\n{tail}"
        )
    return result.stdout


def version(executable: Path, env: dict[str, str]) -> dict[str, object]:
    value = json.loads(run(executable, "version", "--json", env=env))
    if not isinstance(value, dict):
        raise TypeError("hyprial version --json must return an object")
    return value


def git_config(path: Path, remote: Path) -> None:
    path.write_text(
        "\n".join(
            (
                '[protocol "file"]',
                "\tallow = always",
                f'[url "{remote.as_uri()}"]',
                f"\tinsteadOf = {GIT_SOURCE}",
                f"\tinsteadOf = {OFFICIAL_GIT_SOURCE}",
                "[user]",
                "\tname = hyprial uv contract",
                "\temail = hyprial-uv-contract@example.invalid",
                "",
            )
        ),
        encoding="utf-8",
    )




def seed_remote(root: Path, env: dict[str, str]) -> tuple[Path, Path]:
    remote = root / "remote.git"
    seed = root / "seed"
    run("git", "init", "--bare", remote, env=env)
    run("git", "clone", "--no-local", ROOT, seed, env=env)
    run("git", "checkout", "-B", "dev", "HEAD", env=env, cwd=seed)
    if not (seed / "uv.lock").is_file():
        raise AssertionError("seed checkout does not contain uv.lock")
    for inherited_tag in run("git", "tag", "--list", env=env, cwd=seed).splitlines():
        run("git", "tag", "--delete", inherited_tag, env=env, cwd=seed)
    run("git", "config", "user.name", "hyprial uv contract", env=env, cwd=seed)
    run(
        "git", "config", "user.email", "hyprial-uv-contract@example.invalid",
        env=env, cwd=seed,
    )
    write_project_version(seed / "pyproject.toml", INITIAL_VERSION)
    # -u, not a file list: write_project_version edits pyproject.toml on old
    # layouts but the VERSION file on dynamic ones (d9382ea6); naming only
    # pyproject.toml left the rewritten VERSION uncommitted, so the seeded tag
    # kept the tree's real version.
    run("git", "add", "-u", env=env, cwd=seed)
    run(
        "git", "commit", "-m", "test: seed disposable old version", env=env, cwd=seed
    )
    run("git", "tag", f"v{INITIAL_VERSION}", env=env, cwd=seed)
    run("git", "remote", "set-url", "origin", remote, env=env, cwd=seed)
    run("git", "push", "origin", "dev", "--tags", env=env, cwd=seed)
    run("git", "symbolic-ref", "HEAD", "refs/heads/dev", env=env, cwd=remote)
    run("git", "cat-file", "-e", "HEAD:uv.lock", env=env, cwd=seed)
    return remote, seed


def push_one_shot(seed: Path, env: dict[str, str]) -> str:
    project = seed / "pyproject.toml"
    write_project_version(project, ONE_SHOT_VERSION, expected=INITIAL_VERSION)
    run("git", "add", "-u", env=env, cwd=seed)
    run("git", "commit", "-m", "test: publish one-shot early build", env=env, cwd=seed)
    commit = run("git", "rev-parse", "HEAD", env=env, cwd=seed).strip()
    run("git", "cat-file", "-e", "HEAD:uv.lock", env=env, cwd=seed)
    run("git", "push", "origin", "HEAD:refs/heads/early-test", env=env, cwd=seed)
    return commit


def push_upgrade(seed: Path, env: dict[str, str]) -> str:
    project = seed / "pyproject.toml"
    write_project_version(project, UPGRADED_VERSION, expected=ONE_SHOT_VERSION)
    run("git", "add", "-u", env=env, cwd=seed)
    run("git", "commit", "-m", "test: publish disposable upgrade", env=env, cwd=seed)
    commit = run("git", "rev-parse", "HEAD", env=env, cwd=seed).strip()
    run("git", "cat-file", "-e", "HEAD:uv.lock", env=env, cwd=seed)
    run("git", "tag", f"v{UPGRADED_VERSION}", env=env, cwd=seed)
    run("git", "push", "origin", f"v{UPGRADED_VERSION}", env=env, cwd=seed)
    return commit


def main() -> int:
    uv = shutil.which("uv")
    git = shutil.which("git")
    if uv is None or git is None:
        missing = "uv" if uv is None else "git"
        raise AssertionError(f"required executable is missing: {missing}")

    with tempfile.TemporaryDirectory(prefix="hyprial-uv-install-flow-") as temporary:
        root = Path(temporary)
        home = root / "home"
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        cache_dir = root / "cache"
        for directory in (home, tool_dir, bin_dir, cache_dir):
            directory.mkdir()
        # Only `hyprial init` creates a home in the product; the runner creates
        # the redirected HOME's hyprial home explicitly, like a fixture.
        (home / ".hyprial").mkdir(mode=0o700)

        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(home),
                "UV_TOOL_DIR": str(tool_dir),
                "UV_TOOL_BIN_DIR": str(bin_dir),
                "UV_CACHE_DIR": str(cache_dir),
                "UV_LINK_MODE": "copy",
                "UV_NO_PROGRESS": "1",
                "UV_PYTHON_DOWNLOADS": "never",
                "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PATH": f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}",
                "HYPRIAL_NETWORK_ISOLATED": "1",
                # An e2e test node (refuse_unisolatable_daemon).  Without
                # these, a daemon brought up by `upgrade` took the machine's
                # default node id and the inherited / `tailscale whoami`
                # owner: the production daemon's identity on the same Mac.
                "HYPRIAL_OWNER": "e2e",
                "HYPRIAL_NODE_ID": "e2e-uv-install-flow",
                "HYPRIAL_ZENOH_LISTEN": free_tcp_endpoint(),
            }
        )
        # The redirected HOME must be the only hyprial home: an ambient HYPRIAL_HOME
        # (e.g. an operator's E2E preflight exports) would silently make the
        # CLI read settings.json outside the disposable root — including the
        # updateTrack selector this scenario now exercises.
        for leak in (
            "HYPRIAL_HOME",
            "HARNESS_STATE_DIR",
            "HARNESS_E2E_HOME",
            "HARNESS_SOCKET_PATH",
            # The runner itself is launched with PYTHONPATH pointing at the
            # candidate checkout. Installed CLI subprocesses must import from
            # the disposable uv tool, not bypass it through that outer path.
            "PYTHONPATH",
            # The installed CLI must locate the wheel-bundled sidecar, not an
            # ambient override pointing elsewhere.
            "HYPRIAL_TAILCAT_BINARY",
        ):
            environment.pop(leak, None)
        # The old build predates HYPRIAL_NETWORK_ISOLATED: judged on the exact
        # dict its commands (and any daemon they bring up) run with.
        refused = refuse_unisolatable_daemon("uv-install-flow", environment)
        if refused is not None:
            return refused

        # The wheel bundles hyprial-tailcat (build hook, G12). The scenario
        # hands the hook a prebuilt sidecar instead of requiring Go here.
        prebuilt = os.environ.get("HYPRIAL_TAILCAT_BINARY", "")
        if not prebuilt or not os.access(prebuilt, os.X_OK):
            print(
                "RESULT BLOCKED E2E-005 uv-install-flow needs HYPRIAL_TAILCAT_BINARY "
                "(an executable hyprial-tailcat) for the wheel build hook"
            )
            return 2
        environment["HYPRIAL_TAILCAT_PREBUILT"] = prebuilt
        remote, seed = seed_remote(root, environment)
        git_config(Path(environment["GIT_CONFIG_GLOBAL"]), remote)
        executable = bin_dir / "hyprial"
        installed = False
        try:
            run(
                uv,
                "tool",
                "install",
                f"git+{GIT_SOURCE}@v{INITIAL_VERSION}",
                "--python",
                sys.executable,
                env=environment,
            )
            installed = True
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise AssertionError("uv install did not create an executable hyprial")
            bundled = subprocess.run(
                [
                    str(tool_dir / "hyprial" / "bin" / "python"),
                    "-c",
                    "import json, pathlib, sys; "
                    "from hyprial.daemon import locate_tailcat_sidecar, verify_tailcat_sidecar; "
                    "home = pathlib.Path(sys.argv[1]); "
                    "path = locate_tailcat_sidecar(home); "
                    "print(json.dumps({'path': str(path), "
                    "'version': verify_tailcat_sidecar(home)}))",
                    str(home / ".hyprial"),
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if bundled.returncode != 0:
                raise AssertionError(
                    "the installed wheel has no usable hyprial-tailcat: "
                    + (bundled.stderr or bundled.stdout)[-2000:]
                )
            located = json.loads(bundled.stdout)
            # Compare resolved paths: macOS temp dirs are reached through the
            # /var -> /private/var symlink, so a textual prefix check lies.
            sidecar_path = Path(located["path"]).resolve()
            if (
                "_bin" not in sidecar_path.parts
                or not sidecar_path.is_relative_to(tool_dir.resolve())
                or not os.access(sidecar_path, os.X_OK)
            ):
                raise AssertionError(
                    f"hyprial-tailcat was not located inside the installed wheel: {located}"
                )
            if (home / ".hyprial" / "secrets" / "login.json").exists():
                raise AssertionError("locating the sidecar wrote login credentials")
            initial = version(executable, environment)
            if initial.get("packageVersion") != INITIAL_VERSION:
                raise AssertionError(f"unexpected initial version: {initial}")
            if initial.get("localVersion") != "0.4.0-dev.0":
                raise AssertionError(f"unexpected initial semantic version: {initial}")

            one_shot_commit = push_one_shot(seed, environment)
            one_shot = json.loads(
                run(
                    executable,
                    "daemon",
                    "upgrade",
                    "--source",
                    "forgejo",
                    "--ref",
                    one_shot_commit,
                    "--json",
                    env=environment,
                )
            )
            if (
                one_shot.get("upgraded") is not True
                or one_shot.get("source") != "forgejo"
                or one_shot.get("oneShotSource") is not True
                or one_shot.get("resolvedCommit") != one_shot_commit
                or one_shot.get("persistentUpdateSource") != OFFICIAL_GIT_SOURCE
            ):
                raise AssertionError(
                    f"one-shot Forgejo commit did not stay one-shot: {one_shot}"
                )
            after_one_shot = version(executable, environment)
            if after_one_shot.get("packageVersion") != ONE_SHOT_VERSION:
                raise AssertionError(
                    f"one-shot install did not observe its commit: {after_one_shot}"
                )

            upgraded_commit = push_upgrade(seed, environment)
            upgrade = json.loads(
                run(executable, "daemon", "upgrade", "--json", env=environment)
            )
            if not isinstance(upgrade, dict) or upgrade.get("upgraded") is not True:
                raise AssertionError(
                    f"hyprial daemon upgrade did not succeed: {upgrade}"
                )
            if upgrade.get("source") != "official":
                raise AssertionError(
                    f"plain upgrade did not return to the official source: {upgrade}"
                )
            if upgrade.get("resolvedTag") != f"v{UPGRADED_VERSION}":
                raise AssertionError(f"upgrade did not report resolved tag: {upgrade}")
            if upgrade.get("resolvedCommit") != upgraded_commit:
                raise AssertionError(f"upgrade did not report resolved commit: {upgrade}")
            upgraded = version(executable, environment)
            if upgraded.get("packageVersion") != UPGRADED_VERSION:
                raise AssertionError(
                    f"upgrade did not observe tagged commit: {upgraded}"
                )
            if upgraded.get("localVersion") != "0.4.0":
                raise AssertionError(
                    f"unexpected upgraded semantic version: {upgraded}"
                )
            if upgraded.get("persistentUpdateSource") != OFFICIAL_GIT_SOURCE:
                raise AssertionError(
                    f"version did not report the official persistent source: {upgraded}"
                )
            dependency_lock = upgraded.get("dependencyLock")
            if (
                not isinstance(dependency_lock, dict)
                or dependency_lock.get("status") != "match"
                or dependency_lock.get("mismatches") != []
            ):
                raise AssertionError(
                    f"installed dependencies do not match the target lock: {upgraded}"
                )

            # --- track resolution touchpoints (three-track channels) ----- #
            # An explicit settings.json updateTrack resolves the movable tag
            # of the same name; a track install pins the exact commit as the
            # PEP 610 requested_revision; a missing track tag fails loud and
            # NEVER falls back to the highest version tag.
            run("git", "tag", "-f", "stable", upgraded_commit, env=environment, cwd=seed)
            run("git", "push", "--force", "origin", "refs/tags/stable",
                env=environment, cwd=seed)
            hyprial_home = home / ".hyprial"
            hyprial_home.mkdir(parents=True, exist_ok=True)
            settings = hyprial_home / "settings.json"
            settings.write_text(
                '{"version":1,"updateTrack":"stable"}\n', encoding="utf-8"
            )
            track_noop = json.loads(
                run(executable, "daemon", "upgrade", "--json", env=environment)
            )
            if (
                track_noop.get("upgraded") is not False
                or track_noop.get("track") != "stable"
                or track_noop.get("resolvedTag") != "stable"
                or track_noop.get("resolvedCommit") != upgraded_commit
            ):
                raise AssertionError(f"stable track did not resolve as a no-op: {track_noop}")
            forced = json.loads(
                run(
                    executable,
                    "daemon",
                    "upgrade",
                    "--force",
                    "--json",
                    env=environment,
                )
            )
            if forced.get("upgraded") is not True:
                raise AssertionError(f"forced track upgrade did not reinstall: {forced}")
            direct_url = json.loads(
                next(
                    tool_dir.glob(
                        "hyprial/lib/python*/site-packages/hyprial-*.dist-info/direct_url.json"
                    )
                ).read_text(encoding="utf-8")
            )
            requested = (direct_url.get("vcs_info") or {}).get("requested_revision")
            if requested != upgraded_commit:
                raise AssertionError(
                    f"track install did not pin the commit in PEP 610: {direct_url}"
                )
            settings.write_text(
                '{"version":1,"updateTrack":"nightly"}\n', encoding="utf-8"
            )
            raw = subprocess.run(
                [str(executable), "daemon", "upgrade", "--json"],
                env=environment, text=True, capture_output=True, check=False,
            )
            if raw.returncode == 0:
                raise AssertionError(f"missing nightly track tag did not fail: {raw.stdout}")
            antifallback = json.loads(raw.stdout.strip().splitlines()[-1])
            if (
                antifallback.get("code") != "UPGRADE_CHECK_FAILED"
                or "'nightly'" not in str(antifallback.get("error"))
                or "v0.4.0" in raw.stdout
            ):
                raise AssertionError(
                    f"missing track tag fell back or failed wrong: {raw.stdout}"
                )
            settings.unlink()

            run(uv, "tool", "uninstall", "hyprial", env=environment)
            installed = False
            if executable.exists() or (tool_dir / "hyprial").exists():
                raise AssertionError("uv uninstall left hyprial files behind")
        finally:
            if installed:
                run(uv, "tool", "uninstall", "hyprial", env=environment)

    print(
        "RESULT PASS uv-install-flow "
        f"initial={INITIAL_VERSION} upgraded={UPGRADED_VERSION} uninstalled=true "
        "sidecar=installed-without-identity "
        "one-shot=forgejo-commit+official-next "
        "dependency-lock=match track-resolution=noop+commit-pin antifallback=loud"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, TypeError, json.JSONDecodeError, OSError) as error:
        print(f"RESULT FAIL uv-install-flow {error}")
        raise SystemExit(1) from error
