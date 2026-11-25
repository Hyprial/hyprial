"""The wheel's native actor engine stage (``hyprial._core.actors``).

The Hatch hook compiles ``crates/hyprial-py`` with cargo, verifies the module
(import + ``ENGINE_ABI_VERSION == 1``, no libpython, glibc floor) and
force-includes it as ``hyprial/_core/actors.abi3.so`` under a
``cp312-abi3-<plat>`` tag.  These tests drive the real hook and helper with a
recording fake cargo, so no Rust toolchain is needed; only the import probe
is replaced, because the fake cargo cannot produce a loadable extension.
``runpy.run_path`` hands the hook a copy of the helper's globals, so the
probe is replaced in the namespace the helper functions actually read.  The
real build is exercised by ``uv build --wheel`` (evidence in the commit).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import runpy
import shlex
import struct
import sys
import types
from pathlib import Path

import pytest

from hyprial.shell.impl.packaging import native_engine
from hyprial.shell.impl.packaging.tailcat_wheel import TARGET_ENV, host_target

REPO = Path(__file__).resolve().parents[4]
HOOK_PATH = REPO / "src/hyprial/gui_build_hook.py"
SKIP_ENV = "HYPRIAL_BUILD_SKIP_NATIVE"
WORKER_ENV = "HYPRIAL_BUILD_WORKER_WHEEL"


def _hook_module():
    try:
        import hatchling.builders.hooks.plugin.interface  # noqa: F401
    except ModuleNotFoundError:
        names = [
            "hatchling",
            "hatchling.builders",
            "hatchling.builders.hooks",
            "hatchling.builders.hooks.plugin",
            "hatchling.builders.hooks.plugin.interface",
        ]
        for name in names:
            sys.modules.setdefault(name, types.ModuleType(name))
        for parent, child in zip(names, names[1:]):
            setattr(sys.modules[parent], child.split(".")[-1], sys.modules[child])
        sys.modules[names[-1]].BuildHookInterface = type("BuildHookInterface", (), {})
    spec = importlib.util.spec_from_file_location(
        "hyprial_gui_build_hook_native_stage", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _hook(module, root: Path, *, target_name: str = "wheel"):
    class _Hook(module.CustomBuildHook):
        def __init__(self) -> None:
            self.root = str(root)
            self.metadata = types.SimpleNamespace(version="9.9.9")
            self.target_name = target_name

    return _Hook()


def _crates_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    source = root / "crates" / "hyprial-py" / "src"
    source.mkdir(parents=True)
    (root / "crates" / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (root / "crates" / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (root / "crates" / "hyprial-py" / "Cargo.toml").write_text(
        '[package]\nname = "hyprial-py"\n\n[dependencies]\n'
        'pyo3 = { version = "0.30", features = ["abi3-py312", "py-clone"] }\n',
        encoding="utf-8",
    )
    (source / "lib.rs").write_text("// engine\n", encoding="utf-8")
    return root


def _fake_cargo(tmp_path: Path, *, library: str = "libactors.so", exit_code: int = 0) -> Path:
    """A cargo that records argv/env and reports one ``actors`` cdylib."""

    out = tmp_path / "cargo-out"
    out.mkdir()
    produced = out / library
    log = tmp_path / "cargo.log"
    message = json.dumps(
        {
            "reason": "compiler-artifact",
            "target": {"name": "actors", "kind": ["cdylib", "rlib"]},
            "filenames": [str(out / "libactors.rlib"), str(produced)],
        }
    )
    script = tmp_path / "bin" / "cargo"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > {shlex.quote(str(log))}\n"
        f"printf 'PYO3_PYTHON=%s\\n' \"$PYO3_PYTHON\" >> {shlex.quote(str(log))}\n"
        f"printf 'RUSTUP_TOOLCHAIN=%s\\n' \"$RUSTUP_TOOLCHAIN\" >> {shlex.quote(str(log))}\n"
        f"if [ {exit_code} -ne 0 ]; then echo 'error[E0001]: broken' >&2; exit {exit_code}; fi\n"
        f"printf 'native engine bytes' > {shlex.quote(str(produced))}\n"
        f"printf '%s\\n' {shlex.quote(message)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture()
def native_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A build environment with no opt-out, no worker switch and no cargo."""

    monkeypatch.delenv(SKIP_ENV, raising=False)
    monkeypatch.delenv(WORKER_ENV, raising=False)
    monkeypatch.delenv(TARGET_ENV, raising=False)
    monkeypatch.delenv("RUSTUP_TOOLCHAIN", raising=False)
    monkeypatch.setenv(native_engine.CARGO_ENV, "")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def _probe_ok(module, monkeypatch: pytest.MonkeyPatch, version: object = 1) -> list[tuple[Path, str]]:
    calls: list[tuple[Path, str]] = []

    def probe(path: Path, python: str) -> object:
        calls.append((Path(path), python))
        return version

    namespace = module._NATIVE_TOOLS["require_abi"].__globals__
    monkeypatch.setitem(namespace, "probe_abi_version", probe)
    return calls


