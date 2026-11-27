import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.acquisition.extract import extract_hc_archive
from hyprial.daemon.impl.terminals.acquisition.extract import installed_hc_executable
from hyprial.daemon.impl.terminals.acquisition.extract import remove_hc_version_dir


def tar_archive(path: Path, members: list[tuple[str, bytes, int, str | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, body, mode, link in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                archive.addfile(info)
            else:
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))


def test_macos_extract_keeps_signed_bundle_only(tmp_path: Path) -> None:
    archive = tmp_path / "macos.tar.gz"
    tar_archive(
        archive,
        [
            ("herdr-core", b"bare-binary", 0o755, None),
            ("Herdr Core.app/Contents/MacOS/herdr-core", b"signed-bundle-binary", 0o755, None),
            ("Herdr Core.app/Contents/_CodeSignature/CodeResources", b"signature", 0o644, None),
            ("Herdr Core.app/Contents/CodeResources", b"", 0o644, "_CodeSignature/CodeResources"),
        ],
    )
    version_root = extract_hc_archive(archive, "macos-aarch64", tmp_path, "0.1.22")
    executable = installed_hc_executable("0.1.22", tmp_path, "macos-aarch64")
    assert version_root == tmp_path / "state" / "hc" / "bin" / "0.1.22"
    assert executable.read_bytes() == b"signed-bundle-binary"
    assert executable == version_root / "Herdr Core.app" / "Contents" / "MacOS" / "herdr-core"
    link = version_root / "Herdr Core.app" / "Contents" / "CodeResources"
    assert link.is_symlink()
    assert link.readlink() == Path("_CodeSignature/CodeResources")
    assert not (version_root / "herdr-core").exists()


def test_tar_directory_modes_are_preserved(tmp_path: Path) -> None:
    archive = tmp_path / "tar-modes.tar.gz"
    tar_archive(
        archive,
        [
            ("Herdr Core.app/Contents/MacOS/herdr-core", b"signed", 0o755, None),
            ("Herdr Core.app/", b"", 0o750, None),
            ("Herdr Core.app/Contents/", b"", 0o751, None),
            ("Herdr Core.app/Contents/MacOS/", b"", 0o752, None),
        ],
    )
    root = extract_hc_archive(archive, "macos-aarch64", tmp_path, "0.1.22")
    assert stat.S_IMODE((root / "Herdr Core.app").stat().st_mode) == 0o750
    assert stat.S_IMODE((root / "Herdr Core.app/Contents").stat().st_mode) == 0o751
    assert stat.S_IMODE((root / "Herdr Core.app/Contents/MacOS").stat().st_mode) == 0o752


def test_zip_directory_modes_are_preserved(tmp_path: Path) -> None:
    archive = tmp_path / "zip-modes.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("herdr-core.exe", b"windows")
        output.writestr("conpty/x64/OpenConsole.exe", b"console")
        for name, mode in (("conpty/", 0o750), ("conpty/x64/", 0o751)):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFDIR | mode) << 16
            output.writestr(info, b"")
    root = extract_hc_archive(archive, "windows-x86_64", tmp_path, "0.1.22")
    assert stat.S_IMODE((root / "conpty").stat().st_mode) == 0o750
    assert stat.S_IMODE((root / "conpty/x64").stat().st_mode) == 0o751


def test_linux_extract_marks_executable(tmp_path: Path) -> None:
    archive = tmp_path / "linux.tar.gz"
    tar_archive(archive, [("herdr-core", b"linux", 0o600, None)])
    extract_hc_archive(archive, "linux-x86_64", tmp_path, "0.1.22")
    executable = installed_hc_executable("0.1.22", tmp_path, "linux-x86_64")
    assert executable.read_bytes() == b"linux"
    assert executable.stat().st_mode & 0o111


def test_windows_extracts_conpty_tree(tmp_path: Path) -> None:
    archive = tmp_path / "windows.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("herdr-core.exe", b"windows")
        output.writestr("conpty/conpty.dll", b"dll")
        output.writestr("conpty/x64/OpenConsole.exe", b"console")
    extract_hc_archive(archive, "windows-x86_64", tmp_path, "0.1.22")
    root = tmp_path / "state" / "hc" / "bin" / "0.1.22"
    assert (root / "herdr-core.exe").read_bytes() == b"windows"
    assert (root / "conpty/conpty.dll").read_bytes() == b"dll"
    assert (root / "conpty/x64/OpenConsole.exe").read_bytes() == b"console"


@pytest.mark.parametrize("name", ["../evil", "/abs/evil"])
def test_extract_rejects_traversal_without_staging_remains(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bad.tar.gz"
    tar_archive(archive, [(name, b"evil", 0o644, None)])
    with pytest.raises(Exception):
        extract_hc_archive(archive, "linux-x86_64", tmp_path, "0.1.22")
    bin_root = tmp_path / "state" / "hc" / "bin"
    assert not (bin_root / "0.1.22").exists()
    assert not tuple(bin_root.glob(".staging-*"))


def test_extract_rejects_escaping_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "bad-link.tar.gz"
    tar_archive(
        archive,
        [("Herdr Core.app/Contents/escape", b"", 0o644, "../../../outside")],
    )
    with pytest.raises(Exception):
        extract_hc_archive(archive, "macos-aarch64", tmp_path, "0.1.22")
    assert not tuple((tmp_path / "state" / "hc" / "bin").glob(".staging-*"))


def test_remove_version_unregisters_macos_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version_root = tmp_path / "state" / "hc" / "bin" / "0.1.22"
    bundle = version_root / "Herdr Core.app"
    bundle.mkdir(parents=True)
    called: list[Path] = []
    monkeypatch.setattr(
        "hyprial.daemon.impl.terminals.acquisition.extract.unregister_macos_app_bundle",
        lambda path: called.append(path) or True,
    )
    assert remove_hc_version_dir("0.1.22", tmp_path, "macos-aarch64")
    assert called == [bundle]
    assert not version_root.exists()
