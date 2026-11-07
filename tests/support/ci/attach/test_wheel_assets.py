"""Guards for scripts/ci/attach_wheel_assets.py.

These are deliberately narrow. They do NOT prove that a wheel reaches a public
release -- that needs a real dispatch against real credentials, and claiming it
here would be the mistake this repository keeps making. What they do prove is
that the script says the honest thing when it cannot evaluate:

  * no credentials            -> rc 2 (NOT EVALUABLE), never rc 1 (FAIL)
  * rc values are instructions -> 0 / 1 / 2 / 4, and only 0 means "attached"

Why rc=2 matters more than rc=1 here: an attach step that reports FAIL when its
credentials are absent will be read as "the upload broke", and the release will
still have `assets: 0`. The two states have different next actions -- one is a
secrets/ops problem, the other is a code or registry problem -- so they must not
share an exit code.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "attach_wheel_assets.py"


def _module():
    spec = importlib.util.spec_from_file_location("attach_wheel_assets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_exists_and_compiles() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    compile(source, str(SCRIPT), "exec")


def test_missing_github_token_is_not_evaluable_not_failure() -> None:
    done = _run(
        "--tag", "stable",
        "--version", "0.5.4+gabc1234",
        "--registry-url", "https://example.invalid/api/packages/X/pypi",
        "--user", "u",
        "--token", "t",
        "--github-repo", "Hyprial/hyprial",
    )
    assert done.returncode == 2, done.stderr
    assert "NOT EVALUABLE" in done.stderr
    assert "gh-token" in done.stderr


def test_missing_registry_credentials_is_not_evaluable() -> None:
    done = _run(
        "--tag", "stable",
        "--version", "0.5.4+gabc1234",
        "--registry-url", "https://example.invalid/api/packages/X/pypi",
        "--github-repo", "Hyprial/hyprial",
        "--gh-token", "x",
    )
    assert done.returncode == 2, done.stderr
    assert "NOT EVALUABLE" in done.stderr


def test_help_lists_the_pin_inputs() -> None:
    done = _run("--help")
    assert done.returncode == 0
    for flag in ("--tag", "--version", "--registry-url", "--github-repo", "--gh-token"):
        assert flag in done.stdout


def test_unreachable_registry_is_not_evaluable(monkeypatch, capsys) -> None:
    """A registry that cannot be listed is 'I could not look', not 'it is gone'."""
    module = _module()
    monkeypatch.setattr(
        module,
        "_http",
        lambda *_args, **_kwargs: (0, b"no answer: unreachable", {}),
    )
    with pytest.raises(SystemExit, match="2"):
        module._wait_for_registry(
            "https://example.invalid/api/packages/X/pypi",
            ("u", "t"),
            "0.5.4+gabc1234",
            0,
        )
    assert "NOT EVALUABLE" in capsys.readouterr().err


def test_wait_defaults_to_zero_so_a_human_run_is_fast() -> None:
    """The bounded wait is a workflow decision, not a script default.

    `wheel-release` and `github-publish` fire on the same tag push, so the attach
    step can reach the registry before the wheels exist. The workflow therefore
    passes `--wait-for-registry 900`. The default must stay 0: a person running
    the script by hand should get an answer immediately, not sit for 15 minutes.
    """
    done = _run("--help")
    assert done.returncode == 0
    assert "--wait-for-registry" in done.stdout
    # argparse re-wraps the help text, so match a phrase rather than a layout.
    assert "do not wait" in " ".join(done.stdout.split())


def test_credentials_are_read_from_the_environment() -> None:
    """Secrets must be able to arrive without touching argv.

    This repository's wheel-release.yml states the contract: credentials live in
    the process environment only, never in argv, never on disk. An argv token is
    visible in `ps` to every process on the runner, so the workflow must be able
    to pass both credentials purely through the environment -- which it can only
    do if the script reads them from there.
    """
    import os

    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env["WHEEL_REGISTRY_USER"] = "u"
    env["WHEEL_REGISTRY_TOKEN"] = "t"
    done = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--tag", "stable",
         "--version", "0.5.4+gabc1234",
         "--registry-url", "https://example.invalid/api/packages/X/pypi",
         "--github-repo", "Hyprial/hyprial"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    # No token anywhere -> the honest answer is "not evaluable", and it must name
    # the missing token. Registry credentials arrived, so only the token is named.
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert "gh-token" in done.stderr
    assert "WHEEL_REGISTRY_USER" not in done.stderr


def test_internal_plan_selects_exactly_five_wheels_and_sums() -> None:
    module = _module()
    version = "0.5.5+g1234567"
    names = [f"hyprial-{version}-py3-none-{tag}.whl" for tag in module.PLATFORM_TAGS]
    assert module.asset_plan(names, version) == (*sorted(names), "SHA256SUMS")


def test_incomplete_candidate_never_reaches_release_mutation(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    version = "0.5.5+g1234567"
    names = [f"hyprial-{version}-py3-none-{tag}.whl" for tag in module.PLATFORM_TAGS[:-1]]
    release_reads: list[bool] = []
    mutated: list[bool] = []
    monkeypatch.setattr(module, "_wait_for_registry", lambda *args: names)
    monkeypatch.setattr(
        module,
        "_release",
        lambda *args, **kwargs: (
            release_reads.append(True)
            or {"id": 1, "upload_url": "upload", "assets": []}
        ),
    )
    monkeypatch.setattr(module, "_upload", lambda *args, **kwargs: mutated.append(True))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT), "--tag", "internal", "--version", version,
            "--source-sha", "1234567" + "0" * 33,
            "--registry-url", "https://registry.invalid", "--user", "u", "--token", "t",
            "--github-repo", "Hyprial/hyprial", "--gh-token", "g",
            "--workdir", str(tmp_path),
        ],
    )
    assert module.main() == 1
    assert release_reads == [True]  # self-repair check is deliberately first
    assert mutated == []


def test_registry_credentials_come_from_the_environment_too() -> None:
    import os

    env = dict(os.environ)
    env.pop("WHEEL_REGISTRY_USER", None)
    env.pop("WHEEL_REGISTRY_TOKEN", None)
    env["GH_TOKEN"] = "present"  # so only the registry inputs are reported missing
    done = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--tag", "stable",
         "--version", "0.5.4+gabc1234",
         "--registry-url", "https://example.invalid/api/packages/X/pypi",
         "--github-repo", "Hyprial/hyprial"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert "WHEEL_REGISTRY_USER" in done.stderr


def _complete_assets(module, version: str, *, canonical: bool = True):
    import hashlib

    assets = []
    lines = []
    for index, platform in enumerate(module.PLATFORM_TAGS, 1):
        name = f"hyprial-{version}-py3-none-{platform}.whl"
        body = f"wheel-{index}".encode()
        assets.append({"id": index, "name": name, "body": body})
        lines.append(f"{hashlib.sha256(body).hexdigest()}  {name}")
    manifest = ("\n".join(sorted(lines)) + "\n").encode()
    assets.append(
        {
            "id": 10,
            "name": "SHA256SUMS" if canonical else "SHA256SUMS.previous-10",
            "body": manifest,
        }
    )
    return assets, manifest


def test_second_pointer_rename_exhausts_retries_then_rolls_back(capsys) -> None:
    module = _module()
    assets, manifest = _complete_assets(module, "0.5.4+gold0000")
    old = assets[-1]
    staged = {"id": 20, "name": "SHA256SUMS.0.5.5+gnew0000", "body": manifest}
    attempts: list[tuple[int, str]] = []

    def rename(_repo, asset_id, name, _token, *, dry_run):
        attempts.append((asset_id, name))
        if asset_id == staged["id"]:
            return False
        next(asset for asset in [old, staged] if asset["id"] == asset_id)["name"] = name
        return True

    rc = module._swap_manifest_pointer(
        "Hyprial/hyprial", old, staged, "token", dry_run=False,
        repair_command="repair-now", rename=rename, sleep=lambda _delay: None,
    )
    assert rc == 1
    assert old["name"] == "SHA256SUMS"
    assert attempts.count((20, "SHA256SUMS")) == module.NETWORK_RETRY_ATTEMPTS
    assert attempts[-1] == (10, "SHA256SUMS")
    assert "previous complete manifest was restored" in capsys.readouterr().err


def test_pointer_rename_and_rollback_failure_is_loud_and_preserves_assets(capsys) -> None:
    module = _module()
    assets, manifest = _complete_assets(module, "0.5.4+gold0000")
    old = assets[-1]
    staged = {"id": 20, "name": "SHA256SUMS.0.5.5+gnew0000", "body": manifest}
    original_ids = {asset["id"] for asset in [*assets, staged]}
    attempts: list[tuple[int, str]] = []

    def rename(_repo, asset_id, name, _token, *, dry_run):
        attempts.append((asset_id, name))
        if name == "SHA256SUMS.previous-10":
            old["name"] = name
            return True
        return False

    rc = module._swap_manifest_pointer(
        "Hyprial/hyprial", old, staged, "token", dry_run=False,
        repair_command="repair-now", rename=rename, sleep=lambda _delay: None,
    )
    assert rc == 4
    assert {asset["id"] for asset in [*assets, staged]} == original_ids
    assert attempts.count((20, "SHA256SUMS")) == module.NETWORK_RETRY_ATTEMPTS
    assert attempts.count((10, "SHA256SUMS")) == module.NETWORK_RETRY_ATTEMPTS
    error = capsys.readouterr().err
    assert "SHA256SUMS.previous-10" in error
    assert "repair-now" in error
    assert "CANONICAL SHA256SUMS IS MISSING" in error


def test_first_pointer_rename_lost_responses_rolls_back_applied_change(capsys) -> None:
    module = _module()
    assets, manifest = _complete_assets(module, "0.5.4+gold0000")
    old = assets[-1]
    staged = {"id": 20, "name": "SHA256SUMS.0.5.5+gnew0000", "body": manifest}
    backup_name = "SHA256SUMS.previous-10"
    remote_old = dict(old)
    attempts: list[tuple[int, str]] = []

    def rename(_repo, asset_id, name, _token, *, dry_run):
        attempts.append((asset_id, name))
        if name == backup_name:
            # GitHub applied every PATCH, but every response was lost.
            remote_old["name"] = name
            return False
        remote_old["name"] = name
        return True

    def release(_repo, _tag, _token, *, dry_run):
        return {"assets": [remote_old, staged]}

    rc = module._swap_manifest_pointer(
        "Hyprial/hyprial", old, staged, "token", tag="internal", dry_run=False,
        repair_command="repair-now", rename=rename, release=release,
        sleep=lambda _delay: None,
    )
    assert rc == 1
    assert remote_old["name"] == "SHA256SUMS"
    assert attempts.count((10, backup_name)) == module.NETWORK_RETRY_ATTEMPTS
    assert attempts[-1] == (10, "SHA256SUMS")
    assert not any(asset_id == 20 for asset_id, _name in attempts)
    assert "previous complete manifest was restored" in capsys.readouterr().err


def test_first_pointer_rename_lost_responses_and_rollback_failure_is_loud(capsys) -> None:
    module = _module()
    assets, manifest = _complete_assets(module, "0.5.4+gold0000")
    old = assets[-1]
    staged = {"id": 20, "name": "SHA256SUMS.0.5.5+gnew0000", "body": manifest}
    backup_name = "SHA256SUMS.previous-10"
    remote_old = dict(old)

    def rename(_repo, _asset_id, name, _token, *, dry_run):
        if name == backup_name:
            remote_old["name"] = name
        return False

    rc = module._swap_manifest_pointer(
        "Hyprial/hyprial", old, staged, "token", tag="internal", dry_run=False,
        repair_command="repair-now", rename=rename,
        release=lambda *_args, **_kwargs: {"assets": [remote_old, staged]},
        sleep=lambda _delay: None,
    )
    assert rc == 4
    error = capsys.readouterr().err
    assert "CANONICAL SHA256SUMS IS MISSING" in error
    assert backup_name in error
    assert "repair-now" in error


def test_main_repairs_missing_manifest_before_unavailable_registry(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    version = "0.5.5+g1234567"
    assets, _manifest = _complete_assets(module, version, canonical=False)
    calls: list[str] = []
    for asset in assets:
        asset["url"] = f"https://assets.invalid/{asset['id']}"

    def release(*_args, **_kwargs):
        calls.append("release")
        return {"id": 1, "upload_url": "upload", "assets": assets}

    def http(url, *, method="GET", **_kwargs):
        if method == "PATCH":
            calls.append("repair")
            return 200, b"{}", {}
        asset = next(item for item in assets if item["url"] == url)
        return 200, asset["body"], {}

    def unavailable(*_args, **_kwargs):
        calls.append("registry")
        assert any(asset["name"] == "SHA256SUMS" for asset in assets)
        raise SystemExit(2)

    monkeypatch.setattr(module, "_release", release)
    monkeypatch.setattr(module, "_http", http)
    monkeypatch.setattr(module, "_wait_for_registry", unavailable)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT), "--tag", "internal", "--version", version,
            "--source-sha", "1234567" + "0" * 33,
            "--registry-url", "https://registry.invalid", "--user", "u", "--token", "t",
            "--github-repo", "Hyprial/hyprial", "--gh-token", "g",
            "--workdir", str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit, match="2"):
        module.main()
    assert calls == ["release", "repair", "registry"]
    assert assets[-1]["name"] == "SHA256SUMS"


def test_registry_waits_for_exact_five_wheel_set() -> None:
    module = _module()
    version = "0.5.5+g1234567"
    complete = [
        f"hyprial-{version}-py3-none-{platform}.whl"
        for platform in module.PLATFORM_TAGS
    ]
    snapshots = iter((complete[:1], complete))
    sleeps: list[float] = []

    module._registry_files = lambda *_args: next(snapshots)
    names = module._wait_for_registry(
        "registry", ("u", "t"), version, 60,
        monotonic=iter((0.0, 1.0)).__next__, sleep=sleeps.append,
    )
    assert names == complete
    assert sleeps == [30]


def test_registry_incomplete_at_bound_names_every_missing_platform(capsys) -> None:
    module = _module()
    version = "0.5.5+g1234567"
    one = f"hyprial-{version}-py3-none-{module.PLATFORM_TAGS[0]}.whl"
    module._registry_files = lambda *_args: [one]

    with pytest.raises(module.WheelSetError) as raised:
        module._wait_for_registry(
            "registry", ("u", "t"), version, 0,
            monotonic=lambda: 0.0, sleep=lambda _delay: None,
        )
    message = str(raised.value)
    for platform in module.PLATFORM_TAGS[1:]:
        assert platform in message
    assert "after 1 attempt(s) over 0s" in capsys.readouterr().err


def test_next_run_repairs_missing_canonical_from_newest_complete_manifest() -> None:
    module = _module()
    old_assets, _old_manifest = _complete_assets(
        module, "0.5.4+gold0000", canonical=False
    )
    new_assets, new_manifest = _complete_assets(
        module, "0.5.5+gnew0000", canonical=False
    )
    # Keep both complete wheel sets, but give the staged candidate the newest id.
    staged = {
        "id": 30,
        "name": "SHA256SUMS.0.5.5+gnew0000",
        "body": new_manifest,
    }
    assets = old_assets + new_assets[:-1] + [staged]
    renamed: list[tuple[int, str]] = []

    def body(asset, _token):
        return asset["body"]

    def rename(_repo, asset_id, name, _token, *, dry_run):
        renamed.append((asset_id, name))
        next(asset for asset in assets if asset["id"] == asset_id)["name"] = name
        return True

    assert module._repair_missing_manifest(
        assets, "Hyprial/hyprial", "token", dry_run=False,
        repair_command="repair-now", asset_bytes=body, rename=rename,
        sleep=lambda _delay: None,
    )
    assert renamed == [(30, "SHA256SUMS")]
    canonical = next(asset for asset in assets if asset["name"] == "SHA256SUMS")
    assert canonical["body"] == new_manifest


def _forgejo_pypi(registry: str, version: str, names: list[str]):
    """Fake _http answering only the routes a Forgejo PyPI registry serves.

    Forgejo has no JSON API and no `<registry>/<dist>/` listing: the index is
    `<registry>/simple/<dist>/` and files live under
    `<registry>/files/<dist>/<version>/<filename>`. Everything else is 404, which
    is what the 0.5.5 internal publish (run 26682) met for 40 minutes.
    """
    import urllib.parse

    quoted = urllib.parse.quote(version, safe="")
    # Forgejo renders the index with Go html/template, which escapes "+" as "&#43;"
    # in both the href and the link text (run 26697 saw 0.5.5+g22141fa that way).
    def go_html(text: str) -> str:
        return text.replace("+", "&#43;")

    index = "".join(
        f'<a href="{go_html(f"{registry}/files/hyprial/{version}/{n}")}#sha256=00">{go_html(n)}</a>'
        for n in names
    )
    files = {f"{registry}/files/hyprial/{quoted}/{urllib.parse.quote(n)}": n.encode() for n in names}

    def fake(url, *, auth, method="GET", data=None, headers=None):
        if url == f"{registry}/simple/hyprial/":
            return 200, f"<html><body>{index}</body></html>".encode(), {}
        if url in files:
            return 200, files[url], {}
        return 404, b"Not Found", {}

    return fake


def test_lists_and_downloads_through_the_forgejo_pypi_routes(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    registry = "http://tailscale:3000/api/packages/HyprialOS/pypi"
    version = "0.5.5+g8a0a54a"
    names = [f"hyprial-{version}-py3-none-{tag}.whl" for tag in (
        "macosx_13_0_arm64", "macosx_13_0_x86_64", "win_amd64",
        "manylinux_2_17_x86_64.manylinux2014_x86_64",
        "manylinux_2_17_aarch64.manylinux2014_aarch64",
    )]
    stale = "hyprial-0.5.4+g14e3281-py3-none-win_amd64.whl"
    monkeypatch.setattr(module, "_http", _forgejo_pypi(registry, version, [*names, stale]))

    assert module._registry_files(registry, ("u", "t"), version) == sorted(names)
    for name in names:
        path, digest = module._download(registry, ("u", "t"), name, str(tmp_path))
        assert Path(path).read_bytes() == name.encode()
        assert digest == module.hashlib.sha256(name.encode()).hexdigest()