def _elf(needed: list[str], versions: list[str]) -> bytes:
    """A minimal little-endian ELF64 with .dynstr, .dynamic and .gnu.version_r."""

    strings = b"\0"
    offsets: dict[str, int] = {}
    for name in [*needed, "libc.so.6", *versions]:
        if name not in offsets:
            offsets[name] = len(strings)
            strings += name.encode() + b"\0"
    dynamic = b"".join(struct.pack("<qQ", 1, offsets[name]) for name in needed)
    dynamic += struct.pack("<qQ", 0, 0)
    verneed = struct.pack("<HHIII", 1, len(versions), offsets["libc.so.6"], 16, 0)
    for index, name in enumerate(versions):
        following = 16 if index + 1 < len(versions) else 0
        verneed += struct.pack("<IHHII", 0, 0, index + 2, offsets[name], following)
    body_offset = 64
    dynstr_offset = body_offset
    dynamic_offset = dynstr_offset + len(strings)
    verneed_offset = dynamic_offset + len(dynamic)
    section_offset = verneed_offset + len(verneed)
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", header, 0x28, section_offset)
    struct.pack_into("<HH", header, 0x3A, 64, 4)

    def section(sh_type: int, offset: int, size: int, link: int) -> bytes:
        raw = bytearray(64)
        struct.pack_into("<I", raw, 4, sh_type)
        struct.pack_into("<QQ", raw, 0x18, offset, size)
        struct.pack_into("<I", raw, 0x28, link)
        return bytes(raw)

    sections = (
        section(0, 0, 0, 0)
        + section(3, dynstr_offset, len(strings), 0)
        + section(6, dynamic_offset, len(dynamic), 1)
        + section(0x6FFFFFFE, verneed_offset, len(verneed), 1)
    )
    return bytes(header) + strings + dynamic + verneed + sections


def test_opt_out_is_only_the_exact_value_one() -> None:
    assert native_engine.skip_requested({SKIP_ENV: "1"}) is True
    for value in ("", "0", "true", "yes", " 1"):
        assert native_engine.skip_requested({SKIP_ENV: value}) is False
    assert native_engine.skip_requested({}) is False


@pytest.mark.parametrize(
    ("target", "path"),
    [
        ("linux/amd64", "hyprial/_core/actors.abi3.so"),
        ("darwin/arm64", "hyprial/_core/actors.abi3.so"),
        ("windows/amd64", "hyprial/_core/actors.pyd"),
    ],
)
def test_the_module_lands_at_the_r5_path(target: str, path: str) -> None:
    assert native_engine.wheel_module_path(target) == path
    assert native_engine.MODULE_NAME == "hyprial._core.actors"


def test_the_wheel_tag_is_cp312_abi3_on_the_existing_platform_tag() -> None:
    assert (
        native_engine.wheel_tag("manylinux_2_17_x86_64.manylinux2014_x86_64")
        == "cp312-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64"
    )
    assert native_engine.wheel_tag("macosx_13_0_arm64") == "cp312-abi3-macosx_13_0_arm64"


