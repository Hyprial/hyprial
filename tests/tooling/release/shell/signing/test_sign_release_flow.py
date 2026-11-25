"""sign_release.py's flow with recorded tools, on every platform.

A fake runner stands in for codesign, hdiutil, ditto, xcrun (notarytool,
stapler) and spctl, and imitates what the flow relies on: signing changes a
file's bytes, `codesign -d` reports the flags and identity text a real
Developer ID signature would (a fake common name, Team ID and SHA-1, so the
redaction is tested against them), hdiutil attach copies a synthetic app
into the mount point. verify_runtime and the smoke are injected. The real
tools run in test_sign_release_macos.py.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import signal
import struct
import subprocess
from pathlib import Path

import pytest

from tests.tooling.release.shell.support import load

sign_release = load("sign_release")
stage_runtime = load("stage_runtime")
verify_runtime = load("verify_runtime")

LEAF = b"fake leaf certificate (DER)"
SHA1 = hashlib.sha1(LEAF).hexdigest().upper()
TEAM = "ABCDE12345"
COMMON_NAME = f"Developer ID Application: Fake Person ({TEAM})"
COMMIT = "a" * 40
PROFILE = "hyprial-notary-test"
DMG = "HyprialOS_0.2.0_aarch64.dmg"
MACHO = struct.pack("<I", 0xFEEDFACF) + b"\x0c\x00\x00\x01"
FAT = struct.pack(">II", 0xCAFEBABE, 2)
JAVA_CLASS = struct.pack(">IHH", 0xCAFEBABE, 0, 52)
SNAPSHOT = {"search_list": 'rc=0\n    "~/Library/Keychains/login.keychain-db"\n', "default_keychain": 'rc=0\n    "~/Library/Keychains/login.keychain-db"\n'}
RUNTIME_MACHOS = {
    "bin/python3.12",
    "lib/libpython3.12.dylib",
    "lib/python3.12/site-packages/pkg/_ext.cpython-312-darwin.so",
    "lib/python3.12/site-packages/pkg/deep/deeper/_x.so",
    "lib/python3.12/site-packages/pkg/noext_binary",
}


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def make_app(root: Path) -> Path:
    """A tiny HyprialOS.app whose Mach-O files are recognisable only by magic."""

    app = root / "HyprialOS.app"
    contents = app / "Contents"
    _write(contents / "Info.plist", plistlib.dumps({"CFBundleExecutable": "hyprial-desktop", "CFBundleIdentifier": "com.hyprial.hyprialos"}))
    _write(contents / "MacOS" / "hyprial-desktop", MACHO + b"main", 0o755)
    runtime = contents / "Resources" / "runtime"
    _write(runtime / "bin" / "python3.12", MACHO + b"python", 0o755)
    _write(runtime / "lib" / "libpython3.12.dylib", FAT + b"fat library")
    site = runtime / "lib" / "python3.12" / "site-packages" / "pkg"
    _write(site / "_ext.cpython-312-darwin.so", MACHO + b"ext")
    _write(site / "deep" / "deeper" / "_x.so", MACHO + b"x")
    _write(site / "noext_binary", MACHO + b"no extension", 0o755)
    _write(site / "__init__.py", b"VALUE = 1\n")
    _write(site / "fake.so", b"a text file named like a library\n")
    _write(site / "Thing.class", JAVA_CLASS + b"java")
    lock = {"python": "3.12.12", "pbsTag": "20261110", "targets": {"aarch64-apple-darwin": {"sha256": "b" * 64}}}
    facts = {"file": "hyprial-0.0.0-cp312-abi3-macosx_13_0_arm64.whl", "version": "0.0.0", "sha256": "c" * 64, "sourceCommit": COMMIT}
    manifest = stage_runtime.build_manifest(runtime, lock, "aarch64-apple-darwin", facts)
    _write(runtime / stage_runtime.MANIFEST_NAME, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return app


def make_candidate(root: Path, app: Path) -> Path:
    candidate = root / "candidate"
    _write(candidate / DMG, b"fake dmg holding " + app.name.encode())
    shutil.copyfile(app / "Contents" / "Resources" / "runtime" / "runtime-manifest.json", candidate / "runtime-manifest.json")
    sums = "".join(f"{hashlib.sha256((candidate / name).read_bytes()).hexdigest()}  {name}\n" for name in (DMG, "runtime-manifest.json"))
    (candidate / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return candidate


class Tools:
    """Records each call and imitates the behaviour of the macOS tools the flow uses."""

    def __init__(self, source_app: Path, store: Path, **behaviour) -> None:
        self.source_app = source_app
        self.store = store
        self.calls: list[dict] = []
        self.images: dict[str, Path] = {}
        self.signed: dict[str, list[str]] = {}
        self.fail_on = behaviour.get("fail_on")
        self.timeout_on = behaviour.get("timeout_on")
        self.missing = behaviour.get("missing")
        self.notary_status = behaviour.get("notary_status", "Accepted")
        self.leaf = behaviour.get("leaf", LEAF)
        self.on_call = behaviour.get("on_call")

    @staticmethod
    def name(argv: list[str]) -> str:
        tool = Path(argv[0]).name
        if tool == "codesign":
            if "--verify" in argv:
                return "codesign-verify"
            if any(arg.startswith("--extract-certificates") for arg in argv):
                return "codesign-leaf"
            if argv[1] == "-d":
                return "codesign-show"
            return "codesign-sign"
        if tool == "xcrun":
            return f"{argv[1]}-{argv[2]}"
        if tool == "hdiutil":
            return f"hdiutil-{argv[1]}"
        if tool == "ditto":
            return "ditto-zip" if "-c" in argv else "ditto"
        return tool

    def __call__(self, argv, *, env=None, timeout=None, **_kwargs) -> subprocess.CompletedProcess:
        argv = [str(arg) for arg in argv]
        name = self.name(argv)
        self.calls.append({"name": name, "argv": argv, "env": dict(env or {}), "timeout": timeout})
        if self.on_call:
            self.on_call(name, argv)
        if name == self.missing:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        if name == self.timeout_on:
            raise subprocess.TimeoutExpired(argv, timeout or 0)
        if name == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, f"Authority={COMMON_NAME}\n", f"{name} failed for {COMMON_NAME} {SHA1} {SHA1.lower()} TeamIdentifier={TEAM}\n")
        stdout, stderr = "", ""
        if name == "hdiutil-attach":
            mount = Path(argv[argv.index("-mountpoint") + 1])
            image = argv[-1]
            source = self.images.get(image, self.source_app.parent)
            for entry in source.iterdir():
                target = mount / entry.name
                if entry.is_symlink():
                    target.symlink_to(entry.readlink())
                else:
                    shutil.copytree(entry, target, symlinks=True)
        elif name == "hdiutil-detach":
            mount = Path(argv[2])
            for entry in list(mount.iterdir()):
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
        elif name == "hdiutil-create":
            source = Path(argv[argv.index("-srcfolder") + 1])
            kept = self.store / f"image-{len(self.images)}"
            shutil.copytree(source, kept, symlinks=True)
            self.images[argv[-1]] = kept
            Path(argv[-1]).write_bytes(b"dmg built from " + source.name.encode())
        elif name == "ditto":
            shutil.copytree(argv[1], argv[2], symlinks=True)
        elif name == "ditto-zip":
            Path(argv[-1]).write_bytes(b"zip")
        elif name == "codesign-sign":
            target = Path(argv[-1])
            self.signed[str(target)] = argv
            if target.is_dir():
                _write(target / "Contents" / "_CodeSignature" / "CodeResources", b"sealed " + str(len(self.calls)).encode())
            else:
                with target.open("ab") as stream:
                    stream.write(b"\x00signature")
        elif name == "codesign-leaf":
            prefix = next(arg for arg in argv if arg.startswith("--extract-certificates=")).split("=", 1)[1]
            Path(prefix + "0").write_bytes(self.leaf)
            Path(prefix + "1").write_bytes(b"intermediate")
        elif name == "codesign-show":
            signed = self.signed.get(argv[-1], [])
            adhoc = signed[signed.index("--sign") + 1] == "-" if signed else True
            flags = ["adhoc"] if adhoc else []
            if "runtime" in signed:
                flags.append("runtime")
            stderr = f"Executable={argv[-1]}\nIdentifier=com.hyprial.test\nCodeDirectory v=20500 size=9 flags=0x{(2 if adhoc else 0) | (0x10000 if 'runtime' in signed else 0):x}({','.join(flags) or 'none'}) hashes=1+2 location=embedded\n"
            if adhoc:
                stderr += "Signature=adhoc\nTeamIdentifier=not set\n"
            else:
                stderr += f"Signature size=9000\nAuthority={COMMON_NAME}\n"
                if "--timestamp" in signed:
                    stderr += "Timestamp=17 Nov 2026 at 12:00:00\n"
                stderr += f"TeamIdentifier={TEAM}\n"
        elif name == "codesign-verify":
            stderr = f"{argv[-1]}: valid on disk\n{argv[-1]}: satisfies its Designated Requirement\n"
        elif name == "notarytool-submit":
            stdout = json.dumps({"id": "11111111-2222-3333-4444-555555555555", "status": self.notary_status, "message": f"Processing complete for {COMMON_NAME}"})
        elif name.startswith("stapler-"):
            stdout = "Processing: file\nThe staple and validate action worked!\n"
        elif name == "spctl":
            stderr = f"{argv[-1]}: accepted\nsource=Notarized Developer ID\norigin={COMMON_NAME}\n"
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)

    def names(self) -> list[str]:
        return [call["name"] for call in self.calls]

    def of(self, name: str) -> list[list[str]]:
        return [call["argv"] for call in self.calls if call["name"] == name]


class Checks:
    """Stand-ins for verify_runtime.run and smoke_bundle.run that record what they saw."""

    def __init__(self, tools: Tools, *, verify_code: int = 0, smoke_code: int = 0) -> None:
        self.tools = tools
        self.verify_code = verify_code
        self.smoke_code = smoke_code
        self.seen: list[dict] = []

    def verify(self, runtime: Path, root_parent: Path):
        problems, _ = verify_runtime.check_manifest(runtime)
        self.seen.append({"check": "verify", "at": len(self.tools.calls), "manifest_problems": problems, "root_parent": root_parent})
        line = "PASS" if self.verify_code == 0 else f"FAIL version {COMMON_NAME}"
        return self.verify_code, line, {"steps": [{"name": "version", "status": "PASS" if self.verify_code == 0 else "FAIL", "detail": line}]}

    def smoke(self, app: Path, root_parent: Path):
        self.seen.append({"check": "smoke", "at": len(self.tools.calls), "app": app})
        line = "PASS" if self.smoke_code == 0 else "FAIL launch"
        return self.smoke_code, line, {"steps": [{"name": "launch", "status": "PASS" if self.smoke_code == 0 else "FAIL", "detail": line}]}


@pytest.fixture
def world(tmp_path: Path) -> dict:
    app = make_app(tmp_path / "source")
    candidate = make_candidate(tmp_path, app)
    store = tmp_path / "store"
    store.mkdir()
    out_parent = tmp_path / "release"
    out_parent.mkdir()
    temp = tmp_path / "t"
    temp.mkdir()
    home = tmp_path / "operator-home"
    home.mkdir()
    return {"app": app, "candidate": candidate, "store": store, "out": out_parent / "HyprialOS-0.2.0", "temp": temp, "home": home, "tmp": tmp_path}


def _options(world: dict, **overrides):
    values = {
        "candidate": world["candidate"],
        "source_commit": COMMIT,
        "out": world["out"],
        "identity": SHA1,
        "notary_profile": PROFILE,
        "dry_run": False,
        "home": world["home"],
    }
    values.update(overrides)
    return sign_release.Options(**values)


def _release(world: dict, *, tools: Tools | None = None, checks: Checks | None = None, snapshot=None, **overrides):
    tools = tools or Tools(world["app"], world["store"])
    checks = checks or Checks(tools)
    code, evidence = sign_release.run_release(
        _options(world, **overrides),
        run=tools,
        verify=checks.verify,
        smoke=checks.smoke,
        snapshot=snapshot or (lambda: dict(SNAPSHOT)),
        temp_parent=world["temp"],
    )
    return code, evidence, tools, checks


def _tree(path: Path) -> dict[str, str]:
    return {item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest() for item in sorted(path.rglob("*")) if item.is_file()}


def _unfinished(world: dict) -> Path:
    return world["out"].with_name(world["out"].name + ".unfinished")


def test_mach_o_files_are_found_by_magic_number_not_by_extension(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    found = sign_release.macho_files(app)
    relative = [path.relative_to(app / "Contents" / "Resources" / "runtime").as_posix() for path in found if "runtime" in path.parts]
    assert set(relative) == RUNTIME_MACHOS
    assert app / "Contents" / "MacOS" / "hyprial-desktop" in found
    depths = [len(path.parts) for path in found]
    assert depths == sorted(depths, reverse=True)


def test_a_fat_header_is_told_apart_from_a_java_class(tmp_path: Path) -> None:
    cases = {"fat": FAT + b"x", "java": JAVA_CLASS + b"x", "thin": MACHO, "be": struct.pack(">I", 0xFEEDFACF) + b"xxxx", "text": b"#!/bin/sh\n", "short": b"\xca\xfe"}
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    assert {name for name in cases if sign_release.is_macho(tmp_path / name)} == {"fat", "thin", "be"}


def test_a_release_signs_inside_out_notarizes_staples_and_moves_the_product_at_the_end(world: dict) -> None:
    code, evidence, tools, _checks = _release(world)
    assert code == 0, evidence
    assert evidence["verdict"] == "PASS"
    assert sorted(path.name for path in world["out"].iterdir()) == sorted([DMG, "SHA256SUMS", "runtime-manifest.json"])
    assert not _unfinished(world).exists()
    sums = (world["out"] / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums == [f"{hashlib.sha256((world['out'] / name).read_bytes()).hexdigest()}  {name}" for name in (DMG, "runtime-manifest.json")]
    names = tools.names()
    assert names.count("notarytool-submit") == 2
    assert names.count("stapler-staple") == 2 and names.count("stapler-validate") >= 2
    assert names.count("spctl") == 2
    assert tools.of("hdiutil-attach")[0][-1] == str(world["candidate"] / DMG)
    assert len(tools.of("hdiutil-detach")) == len(tools.of("hdiutil-attach"))
    statuses = {step["step"]: step["status"] for step in evidence["steps"]}
    assert set(statuses.values()) == {"PASS"}


def test_signing_is_deepest_first_never_deep_hardened_and_timestamped(world: dict) -> None:
    _code, _evidence, tools, _checks = _release(world)
    signs = tools.of("codesign-sign")
    targets = [Path(argv[-1]) for argv in signs]
    work = _unfinished(world)
    app = work / "app" / "HyprialOS.app"
    inner = [target for target in targets if target.is_relative_to(app) and target != app]
    assert {target.relative_to(app / "Contents" / "Resources" / "runtime").as_posix() for target in inner} == RUNTIME_MACHOS
    assert [len(target.parts) for target in inner] == sorted((len(target.parts) for target in inner), reverse=True)
    assert targets[len(inner)] == app
    assert targets[len(inner) + 1].name == DMG
    for argv in signs:
        assert "--deep" not in argv
        assert argv[argv.index("--sign") + 1] == SHA1
        assert "--timestamp" in argv and "--timestamp=none" not in argv
        assert "--entitlements" not in argv
    for argv in signs[: len(inner) + 1]:
        assert argv[argv.index("--options") + 1] == "runtime"
    deep_verifies = [argv for argv in tools.of("codesign-verify") if "--deep" in argv]
    assert deep_verifies and all("--strict" in argv for argv in deep_verifies)


def test_the_manifest_is_checked_before_signing_and_regenerated_before_the_app_signature(world: dict) -> None:
    order: list[str] = []
    app_path: list[Path] = []

    def observe(name: str, argv: list[str]) -> None:
        if name == "codesign-sign":
            target = Path(argv[-1])
            if target.name == "HyprialOS.app":
                app_path.append(target)
                problems, _ = verify_runtime.check_manifest(target / "Contents" / "Resources" / "runtime")
                order.append(f"app-sign manifest-problems={len(problems)}")
            else:
                order.append("inner-sign")

    tools = Tools(world["app"], world["store"], on_call=observe)
    code, evidence, _tools, checks = _release(world, tools=tools)
    assert code == 0, evidence
    assert order[-2] == "app-sign manifest-problems=0"
    assert "inner-sign" in order[: order.index("app-sign manifest-problems=0")]
    manifest = evidence["manifest"]
    assert manifest["before_sha256"] == hashlib.sha256((world["candidate"] / "runtime-manifest.json").read_bytes()).hexdigest()
    assert manifest["after_sha256"] == hashlib.sha256((world["out"] / "runtime-manifest.json").read_bytes()).hexdigest()
    assert manifest["before_sha256"] != manifest["after_sha256"]
    assert manifest["changed_files"] == len(RUNTIME_MACHOS)
    verify_seen = next(seen for seen in checks.seen if seen["check"] == "verify")
    assert verify_seen["manifest_problems"] == []
    assert verify_seen["at"] > tools.names().index("codesign-sign")


def test_only_mach_o_hashes_change_in_the_regenerated_manifest(world: dict) -> None:
    code, _evidence, _tools, _checks = _release(world)
    assert code == 0
    before = json.loads((world["candidate"] / "runtime-manifest.json").read_text(encoding="utf-8"))
    after = json.loads((world["out"] / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert {key: value for key, value in before.items() if key != "files"} == {key: value for key, value in after.items() if key != "files"}
    assert set(before["files"]) == set(after["files"])
    assert {name for name in before["files"] if before["files"][name] != after["files"][name]} == RUNTIME_MACHOS
    assert all(before["files"][name]["mode"] == after["files"][name]["mode"] for name in before["files"])


def test_a_tampered_runtime_is_refused_before_any_signature(world: dict) -> None:
    runtime = world["app"] / "Contents" / "Resources" / "runtime"
    (runtime / "lib" / "python3.12" / "site-packages" / "pkg" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    code, evidence, tools, _checks = _release(world)
    assert code == 1
    assert "codesign-sign" not in tools.names()
    assert any("manifest" in problem for problem in evidence["problems"])
    assert not world["out"].exists() and not _unfinished(world).exists()


def test_the_bundled_manifest_must_be_the_one_in_sha256sums(world: dict) -> None:
    runtime = world["app"] / "Contents" / "Resources" / "runtime"
    manifest = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    manifest["pbsTag"] = "20990101"
    (runtime / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    code, evidence, tools, _checks = _release(world)
    assert code == 1
    assert "codesign-sign" not in tools.names()
    assert any("runtime-manifest.json" in problem for problem in evidence["problems"])


def test_notarization_uses_only_the_keychain_profile_with_a_timeout(world: dict) -> None:
    _code, _evidence, tools, _checks = _release(world)
    submits = tools.of("notarytool-submit")
    for argv in submits:
        assert argv[:3] == [sign_release.XCRUN, "notarytool", "submit"]
        assert argv[argv.index("--keychain-profile") + 1] == PROFILE
        assert "--wait" in argv
        assert argv[argv.index("--timeout") + 1] == f"{sign_release.NOTARY_TIMEOUT_MINUTES}m"
        assert argv[argv.index("--output-format") + 1] == "json"
    notary_call = next(call for call in tools.calls if call["name"] == "notarytool-submit")
    assert notary_call["timeout"] > sign_release.NOTARY_TIMEOUT_MINUTES * 60
    assert Path(submits[0][3]).suffix == ".zip" and Path(submits[1][3]).name == DMG
    for call in tools.calls:
        for forbidden in ("--password", "--key", "--key-id", "--issuer", "--apple-id", "--team-id", "-p"):
            assert forbidden not in call["argv"][1:], call["name"]


def test_final_checks_are_deep_strict_verify_stapler_validate_and_gatekeeper(world: dict) -> None:
    _code, _evidence, tools, _checks = _release(world)
    spctl = tools.of("spctl")
    assert spctl[0][1:5] == ["--assess", "--type", "execute", "--verbose=2"]
    assert spctl[1][1:7] == ["--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2"]
    assert spctl[1][-1].endswith(DMG)
    last_verify = tools.names().index("spctl")
    assert "codesign-verify" in tools.names()[:last_verify]


def test_an_invalid_notarization_fails_and_leaves_nothing_that_looks_finished(world: dict) -> None:
    code, evidence, _tools, _checks = _release(world, tools=Tools(world["app"], world["store"], notary_status="Invalid"))
    assert code == 1
    assert evidence["verdict"] == "FAIL"
    assert not world["out"].exists() and not _unfinished(world).exists()


@pytest.mark.parametrize("failing", ["codesign-sign", "codesign-verify", "notarytool-submit", "stapler-staple", "stapler-validate", "spctl", "hdiutil-create", "ditto"])
def test_a_failing_tool_leaves_no_product_and_no_mount(world: dict, failing: str) -> None:
    code, evidence, tools, _checks = _release(world, tools=Tools(world["app"], world["store"], fail_on=failing))
    assert code == 1
    assert evidence["verdict"] == "FAIL"
    assert not world["out"].exists() and not _unfinished(world).exists()
    assert len(tools.of("hdiutil-detach")) == len(tools.of("hdiutil-attach"))
    assert any(failing.split("-")[0] in problem for problem in evidence["problems"])


@pytest.mark.parametrize("which", ["verify", "smoke"])
def test_a_failing_verify_runtime_or_smoke_on_the_signed_bundle_fails(world: dict, which: str) -> None:
    tools = Tools(world["app"], world["store"])
    checks = Checks(tools, verify_code=1 if which == "verify" else 0, smoke_code=1 if which == "smoke" else 0)
    code, evidence, _tools, _checks = _release(world, tools=tools, checks=checks)
    assert code == 1
    assert "notarytool-submit" not in tools.names()
    assert not world["out"].exists() and not _unfinished(world).exists()
    assert any(which in problem for problem in evidence["problems"])


def test_a_missing_tool_or_a_timeout_fails_without_a_traceback(world: dict) -> None:
    code, evidence, _tools, _checks = _release(world, tools=Tools(world["app"], world["store"], missing="hdiutil-attach"))
    assert code == 1 and any("hdiutil" in problem for problem in evidence["problems"])
    assert not _unfinished(world).exists()
    code, evidence, _tools, _checks = _release(world, tools=Tools(world["app"], world["store"], timeout_on="notarytool-submit"))
    assert code == 1 and any("timeout" in problem for problem in evidence["problems"])
    assert not world["out"].exists() and not _unfinished(world).exists()


def test_a_signal_midway_still_cleans_up_and_signals_are_ignored_during_cleanup(world: dict) -> None:
    during_cleanup: list[object] = []

    def interrupt(name: str, argv: list[str]) -> None:
        if name == "ditto":
            raise sign_release.StepFailed("interrupted by signal 15")
        if name == "hdiutil-detach":
            during_cleanup.append(signal.getsignal(signal.SIGTERM))

    previous = signal.getsignal(signal.SIGTERM)
    code, evidence, _tools, _checks = _release(world, tools=Tools(world["app"], world["store"], on_call=interrupt))
    assert code == 1 and "interrupted by signal 15" in evidence["problems"][0]
    assert during_cleanup == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGTERM) == previous
    assert not _unfinished(world).exists()


def test_the_input_candidate_is_never_modified(world: dict) -> None:
    before = _tree(world["candidate"])
    source_before = _tree(world["app"])
    code, _evidence, _tools, _checks = _release(world)
    assert code == 0
    assert _tree(world["candidate"]) == before
    assert _tree(world["app"]) == source_before


def test_a_checksum_or_commit_mismatch_is_refused_before_anything_runs(world: dict) -> None:
    (world["candidate"] / DMG).write_bytes(b"swapped dmg")
    with pytest.raises(sign_release.Refused, match="SHA256SUMS"):
        _release(world)
    make_candidate(world["tmp"], world["app"])
    with pytest.raises(sign_release.Refused, match="source commit"):
        _release(world, source_commit="b" * 40)
    (world["candidate"] / "SHA256SUMS").unlink()
    with pytest.raises(sign_release.Refused, match="SHA256SUMS"):
        _release(world)
    assert not world["out"].exists() and not _unfinished(world).exists()


def test_an_existing_output_or_unfinished_directory_is_refused(world: dict) -> None:
    world["out"].mkdir()
    with pytest.raises(sign_release.Refused, match="already exists"):
        _release(world)
    world["out"].rmdir()
    _unfinished(world).mkdir()
    with pytest.raises(sign_release.Refused, match="unfinished"):
        _release(world)


def test_the_identity_is_a_sha1_and_ad_hoc_only_in_a_dry_run(world: dict) -> None:
    for identity in ("Developer ID Application: Fake Person", "ABC", "g" * 40):
        with pytest.raises(sign_release.Refused, match="SHA-1"):
            _release(world, identity=identity)
    with pytest.raises(sign_release.Refused, match="dry run"):
        _release(world, identity="-")
    with pytest.raises(sign_release.Refused, match="profile"):
        _release(world, notary_profile=None)
    code, _evidence, _tools, _checks = _release(world, identity=SHA1.lower())
    assert code == 0


def test_another_leaf_on_the_signature_fails(world: dict) -> None:
    code, evidence, tools, _checks = _release(world, tools=Tools(world["app"], world["store"], leaf=b"someone else"))
    assert code == 1
    assert "notarytool-submit" not in tools.names()
    assert any("leaf" in problem for problem in evidence["problems"])


def test_the_evidence_and_output_never_carry_the_identity(world: dict, capsys: pytest.CaptureFixture[str]) -> None:
    for tools in (Tools(world["app"], world["store"]), Tools(world["app"], world["store"], fail_on="spctl"), Tools(world["app"], world["store"], fail_on="codesign-sign")):
        shutil.rmtree(world["out"], ignore_errors=True)
        _code, evidence, _tools, _checks = _release(world, tools=tools)
        text = json.dumps(evidence)
        for secret in (SHA1, SHA1.lower(), TEAM, "Fake Person", "Developer ID Application", str(world["home"]), PROFILE):
            assert secret not in text, secret
        assert evidence["identity"]["marker"] == sign_release.identity_marker(SHA1)
    assert sign_release.identity_marker(SHA1) == sign_release.identity_marker(SHA1.lower())
    assert len(sign_release.identity_marker(SHA1)) == len("id-") + 8
    evidence_path = world["tmp"] / "evidence.json"
    tools = Tools(world["app"], world["store"], fail_on="spctl")
    checks = Checks(tools)
    code = sign_release.main(
        ["--candidate", str(world["candidate"]), "--source-commit", COMMIT, "--out", str(world["tmp"] / "cli-out"), "--identity", SHA1, "--notary-profile", PROFILE, "--evidence", str(evidence_path)],
        platform="darwin", home=world["home"], run=tools, verify=checks.verify, smoke=checks.smoke, snapshot=lambda: dict(SNAPSHOT), temp_parent=world["temp"],
    )
    printed = capsys.readouterr().out
    assert code == 1
    for secret in (SHA1, SHA1.lower(), TEAM, "Fake Person", "Developer ID Application"):
        assert secret not in printed and secret not in evidence_path.read_text(encoding="utf-8")


def test_the_redaction_removes_names_team_ids_hashes_and_paths() -> None:
    redact = sign_release.Redactor(SHA1, {"/Users/someone/work": "<work>", "/Users/someone": "~"})
    text = f"Authority={COMMON_NAME}\nTeamIdentifier={TEAM}\norigin={COMMON_NAME}\n{SHA1.lower()}: no identity found\n/Users/someone/work/app: ok\n/Users/someone/x"
    cleaned = redact(text)
    for secret in (TEAM, "Fake Person", SHA1.lower(), "/Users/someone"):
        assert secret not in cleaned
    assert sign_release.identity_marker(SHA1) in cleaned
    assert "<work>/app: ok" in cleaned and "~/x" in cleaned


def test_children_get_an_environment_built_from_nothing_with_the_operator_home(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLE_PASSWORD", "caller-secret-value")
    monkeypatch.setenv("APPLE_SIGNING_IDENTITY", "caller-identity")
    _code, _evidence, tools, _checks = _release(world)
    work = _unfinished(world)
    for call in tools.calls:
        assert call["env"] == {"HOME": str(world["home"]), "PATH": sign_release.CHILD_PATH, "LANG": "C", "TMPDIR": str(work / "tmp")}, call["name"]
        assert call["timeout"] is not None and call["timeout"] > 0


def test_a_dry_run_exercises_everything_but_notarization_and_reports_it_unknown(world: dict) -> None:
    code, evidence, tools, checks = _release(world, dry_run=True, notary_profile=None)
    assert code == 3
    assert evidence["verdict"] == "UNKNOWN" and evidence["dry_run"] is True
    for name in ("notarytool-submit", "stapler-staple", "stapler-validate", "spctl"):
        assert name not in tools.names()
    statuses = {step["step"]: step["status"] for step in evidence["steps"]}
    for step in ("notarize app", "staple app", "notarize dmg", "staple dmg", "gatekeeper app", "gatekeeper dmg"):
        assert statuses[step] == "UNKNOWN"
    assert "PASS" not in {statuses[step] for step in statuses if "notar" in step or "staple" in step or "gatekeeper" in step}
    assert {seen["check"] for seen in checks.seen} == {"verify", "smoke"}
    signs = tools.of("codesign-sign")
    assert all("--timestamp=none" in argv and "--timestamp" not in argv for argv in signs)
    assert not world["out"].exists() and not _unfinished(world).exists()


def test_only_a_dry_run_carries_the_library_validation_stand_in(world: dict) -> None:
    _code, _evidence, tools, _checks = _release(world, dry_run=True, identity="-", notary_profile=None)
    signs = tools.of("codesign-sign")
    app = _unfinished(world) / "app" / "HyprialOS.app"
    inner = [argv for argv in signs if Path(argv[-1]).is_relative_to(app) and Path(argv[-1]) != app]
    assert inner and all(argv[argv.index("--entitlements") + 1].endswith(sign_release.STAND_IN_NAME) for argv in inner)
    assert all("--entitlements" not in argv for argv in signs if argv not in inner)
    assert all(argv[argv.index("--sign") + 1] == "-" for argv in signs)
    assert plistlib.loads(sign_release.STAND_IN_ENTITLEMENTS) == {"com.apple.security.cs.disable-library-validation": True}


def test_a_changed_keychain_search_list_fails(world: dict) -> None:
    readings = iter([dict(SNAPSHOT), {**SNAPSHOT, "search_list": SNAPSHOT["search_list"] + '    "/somewhere/else.keychain-db"\n'}])
    code, evidence, _tools, _checks = _release(world, snapshot=lambda: next(readings))
    assert code == 1
    assert evidence["keychains"] == {"search_list_unchanged": False, "default_keychain_unchanged": True}
    assert not world["out"].exists()


def test_refuses_off_macos(world: dict, capsys: pytest.CaptureFixture[str]) -> None:
    code = sign_release.main(["--candidate", str(world["candidate"]), "--source-commit", COMMIT, "--out", str(world["out"]), "--dry-run", "--identity", "-", "--evidence", str(world["tmp"] / "e.json")], platform="linux")
    assert code == 2
    assert "macOS only" in capsys.readouterr().out


def test_identity_and_profile_can_come_from_the_environment(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sign_release.IDENTITY_ENV, SHA1)
    monkeypatch.setenv(sign_release.PROFILE_ENV, PROFILE)
    tools = Tools(world["app"], world["store"])
    checks = Checks(tools)
    code = sign_release.main(
        ["--candidate", str(world["candidate"]), "--source-commit", COMMIT, "--out", str(world["out"]), "--evidence", str(world["tmp"] / "e.json")],
        platform="darwin", home=world["home"], run=tools, verify=checks.verify, smoke=checks.smoke, snapshot=lambda: dict(SNAPSHOT), temp_parent=world["temp"],
    )
    assert code == 0
    assert tools.of("codesign-sign")[0][tools.of("codesign-sign")[0].index("--sign") + 1] == SHA1
