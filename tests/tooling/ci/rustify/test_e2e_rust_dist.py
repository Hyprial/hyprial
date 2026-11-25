"""Verified dist archives are the only inputs admitted to shared Rust caches."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/ci-rust-dist.py"
HOST = "x86_64-unknown-linux-gnu"
VERSION = "1.93.1"
DATE = "2026-02-12"
COMPONENTS = ("cargo", "rust-std", "rustc")


@pytest.fixture
def dist():
    spec = importlib.util.spec_from_file_location("e2e_rust_dist", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(host: str = HOST, *, url_host: str | None = None, version: str = VERSION) -> bytes:
    url_host = url_host or host
    parts = [
        'manifest-version = "2"', f'date = "{DATE}"',
        '[profiles]', 'minimal = ["rustc", "cargo", "rust-std"]',
        '[pkg.rust]', f'version = "{version} (fixture)"',
    ]
    for name in COMPONENTS:
        package_version = "0.94.0" if name == "cargo" else version
        parts += [
            f'[pkg.{name}]', f'version = "{package_version} (fixture)"',
            f'[pkg.{name}.target.{host}]', 'available = true',
            f'xz_url = "https://static.rust-lang.org/dist/{DATE}/{name}-{version}-{url_host}.tar.xz"',
            f'xz_hash = "{hashlib.sha256(name.encode()).hexdigest()}"',
        ]
    return ("\n".join(parts) + "\n").encode()


@pytest.fixture
def provision(dist, tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "run"
    cache = tmp_path / "cache"
    downloads = root / "rustup/downloads"
    calls = []
    data = manifest()

    def fetch(url, destination, component):
        calls.append((url, component))
        if component == "manifest":
            content = data
        elif component == "manifest-sha":
            content = (hashlib.sha256(data).hexdigest() + "  channel-rust-1.93.1.toml\n").encode()
        else:
            content = component.encode()
        destination.write_bytes(content)

    monkeypatch.setattr(dist, "download", fetch)
    return SimpleNamespace(module=dist, root=root, cache=cache, downloads=downloads, calls=calls, fetch=fetch)


@pytest.mark.parametrize("host", [HOST, "aarch64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-apple-darwin"])
def test_official_exact_version_selects_only_same_host_minimal_xz(dist, host: str) -> None:
    selected = dist.select_components(manifest(host), host, VERSION)
    assert [row["component"] for row in selected] == list(COMPONENTS)
    for row in selected:
        assert row["url"].endswith(f"-{VERSION}-{host}.tar.xz")
        assert len(row["sha256"]) == 64


@pytest.mark.parametrize("mutation", [
    "wrong-version", "wrong-host", "unavailable", "nonofficial-url", "http-url",
    "url-credentials", "query", "traversal", "wrong-extension", "bad-hash", "missing-component", "profile",
])
def test_invalid_manifest_cannot_choose_an_archive(dist, mutation: str) -> None:
    text = manifest().decode()
    if mutation == "wrong-version":
        text = manifest(version="1.93.0").decode()
    elif mutation == "wrong-host":
        text = manifest(url_host="aarch64-apple-darwin").decode()
    elif mutation == "unavailable":
        text = text.replace("available = true", "available = false", 1)
    elif mutation == "nonofficial-url":
        text = text.replace("https://static.rust-lang.org", "https://example.invalid", 1)
    elif mutation == "http-url":
        text = text.replace("https://", "http://", 1)
    elif mutation == "url-credentials":
        text = text.replace("https://static.rust-lang.org", "https://user:password@static.rust-lang.org", 1)
    elif mutation == "query":
        text = text.replace('.tar.xz"', '.tar.xz?mirror=other"', 1)
    elif mutation == "traversal":
        text = text.replace(f"/dist/{DATE}/", f"/dist/{DATE}/../", 1)
    elif mutation == "wrong-extension":
        text = text.replace(".tar.xz", ".tar.gz", 1)
    elif mutation == "bad-hash":
        text = text.replace(hashlib.sha256(b"cargo").hexdigest(), "not-a-sha256")
    elif mutation == "missing-component":
        text = text.split("[pkg.rustc]")[0]
    else:
        text = text.replace('["rustc", "cargo", "rust-std"]', '["rustc", "cargo", "rust-std", "rust-docs"]')
    with pytest.raises(dist.ProvisioningError):
        dist.select_components(text.encode(), HOST, VERSION)


def test_minimal_profile_ignores_only_packages_unavailable_for_this_host(dist) -> None:
    text = manifest().decode().replace(
        '["rustc", "cargo", "rust-std"]', '["rustc", "cargo", "rust-std", "rust-mingw"]',
    )
    text += '[pkg.rust-mingw.target.x86_64-pc-windows-gnu]\navailable = true\n'
    assert len(dist.select_components(text.encode(), HOST, VERSION)) == 3


def test_verified_archives_are_reused_across_separate_private_homes(provision) -> None:
    p = provision
    p.module.prepare_dist(HOST, VERSION, p.root, p.cache, p.downloads)
    assert [component for _, component in p.calls] == ["manifest", "manifest-sha", *COMPONENTS]
    for component in COMPONENTS:
        digest = hashlib.sha256(component.encode()).hexdigest()
        assert (p.downloads / digest).read_bytes() == component.encode()
        assert (p.cache / "dist" / VERSION / HOST / digest).read_bytes() == component.encode()
        assert not (p.downloads / digest).is_symlink()
    p.calls.clear()
    second = p.root.parent / "second-run"
    p.module.prepare_dist(HOST, VERSION, second, p.cache, second / "rustup/downloads")
    assert [component for _, component in p.calls] == ["manifest", "manifest-sha"]
    assert not list(p.cache.rglob("*.partial"))
    assert not list(p.cache.rglob(".publish-*"))


@pytest.mark.parametrize("case", ["corrupt", "symlink", "directory", "fifo"])
def test_bad_or_symlinked_shared_cache_is_rejected_without_replacement(provision, case: str) -> None:
    p = provision
    digest = hashlib.sha256(b"cargo").hexdigest()
    path = p.cache / "dist" / VERSION / HOST / digest
    path.parent.mkdir(parents=True)
    if case == "corrupt":
        path.write_bytes(b"bad archive")
    elif case == "symlink":
        target = p.root.parent / "external"
        target.write_bytes(b"cargo")
        path.symlink_to(target)
    elif case == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(p.module.ProvisioningError):
        p.module.prepare_dist(HOST, VERSION, p.root, p.cache, p.downloads)
    assert [component for _, component in p.calls] == ["manifest", "manifest-sha"]
    if case == "corrupt":
        assert path.read_bytes() == b"bad archive"


def test_corrupt_download_never_becomes_a_reusable_cache_entry(provision, monkeypatch) -> None:
    p = provision

    def corrupt(url, destination, component):
        p.fetch(url, destination, component)
        if component == "cargo":
            destination.write_bytes(b"partial")

    monkeypatch.setattr(p.module, "download", corrupt)
    with pytest.raises(p.module.ProvisioningError):
        p.module.prepare_dist(HOST, VERSION, p.root, p.cache, p.downloads)
    assert not list(p.cache.rglob("*" + hashlib.sha256(b"cargo").hexdigest()))
    assert not list(p.cache.rglob(".publish-*"))


def test_manifest_bytes_must_match_the_official_checksum_response(provision, monkeypatch) -> None:
    p = provision

    def corrupt(url, destination, component):
        p.fetch(url, destination, component)
        if component == "manifest-sha":
            destination.write_text("0" * 64 + "  channel-rust-1.93.1.toml\n")

    monkeypatch.setattr(p.module, "download", corrupt)
    with pytest.raises(p.module.ProvisioningError):
        p.module.prepare_dist(HOST, VERSION, p.root, p.cache, p.downloads)
    assert not any(component in COMPONENTS for _, component in p.calls)


@pytest.mark.parametrize("case", ["cache-directory", "downloads-directory", "downloads-file", "outside-run", "global-home"])
def test_unsafe_paths_are_refused_without_cache_publication(provision, case: str) -> None:
    p = provision
    external = p.root.parent / "external"
    external.mkdir()
    if case == "cache-directory":
        p.cache.mkdir()
        (p.cache / "dist").symlink_to(external, target_is_directory=True)
    elif case == "downloads-directory":
        p.downloads.parent.mkdir(parents=True)
        p.downloads.symlink_to(external, target_is_directory=True)
    elif case == "downloads-file":
        p.downloads.mkdir(parents=True)
        target = external / "file"
        target.write_bytes(b"cargo")
        (p.downloads / hashlib.sha256(b"cargo").hexdigest()).symlink_to(target)
    elif case == "outside-run":
        p.downloads = external / "downloads"
    else:
        p.cache = Path(p.module.os.environ["HOME"]) / ".rustup"
    with pytest.raises(p.module.ProvisioningError):
        p.module.prepare_dist(HOST, VERSION, p.root, p.cache, p.downloads)
    assert not list(p.cache.rglob(".publish-*"))


def test_download_has_diagnostic_and_bounded_tls_options(dist, tmp_path: Path, monkeypatch) -> None:
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, options=kwargs)
        return SimpleNamespace(returncode=28)

    monkeypatch.setattr(dist.subprocess, "run", run)
    url = f"https://static.rust-lang.org/dist/{DATE}/rustc-{VERSION}-{HOST}.tar.xz"
    with pytest.raises(dist.ProvisioningError, match="curl exit 28"):
        dist.download(url, tmp_path / "archive", "rustc")
    command = observed["command"]
    for flag in ("--proto", "--proto-redir", "--connect-timeout", "--max-time", "--speed-limit", "--speed-time", "--retry", "--retry-max-time", "--write-out"):
        assert flag in command
    assert "--insecure" not in command and "-k" not in command
    assert command[command.index("--proto") + 1] == "=https"
    assert int(command[command.index("--retry") + 1]) <= 2
    assert observed["options"]["timeout"] <= 750


@pytest.mark.parametrize(("host", "component", "speed_limit"), [
    ("Darwin", "manifest", "1024"), ("Darwin", "manifest-sha", "1024"),
    ("Darwin", "rustc", "16384"), ("Linux", "manifest", "16384"),
])
def test_small_manifest_tolerates_slow_progress_without_changing_deadlines(
    dist, tmp_path: Path, monkeypatch, host: str, component: str, speed_limit: str
) -> None:
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, options=kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dist.subprocess, "run", run)
    monkeypatch.setattr(dist.platform, "system", lambda: host)
    dist.download("https://static.rust-lang.org/dist/file", tmp_path / "file", component)
    command = observed["command"]

    def value(flag: str) -> str:
        return command[command.index(flag) + 1]

    assert value("--speed-limit") == speed_limit
    assert value("--speed-time") == "30"
    assert value("--max-time") == ("60" if component.startswith("manifest") else "240")
    assert value("--retry") == ("1" if component.startswith("manifest") else "2")
    assert observed["options"]["timeout"] == (140 if component.startswith("manifest") else 750)


@pytest.mark.parametrize("url", ["http://static.rust-lang.org/dist/file", "https://example.invalid/dist/file", "https://static.rust-lang.org:444/dist/file"])
def test_nonofficial_download_urls_never_reach_curl(dist, tmp_path: Path, monkeypatch, url: str) -> None:
    monkeypatch.setattr(dist.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("curl executed"))
    with pytest.raises(dist.ProvisioningError):
        dist.download(url, tmp_path / "archive", "rustc")


def test_download_process_deadline_is_an_explicit_failure(dist, tmp_path: Path, monkeypatch) -> None:
    def timeout(command, **kwargs):
        raise dist.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(dist.subprocess, "run", timeout)
    with pytest.raises(dist.ProvisioningError, match="bounded curl process deadline"):
        dist.download("https://static.rust-lang.org/dist/archive", tmp_path / "archive", "rustc")


def test_unsupported_host_or_version_is_not_an_empty_success(provision) -> None:
    p = provision
    for host, version in (("unknown-host", VERSION), (HOST, "stable")):
        with pytest.raises(p.module.ProvisioningError):
            p.module.prepare_dist(host, version, p.root, p.cache, p.downloads)
    assert p.calls == []


def test_cli_wrong_machine_host_is_refused_before_path_or_network_use(dist, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dist, "current_host", lambda: HOST)
    monkeypatch.setattr(dist, "download", lambda *_args: pytest.fail("download executed"))
    result = dist.main([
        "--host", "aarch64-apple-darwin", "--run-root", str(tmp_path / "run"),
        "--cache-root", str(tmp_path / "cache"), "--downloads", str(tmp_path / "run/downloads"),
    ])
    assert result == 1
    assert list(tmp_path.iterdir()) == []


def test_failed_later_component_preserves_only_verified_earlier_archives(provision, monkeypatch) -> None:
    p = provision

    def fail_rustc(url, destination, component):
        if component == "rustc":
            destination.write_bytes(b"partial download")
            raise p.module.ProvisioningError("rustc bounded download failed")
        p.fetch(url, destination, component)

    monkeypatch.setattr(p.module, "download", fail_rustc)
    with pytest.raises(p.module.ProvisioningError):
        p.module.prepare_dist(HOST, VERSION, p.root, p.cache, p.downloads)
    for component in ("cargo", "rust-std"):
        assert (p.cache / "dist" / VERSION / HOST / hashlib.sha256(component.encode()).hexdigest()).is_file()
    assert not (p.cache / "dist" / VERSION / HOST / hashlib.sha256(b"rustc").hexdigest()).exists()
    monkeypatch.setattr(p.module, "download", p.fetch)
    p.calls.clear()
    second = p.root.parent / "second-run"
    p.module.prepare_dist(HOST, VERSION, second, p.cache, second / "rustup/downloads")
    assert [component for _, component in p.calls] == ["manifest", "manifest-sha", "rustc"]