def test_cargo_runs_the_exact_adr_command(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert native_engine.cargo_command("cargo", root) == [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        str(root / "crates/Cargo.toml"),
        "-p",
        "hyprial-py",
        "--features",
        "extension-module",
        "--message-format=json-render-diagnostics",
    ]


def test_cargo_gets_the_absolute_build_interpreter_and_the_pinned_toolchain() -> None:
    env = native_engine.cargo_environment("relative/python", {"PATH": "/x"})
    assert env["PYO3_PYTHON"] == os.path.abspath("relative/python")
    assert os.path.isabs(env["PYO3_PYTHON"])
    assert env["RUSTUP_TOOLCHAIN"] == "1.93.1"
    chosen = native_engine.cargo_environment(sys.executable, {"RUSTUP_TOOLCHAIN": "stable"})
    assert chosen["RUSTUP_TOOLCHAIN"] == "stable"
    assert chosen["PYO3_PYTHON"] == os.path.abspath(sys.executable)


def test_the_artifact_comes_from_cargo_messages() -> None:
    lines = "\n".join(
        [
            "not json",
            json.dumps({"reason": "build-script-executed"}),
            json.dumps(
                {
                    "reason": "compiler-artifact",
                    "target": {"name": "hyprial_core", "kind": ["lib"]},
                    "filenames": ["/t/libhyprial_core.rlib"],
                }
            ),
            json.dumps(
                {
                    "reason": "compiler-artifact",
                    "target": {"name": "actors", "kind": ["cdylib", "rlib"]},
                    "filenames": ["/t/libactors.rlib", "/t/libactors.so"],
                }
            ),
        ]
    )
    assert native_engine.library_from_messages(lines) == Path("/t/libactors.so")
    with pytest.raises(native_engine.NativeEngineError, match="0 cdylib"):
        native_engine.library_from_messages("")


def test_elf_audit_reads_needed_libraries_and_the_glibc_floor() -> None:
    data = _elf(["libgcc_s.so.1", "libc.so.6"], ["GLIBC_2.2.5", "GLIBC_2.34", "GLIBC_2.17"])
    needed, glibc = native_engine.elf_dependencies(data)
    assert needed == ["libgcc_s.so.1", "libc.so.6"]
    assert glibc == (2, 34)
    assert native_engine.elf_dependencies(b"MZ not elf") == ([], None)


def test_a_libpython_dependency_fails_the_audit(tmp_path: Path) -> None:
    module = tmp_path / "actors.abi3.so"
    module.write_bytes(_elf(["libpython3.12.so.1.0", "libc.so.6"], ["GLIBC_2.17"]))
    with pytest.raises(native_engine.NativeEngineError, match="libpython"):
        native_engine.audit_library(module)
    module.write_bytes(_elf(["libc.so.6"], ["GLIBC_2.16"]))
    assert native_engine.audit_library(module) == (2, 16)


def test_a_newer_glibc_raises_the_tag_loudly_instead_of_lying() -> None:
    declared = "manylinux_2_17_x86_64.manylinux2014_x86_64"
    assert native_engine.platform_tag_for_floor(declared, (2, 16)) == (declared, None)
    assert native_engine.platform_tag_for_floor(declared, (2, 17)) == (declared, None)
    assert native_engine.platform_tag_for_floor(declared, None) == (declared, None)
    opt_in = {native_engine.ALLOW_GLIBC_RAISE_ENV: "1"}
    tag, warning = native_engine.platform_tag_for_floor(declared, (2, 34), opt_in)
    assert tag == "manylinux_2_34_x86_64"
    assert warning is not None and "GLIBC_2.34" in warning and "manylinux2014" in warning
    assert native_engine.platform_tag_for_floor("macosx_13_0_arm64", (2, 34)) == (
        "macosx_13_0_arm64",
        None,
    )


def test_no_cargo_is_a_clear_error_naming_the_opt_out(
    tmp_path: Path, native_env: None
) -> None:
    root = _crates_root(tmp_path)
    with pytest.raises(native_engine.NativeEngineError) as caught:
        native_engine.build_extension(
            root, host_target(), host=host_target(), output=tmp_path / "out"
        )
    text = str(caught.value)
    assert "Rust toolchain" in text
    assert "1.93.1" in text
    assert f"{SKIP_ENV}=1" in text
    assert "must not be published" in text


def test_a_cross_target_is_refused(tmp_path: Path, native_env: None) -> None:
    root = _crates_root(tmp_path)
    foreign = "windows/amd64" if host_target() != "windows/amd64" else "linux/amd64"
    with pytest.raises(native_engine.NativeEngineError, match="build host"):
        native_engine.build_extension(root, foreign, host=host_target(), output=tmp_path)


def test_missing_crates_sources_are_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(native_engine.NativeEngineError, match="crates/Cargo.toml"):
        native_engine.require_sources(tmp_path)


def test_a_failing_cargo_build_reports_its_diagnostics(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _crates_root(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path, exit_code=101)))
    with pytest.raises(native_engine.NativeEngineError, match="E0001"):
        native_engine.build_extension(root, host_target(), host=host_target(), output=tmp_path / "o")


def test_a_wrong_abi_version_fails_the_stage(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _crates_root(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
    monkeypatch.setattr(native_engine, "probe_abi_version", lambda path, python: 2)
    with pytest.raises(native_engine.NativeEngineError, match="ENGINE_ABI_VERSION 2"):
        native_engine.build_extension(root, host_target(), host=host_target(), output=tmp_path / "o")


def test_the_real_probe_rejects_a_file_that_is_not_an_extension(tmp_path: Path) -> None:
    bogus = tmp_path / "actors.abi3.so"
    bogus.write_bytes(b"not an extension")
    with pytest.raises(native_engine.NativeEngineError, match="does not import"):
        native_engine.probe_abi_version(bogus, sys.executable)


def test_the_source_digest_follows_sources_and_ignores_target_dirs(tmp_path: Path) -> None:
    root = _crates_root(tmp_path)
    before = native_engine.source_digest(root)
    target = root / "crates" / "target" / "release"
    target.mkdir(parents=True)
    (target / "libactors.so").write_bytes(b"build output")
    (root / "crates" / "target-ft").mkdir()
    assert native_engine.source_digest(root) == before
    (root / "crates" / "hyprial-py" / "src" / "lib.rs").write_text("// changed\n", encoding="utf-8")
    assert native_engine.source_digest(root) != before


def _stage_editable(root: Path) -> Path:
    module = root / native_engine.EDITABLE_MODULE
    module.parent.mkdir(parents=True)
    module.write_bytes(b"editable module")
    native_engine.write_stamp(root, module, features="extension-module", profile="release")
    return module


def test_editable_verification_accepts_a_fresh_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _crates_root(tmp_path)
    assert native_engine.verify_editable(root) is None
    _stage_editable(root)
    monkeypatch.setattr(native_engine, "probe_abi_version", lambda path, python: 1)
    assert "matches crates/" in native_engine.verify_editable(root)


def test_editable_verification_rejects_stale_unstamped_and_foreign_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _crates_root(tmp_path)
    module = _stage_editable(root)
    monkeypatch.setattr(native_engine, "probe_abi_version", lambda path, python: 1)
    (root / "crates" / "hyprial-py" / "src" / "lib.rs").write_text("// edited\n", encoding="utf-8")
    with pytest.raises(native_engine.NativeEngineError, match="stale.*HYPRIAL_BUILD_NATIVE=1"):
        native_engine.verify_editable(root)
    native_engine.write_stamp(root, module, features="extension-module", profile="release")
    module.write_bytes(b"swapped module")
    with pytest.raises(native_engine.NativeEngineError, match="not the module its stamp"):
        native_engine.verify_editable(root)
    native_engine.stamp_path(module).unlink()
    with pytest.raises(
        native_engine.NativeEngineError, match="no readable build stamp.*HYPRIAL_BUILD_NATIVE=1"
    ):
        native_engine.verify_editable(root)
    native_engine.write_stamp(root, module, features="extension-module", profile="release")
    monkeypatch.setattr(native_engine, "probe_abi_version", lambda path, python: 0)
    with pytest.raises(native_engine.NativeEngineError, match="ENGINE_ABI_VERSION 0"):
        native_engine.verify_editable(root)


def test_the_stamp_cli_writes_the_record(tmp_path: Path) -> None:
    root = _crates_root(tmp_path)
    module = root / native_engine.EDITABLE_MODULE
    module.parent.mkdir(parents=True)
    module.write_bytes(b"m")
    assert native_engine.main(["stamp", str(root), str(module), "extension-module", "debug"]) == 0
    record = json.loads(native_engine.stamp_path(module).read_text(encoding="utf-8"))
    assert record["source_sha256"] == native_engine.source_digest(root)
    assert record["profile"] == "debug"
    assert native_engine.main(["stamp"]) == 2


def test_hook_wheel_stages_the_module_with_the_abi3_tag(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
    probes = _probe_ok(module, monkeypatch)
    hook = _hook(module, root)
    hook._stages = []
    build_data: dict = {"force_include": {}, "tag": "py3-none-anything", "pure_python": False}

    hook._stage_native(root, build_data)

    target = host_target()
    staged = {destination: source for source, destination in build_data["force_include"].items()}
    expected = "hyprial/_core/actors.pyd" if target.startswith("windows/") else "hyprial/_core/actors.abi3.so"
    assert list(staged) == [expected]
    assert Path(staged[expected]).read_bytes() == b"native engine bytes"
    platform_tag = module._TAILCAT_TOOLS["platform_tag"](target)
    assert build_data["tag"] == f"cp312-abi3-{platform_tag}"
    assert build_data["pure_python"] is False
    log = (tmp_path / "cargo.log").read_text(encoding="utf-8").splitlines()
    assert log[0] == (
        f"build --release --locked --manifest-path {root / 'crates/Cargo.toml'} "
        "-p hyprial-py --features extension-module --message-format=json-render-diagnostics"
    )
    assert log[1] == f"PYO3_PYTHON={os.path.abspath(sys.executable)}"
    assert log[2] == "RUSTUP_TOOLCHAIN=1.93.1"
    assert probes == [(Path(staged[expected]), sys.executable)]
    staged_dir = Path(staged[expected]).parent
    hook.finalize("standard", build_data, "unused")
    assert not staged_dir.exists()


def test_hook_without_cargo_fails_closed(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    monkeypatch.setenv(WORKER_ENV, "1")
    hook = _hook(module, root)

    with pytest.raises(module.NativeEngineError, match=SKIP_ENV):
        hook.initialize("standard", {"force_include": {}})


def test_a_typo_in_the_opt_out_still_fails_closed(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    monkeypatch.setenv(WORKER_ENV, "1")
    monkeypatch.setenv(SKIP_ENV, "true")
    with pytest.raises(module.NativeEngineError):
        _hook(module, _crates_root(tmp_path)).initialize("standard", {"force_include": {}})


def test_the_worker_wheel_keeps_the_native_stage(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    monkeypatch.setenv(WORKER_ENV, "1")
    monkeypatch.setenv(native_engine.CARGO_ENV, str(_fake_cargo(tmp_path)))
    _probe_ok(module, monkeypatch)
    build_data: dict = {"force_include": {}}

    _hook(module, root).initialize("standard", build_data)

    destinations = sorted(build_data["force_include"].values())
    assert len(destinations) == 1 and destinations[0].startswith("hyprial/_core/actors.")
    assert not any(d.startswith(("hyprial/_bin/", "hyprial/_gui/")) for d in destinations)
    assert build_data["tag"].startswith("cp312-abi3-")


def test_the_explicit_opt_out_skips_the_stage_and_says_so(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _hook_module()
    monkeypatch.setenv(WORKER_ENV, "1")
    monkeypatch.setenv(SKIP_ENV, "1")
    build_data: dict = {}

    _hook(module, tmp_path).initialize("standard", build_data)

    assert build_data == {}
    error = capsys.readouterr().err
    assert f"{SKIP_ENV}=1" in error and "must not be published" in error


def test_the_tailcat_escape_hatch_is_not_a_native_opt_out(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    monkeypatch.setenv(WORKER_ENV, "1")
    monkeypatch.setenv("HYPRIAL_TAILCAT_ALLOW_MISSING", "1")
    with pytest.raises(module.NativeEngineError):
        _hook(module, _crates_root(tmp_path)).initialize("standard", {"force_include": {}})


def test_the_sdist_requires_the_crates_sources(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    monkeypatch.setenv(WORKER_ENV, "1")
    build_data: dict = {"force_include": {}}
    _hook(module, _crates_root(tmp_path), target_name="sdist").initialize("standard", build_data)
    assert build_data == {"force_include": {}}
    with pytest.raises(module.NativeEngineError, match="crates/Cargo.toml"):
        _hook(module, tmp_path / "bare", target_name="sdist").initialize("standard", {})


def test_editable_build_verifies_a_staged_module(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    build_data: dict = {"force_include": {}}
    _hook(module, root).initialize("editable", build_data)
    assert build_data == {"force_include": {}}
    assert "build_native_engine.sh" in capsys.readouterr().err

    _stage_editable(root)
    _probe_ok(module, monkeypatch)
    _hook(module, root).initialize("editable", build_data)
    assert build_data == {"force_include": {}}
    (root / "crates" / "Cargo.lock").write_text("version = 5\n", encoding="utf-8")
    with pytest.raises(module.NativeEngineError, match="stale"):
        _hook(module, root).initialize("editable", build_data)
    monkeypatch.setenv(SKIP_ENV, "1")
    _hook(module, root).initialize("editable", build_data)
    assert build_data == {"force_include": {}}


def _module_bytes(target: str, *, libpython: bool = False) -> bytes:
    os_name, arch = target.split("/")
    if os_name == "linux":
        machine = 62 if arch == "amd64" else 183
        if libpython:
            body = bytearray(
                _elf(["libpython3.12.so.1.0", "libc.so.6"], ["GLIBC_2.17"])
            )
            struct.pack_into("<H", body, 18, machine)
            return bytes(body)
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 16, 3)
        struct.pack_into("<H", header, 18, machine)
        return bytes(header)
    if os_name == "darwin":
        header = bytearray(32)
        header[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", header, 4, 0x01000007 if arch == "amd64" else 0x0100000C)
        return bytes(header)
    data = bytearray(0x80)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, 0x8664 if arch == "amd64" else 0xAA64)
    return bytes(data)


def _write_prebuilt(
    tmp_path: Path,
    root: Path,
    target: str,
    *,
    data: bytes | None = None,
    sha256: str | None = None,
    size: int | None = None,
    source_sha256: str | None = None,
    module_name: str | None = None,
    engine_abi_version: int = 1,
    platform_tag: str | None = None,
    schema: str = "hyprial.native-prebuilt/v1",
) -> tuple[Path, Path]:
    directory = tmp_path / "prebuilt"
    folder = directory / target.replace("/", "-")
    folder.mkdir(parents=True)
    module = folder / native_engine.wheel_module_name(target)
    body = _module_bytes(target) if data is None else data
    module.write_bytes(body)
    document = {
        "schema": schema,
        "source_sha256": source_sha256 or native_engine.source_digest(root),
        "targets": {
            target: {
                "module": module_name or module.name,
                "sha256": sha256 or hashlib.sha256(body).hexdigest(),
                "size": len(body) if size is None else size,
                "engine_abi_version": engine_abi_version,
                "platform_tag": platform_tag
                or _TAILCAT_TOOLS["platform_tag"](target),
            }
        },
    }
    (directory / native_engine.PREBUILT_MANIFEST).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return directory, module


_TAILCAT_TOOLS = runpy.run_path(
    str(REPO / "src/hyprial/shell/impl/packaging/tailcat_wheel.py")
)


def test_prebuilt_stage_copies_a_host_module_without_cargo(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    target = host_target()
    directory, source = _write_prebuilt(tmp_path, root, target)
    monkeypatch.setenv(native_engine.PREBUILT_ENV, str(directory))
    monkeypatch.setenv(WORKER_ENV, "1")
    build_data: dict = {"force_include": {}}

    hook = _hook(module, root)
    hook.initialize("standard", build_data)

    staged = {destination: Path(source) for source, destination in build_data["force_include"].items()}
    expected = "hyprial/_core/actors.pyd" if target.startswith("windows/") else "hyprial/_core/actors.abi3.so"
    assert list(staged) == [expected]
    assert staged[expected].read_bytes() == source.read_bytes()
    assert build_data["tag"] == f"cp312-abi3-{_TAILCAT_TOOLS['platform_tag'](target)}"
    assert build_data["pure_python"] is False
    hook.finalize("standard", build_data, "unused")
    assert not staged[expected].parent.exists()


def test_prebuilt_cross_target_skips_the_host_only_preflight(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    target = "linux/amd64" if host_target() != "linux/amd64" else "windows/amd64"
    directory, source = _write_prebuilt(tmp_path, root, target)
    monkeypatch.setenv(native_engine.PREBUILT_ENV, str(directory))
    monkeypatch.setenv(TARGET_ENV, target)
    monkeypatch.setenv(WORKER_ENV, "1")
    build_data: dict = {"force_include": {}}

    _hook(module, root).initialize("standard", build_data)

    staged = {destination: Path(source) for source, destination in build_data["force_include"].items()}
    expected = "hyprial/_core/actors.pyd" if target.startswith("windows/") else "hyprial/_core/actors.abi3.so"
    assert list(staged) == [expected]
    assert build_data["tag"] == f"cp312-abi3-{_TAILCAT_TOOLS['platform_tag'](target)}"


def test_prebuilt_rejects_a_digest_mismatch(
    tmp_path: Path, native_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    directory, _ = _write_prebuilt(
        tmp_path, root, host_target(), sha256="0" * 64
    )
    with pytest.raises(module.NativeEngineError, match="sha256"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, host_target(), environ={native_engine.PREBUILT_ENV: str(directory)}
        )


def test_prebuilt_rejects_another_source_digest(
    tmp_path: Path,
    native_env: None,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    directory, _ = _write_prebuilt(
        tmp_path, root, host_target(), source_sha256="0" * 64
    )
    with pytest.raises(module.NativeEngineError, match="source_sha256"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, host_target(), environ={native_engine.PREBUILT_ENV: str(directory)}
        )


def test_prebuilt_rejects_a_module_for_the_wrong_architecture(
    tmp_path: Path,
    native_env: None,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    target = "darwin/arm64"
    directory, _ = _write_prebuilt(
        tmp_path, root, target, data=_module_bytes("darwin/amd64")
    )
    with pytest.raises(module.NativeEngineError, match="needs arm64"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, target, environ={native_engine.PREBUILT_ENV: str(directory)}
        )


def test_prebuilt_rejects_a_linux_module_that_links_libpython(
    tmp_path: Path,
    native_env: None,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    target = "linux/amd64"
    directory, _ = _write_prebuilt(
        tmp_path, root, target, data=_module_bytes(target, libpython=True)
    )
    with pytest.raises(module.NativeEngineError, match="libpython"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, target, environ={native_engine.PREBUILT_ENV: str(directory)}
        )


def test_prebuilt_rejects_a_missing_target_entry_and_a_symlinked_module(
    tmp_path: Path,
    native_env: None,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    directory = tmp_path / "prebuilt"
    directory.mkdir()
    (directory / native_engine.PREBUILT_MANIFEST).write_text(
        json.dumps(
            {
                "schema": native_engine.PREBUILT_SCHEMA,
                "source_sha256": native_engine.source_digest(root),
                "targets": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.NativeEngineError, match="no entry"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, host_target(), environ={native_engine.PREBUILT_ENV: str(directory)}
        )

    target = host_target()
    directory, source = _write_prebuilt(tmp_path, root, target)
    source.unlink()
    source.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(module.NativeEngineError, match="regular prebuilt module"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, target, environ={native_engine.PREBUILT_ENV: str(directory)}
        )


def test_prebuilt_rejects_a_manifest_with_another_schema(
    tmp_path: Path,
    native_env: None,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    directory, _ = _write_prebuilt(
        tmp_path, root, host_target(), schema="hyprial.native-prebuilt/v0"
    )
    with pytest.raises(module.NativeEngineError, match="schema"):
        module._NATIVE_TOOLS["verify_prebuilt"](
            root, host_target(), environ={native_engine.PREBUILT_ENV: str(directory)}
        )


def test_prebuilt_with_a_newer_glibc_fails_the_stage_unless_explicitly_allowed(
    tmp_path: Path,
    native_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _hook_module()
    root = _crates_root(tmp_path)
    target = "linux/amd64"
    body = bytearray(_elf(["libc.so.6"], ["GLIBC_2.34"]))
    struct.pack_into("<H", body, 18, 62)
    directory, _ = _write_prebuilt(tmp_path, root, target, data=bytes(body))
    monkeypatch.setenv(native_engine.PREBUILT_ENV, str(directory))
    monkeypatch.setenv(TARGET_ENV, target)
    monkeypatch.setenv(WORKER_ENV, "1")
    hook = _hook(module, root)
    hook._stages = []

    with pytest.raises(module.NativeEngineError, match="GLIBC_2.34"):
        hook.initialize("standard", {"force_include": {}})

    monkeypatch.setenv(native_engine.ALLOW_GLIBC_RAISE_ENV, "1")
    build_data: dict = {"force_include": {}}
    hook = _hook(module, root)
    hook.initialize("standard", build_data)
    assert build_data["tag"].startswith("cp312-abi3-manylinux_2_34_")


def test_the_library_cli_reports_the_cdylib_cargo_named(tmp_path: Path, capsys) -> None:
    library = tmp_path / "anywhere" / "libactors.so"
    messages = tmp_path / "messages.json"
    messages.write_text(
        json.dumps(
            {
                "reason": "compiler-artifact",
                "target": {"name": "actors", "kind": ["cdylib"]},
                "filenames": [str(library)],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert native_engine.main(["library", str(messages)]) == 0
    assert capsys.readouterr().out.strip() == str(library)
    messages.write_text("not cargo output\n", encoding="utf-8")
    assert native_engine.main(["library", str(messages)]) == 1
    assert "expected exactly one" in capsys.readouterr().err
