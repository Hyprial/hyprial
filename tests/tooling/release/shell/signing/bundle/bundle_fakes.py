"""Fake macOS tools and a synthetic HyprialOS.app for sign_bundle.py.

``sign_bundle.py`` takes every tool from ``/usr/bin:/bin:/usr/sbin:/sbin``
unless the test-only variable ``HYPRIAL_SIGN_BUNDLE_TOOL_PATH`` names a PATH
to use instead (the same pattern as ``identity_fakes``). ``Bench`` builds that
PATH from scratch: one fake written in Python stands in for ``codesign``,
``security``, ``xcrun`` (``notarytool`` and ``stapler``), ``hdiutil``,
``ditto``, ``spctl`` and ``uname``, so no tool of the machine running the
tests can leak in and the tests run on Linux too.

The fake appends one JSON line per call to ``<state>/calls.jsonl`` (tool and
argv). Signing changes bytes the way the real tool does: a signed file gets a
trailer, a signed directory gets ``Contents/_CodeSignature/CodeResources``;
``<state>/signed.json`` remembers how each path was signed so ``codesign -dvv``
and ``--extract-certificates`` can report it. A signed directory's call also
records the SHA-256 of its runtime manifest at that moment. The tool gives
its children an environment built from nothing, so the launchers carry the
state directory themselves and the behaviour knobs live in
``<state>/knobs.json`` (``Bench.knob``): ``FAKE_IDENTITIES`` (JSON list of
``[sha1, common name, leaf text]``), ``FAKE_NOTARY`` (comma list of statuses
that ``notarytool wait`` reports, one per submission; ``submit`` answers with
a new id at once and appends ``<id> <file name>`` to ``<state>/submitted.txt``,
which ``notarytool history`` lists; ``FAKE_SUBMIT_HANG`` makes the submit
touch ``<state>/submit-started`` and sleep like a long upload,
``FAKE_SUBMIT_SECONDS`` delays the reply; ``timeout``
imitates a wait that ran out, ``hang`` touches ``<state>/hang-started`` and
sleeps),
``FAKE_NOTARY_EXIT`` (exit code that goes with an Accepted reply), ``FAKE_FAIL``
(space list of call names that fail; ``<name>@dmg`` fails only on a ``.dmg``
target, ``<name>@mnt`` only under the mount point, ``<name>@final`` only under
a ``final`` directory (the copy of ``--extract-to``), ``hdiutil-detach-force``
the forced detach), ``FAKE_SPCTL_SOURCE``, ``FAKE_SPCTL_EXIT`` (exit code that
goes with an accepted verdict), ``FAKE_SPCTL_VERDICT`` (the word after the
path, default ``accepted``), ``FAKE_UNAME_S`` and ``FAKE_UNAME_M``.

The signature audit knobs change only what ``codesign -dvv`` and
``--extract-certificates`` report, never what ``security`` lists or what
signing does: ``FAKE_TEAM_FOR`` (JSON object, path suffix -> Team ID),
``FAKE_NO_RUNTIME_FOR`` (path suffix shown without the runtime flag),
``FAKE_AUTHORITY`` (the first ``Authority=`` line), ``FAKE_CHAIN`` (JSON list,
the ``Authority=`` lines after it), ``FAKE_LEAF`` and ``FAKE_ROOT`` (bytes of
the extracted leaf and root certificate), ``FAKE_EXTRA_CERT`` (a fourth
extracted certificate), ``FAKE_ADHOC_FOR`` (path suffix whose report also
carries ``Signature=adhoc``). ``xattr`` only records its call. A signed directory's seal carries
the digest of its tree (without ``_CodeSignature`` and the stapled ticket),
so ``codesign --verify`` of any copy of it, the mounted one too, fails once a
file changed; modes are not part of the seal. ``FAKE_TAMPER`` changes the app
after its staple (``app-content``: a resource outside the runtime,
``app-mode``: the mode of a runtime file) or only the dmg's copy
(``image-content``, ``image-mode``). ``hdiutil attach`` copies what ``create``
saw to the mount point and ``detach`` removes it.
A path signed inside ``<out>.unfinished`` is also known under ``<out>``
after the final rename (``--verify-only`` checks the finished directory);
``notarytool history`` lists the submissions of this bench unless it is
listed in ``FAKE_FAIL``.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import struct
import sys
import zipfile
from pathlib import Path

from tests.tooling.release.shell.support import load

stage_runtime = load("stage_runtime")

TEAM = "ABCDE12345"
PROFILE = "hyprial-notary-test"
COMMIT = "a" * 40
VERSION = "0.2.0"
LEAF = "fake leaf certificate one"
SHA1 = hashlib.sha1(LEAF.encode()).hexdigest().upper()
OTHER_LEAF = "fake leaf certificate two"
OTHER_SHA1 = hashlib.sha1(OTHER_LEAF.encode()).hexdigest().upper()
DEVID = f"Developer ID Application: Example Developer ({TEAM})"
APPLE_DEV = "Apple Development: Example Developer (ZZZZZ99999)"
MACHO = struct.pack("<I", 0xFEEDFACF) + b"\x0c\x00\x00\x01"
FAT = struct.pack(">II", 0xCAFEBABE, 2)
FAT64 = struct.pack(">II", 0xCAFEBABF, 1)
JAVA_CLASS = struct.pack(">IHH", 0xCAFEBABE, 0, 52)
RUNTIME_MACHOS = {
    "bin/python3.12",
    "lib/libpython3.12.dylib",
    "lib/python3.12/site-packages/pkg/_ext.cpython-312-darwin.so",
    "lib/python3.12/site-packages/pkg/deep/deeper/_x.so",
    "lib/python3.12/site-packages/pkg/noext_binary",
}
FAKE_NAMES = ("codesign", "security", "xcrun", "hdiutil", "ditto", "spctl", "xattr", "uname")
ROOT_CERT = b"fake apple root certificate"

FAKE_TOOL = r'''
import hashlib, json, os, shutil, sys, time, uuid, zipfile
from pathlib import Path

TOOL = os.environ["FAKE_TOOL_NAME"]
ARGS = sys.argv[1:]
STATE = Path(os.environ["FAKE_STATE"])
KNOBS = json.loads((STATE / "knobs.json").read_text()) if (STATE / "knobs.json").exists() else {}


def knob(name, default=""):
    return KNOBS.get(name, default)


IDENTITIES = json.loads(knob("FAKE_IDENTITIES", "[]"))
TEAM = knob("FAKE_TEAM", "ABCDE12345")


def log(entry):
    with open(STATE / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def failed(name, target=""):
    wanted = knob("FAKE_FAIL").split()
    if name in wanted:
        return True
    if target.endswith(".dmg") and name + "@dmg" in wanted:
        return True
    if "/final/" in target and name + "@final" in wanted:
        return True
    return "/mnt/" in target and name + "@mnt" in wanted


def sealed_hash(root):
    digest = hashlib.sha256()
    for item in sorted(Path(root).rglob("*")):
        relative = item.relative_to(root).as_posix()
        if "_CodeSignature" in item.parts or relative == "Contents/CodeResources" or (item.is_dir() and not item.is_symlink()):
            continue
        digest.update(relative.encode() + b"\0")
        digest.update(("link:" + os.readlink(item)).encode() if item.is_symlink() else item.read_bytes())
    return digest.hexdigest()


def tamper(app, how):
    if how.endswith("content"):
        with open(Path(app) / "Contents" / "Resources" / "icon.icns", "ab") as stream:
            stream.write(b"tampered")
    elif how.endswith("mode"):
        (Path(app) / "Contents" / "Resources" / "runtime" / "lib" / "python3.12" / "site-packages" / "pkg" / "__init__.py").chmod(0o600)


def option(name, default=None):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


def signed():
    path = STATE / "signed.json"
    return json.loads(path.read_text()) if path.exists() else {}


def remember(target, record):
    model = signed()
    model[target] = record
    (STATE / "signed.json").write_text(json.dumps(model))


def record_of(target):
    model = signed()
    if target in model:
        return model[target]
    return next((value for key, value in model.items() if key.replace(".unfinished/", "/") == target), None)


def manifest_sha(target):
    manifest = Path(target) / "Contents" / "Resources" / "runtime" / "runtime-manifest.json"
    return hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else None


def codesign():
    target = ARGS[-1]
    if "--sign" in ARGS:
        identity = option("--sign")
        entry = {"tool": "codesign", "name": "codesign-sign", "argv": ARGS}
        if Path(target).is_dir():
            entry["manifest_sha256"] = manifest_sha(target)
        log(entry)
        if failed("codesign-sign"):
            print(target + ": errSecInternalComponent", file=sys.stderr)
            return 1
        known = [item for item in IDENTITIES if item[0].upper() == identity.upper()]
        if not known:
            print("error: The specified item could not be found in the keychain.", file=sys.stderr)
            return 1
        path = Path(target)
        if path.is_dir():
            seal = path / "Contents" / "_CodeSignature" / "CodeResources"
            seal.parent.mkdir(parents=True, exist_ok=True)
            seal.write_text("sealed by " + identity + " " + sealed_hash(path))
        else:
            with path.open("ab") as stream:
                stream.write(b"\x00fake-signature")
        remember(str(path), {"identity": identity.upper(), "runtime": option("--options") == "runtime", "timestamp": "--timestamp" in ARGS, "name": known[0][1], "leaf": known[0][2]})
        return 0
    if "--verify" in ARGS:
        log({"tool": "codesign", "name": "codesign-verify", "argv": ARGS})
        path = Path(target)
        seal = path / "Contents" / "_CodeSignature" / "CodeResources"
        if path.is_dir():
            valid = seal.is_file() and seal.read_text().split()[-1] == sealed_hash(path)
        else:
            valid = record_of(target) is not None
        if failed("codesign-verify", target) or not valid:
            print(target + ": a sealed resource is missing or invalid", file=sys.stderr)
            return 1
        print(target + ": valid on disk\n" + target + ": satisfies its Designated Requirement", file=sys.stderr)
        return 0
    extract = [arg for arg in ARGS if arg.startswith("--extract-certificates=")]
    record = record_of(target)
    if extract:
        log({"tool": "codesign", "name": "codesign-leaf", "argv": ARGS})
        if record is None:
            return 1
        prefix = extract[0].split("=", 1)[1]
        Path(prefix + "0").write_bytes(knob("FAKE_LEAF", record["leaf"]).encode())
        Path(prefix + "1").write_bytes(b"intermediate")
        Path(prefix + "2").write_bytes(knob("FAKE_ROOT", "fake apple root certificate").encode())
        if knob("FAKE_EXTRA_CERT"):
            Path(prefix + "3").write_bytes(b"a fourth certificate")
        return 0
    log({"tool": "codesign", "name": "codesign-show", "argv": ARGS})
    if record is None:
        print("Executable=" + target + "\nIdentifier=x\nCodeDirectory v=20400 size=1 flags=0x20002(adhoc,linker-signed) hashes=1+0 location=embedded\nSignature=adhoc\nTeamIdentifier=not set", file=sys.stderr)
        return 0
    stripped = knob("FAKE_NO_RUNTIME_FOR")
    flags = "0x10000(runtime)" if record["runtime"] and not (stripped and target.endswith(stripped)) else "0x0(none)"
    chain = json.loads(knob("FAKE_CHAIN", '["Developer ID Certification Authority", "Apple Root CA"]'))
    lines = ["Executable=" + target, "Identifier=x", "CodeDirectory v=20500 size=1 flags=" + flags + " hashes=1+2 location=embedded", "Signature size=9000", "Authority=" + knob("FAKE_AUTHORITY", record["name"])] + ["Authority=" + name for name in chain]
    if record["timestamp"] and not failed("timestamp"):
        lines.append("Timestamp=19 Nov 2026 at 12:00:00")
    team = next((value for suffix, value in json.loads(knob("FAKE_TEAM_FOR", "{}")).items() if target.endswith(suffix)), TEAM)
    lines.append("TeamIdentifier=" + team)
    adhoc = knob("FAKE_ADHOC_FOR")
    if adhoc and target.endswith(adhoc):
        lines.append("Signature=adhoc")
    print("\n".join(lines), file=sys.stderr)
    return 0


def security():
    log({"tool": "security", "name": "security", "argv": ARGS})
    if ARGS[:1] != ["find-identity"]:
        return 2
    rows = [(item[0], item[1]) for item in IDENTITIES] + [("C" * 40, "Apple Development: Example Developer (ZZZZZ99999)")]
    print("\nPolicy: Code Signing\n  Matching identities")
    for number, (sha, name) in enumerate(rows, 1):
        print("  " + str(number) + ") " + sha + ' "' + name + '"')
    print("     " + str(len(rows)) + " valid identities found")
    return 0


def notary_status():
    counter = STATE / "notary-count"
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    statuses = knob("FAKE_NOTARY", "Accepted,Accepted").split(",")
    return statuses[count] if count < len(statuses) else "Accepted"


def xcrun():
    name = ARGS[0] + "-" + ARGS[1]
    log({"tool": "xcrun", "name": name, "argv": ARGS})
    if failed(name, ARGS[-1]):
        print("Error: " + name + " failed", file=sys.stderr)
        return 1
    if name == "notarytool-submit":
        job = str(uuid.uuid4())
        with open(STATE / "submitted.txt", "a", encoding="utf-8") as handle:
            handle.write(job + " " + Path(ARGS[2]).name + "\n")
        if knob("FAKE_SUBMIT_SECONDS"):
            time.sleep(float(knob("FAKE_SUBMIT_SECONDS")))
        if knob("FAKE_SUBMIT_HANG"):
            (STATE / "submit-started").write_text("")
            time.sleep(120)
            return 1
        print(json.dumps({"id": job, "message": "Successfully uploaded file", "path": ARGS[2]}))
        return 0
    if name == "notarytool-wait":
        status = notary_status()
        job = ARGS[2]
        if status == "hang":
            print("Waiting for processing to complete.", file=sys.stderr, flush=True)
            (STATE / "hang-id").write_text(job)
            (STATE / "hang-started").write_text("")
            time.sleep(120)
            return 124
        if status == "timeout":
            (STATE / "timeout-id").write_text(job)
            print("Current status: In Progress....Error: Timeout of 30m reached.", file=sys.stderr)
            return 124
        print(json.dumps({"id": job, "status": status, "message": "Processing complete"}))
        return int(knob("FAKE_NOTARY_EXIT", "0"))
    if name == "notarytool-history":
        listed = (STATE / "submitted.txt").read_text().split("\n") if (STATE / "submitted.txt").exists() else []
        entries = [{"id": line.split(" ", 1)[0], "name": line.split(" ", 1)[1], "status": "In Progress", "createdDate": "2026-11-19T00:00:00.000Z"} for line in listed if line]
        print(json.dumps({"history": entries[::-1], "message": "Successfully received submission history."}))
        return 0
    if name == "notarytool-log":
        Path(ARGS[-1]).write_text(json.dumps({"jobId": ARGS[2], "status": "fake", "issues": [{"path": "x", "message": "The signature does not include a secure timestamp."}]}))
        return 0
    if name == "stapler-staple":
        target = Path(ARGS[-1])
        if target.is_dir():
            (target / "Contents" / "CodeResources").write_text("ticket")
            if knob("FAKE_TAMPER").startswith("app-"):
                tamper(target, knob("FAKE_TAMPER"))
        else:
            with target.open("ab") as stream:
                stream.write(b"ticket")
        print("The staple and validate action worked!")
        return 0
    if name == "stapler-validate":
        print("The validate action worked!")
        return 0
    return 2


def hdiutil():
    log({"tool": "hdiutil", "name": "hdiutil-" + ARGS[0], "argv": ARGS})
    if failed("hdiutil-detach-force" if ARGS[0] == "detach" and "-force" in ARGS else "hdiutil-" + ARGS[0], ARGS[-1]):
        print("hdiutil: " + ARGS[0] + " failed - Resource busy", file=sys.stderr)
        return 16
    if ARGS[0] == "create":
        source = Path(option("-srcfolder"))
        out = Path(ARGS[-1])
        images = STATE / "images"
        images.mkdir(exist_ok=True)
        shutil.copytree(source, images / out.name, symlinks=True)
        if knob("FAKE_TAMPER").startswith("image-"):
            tamper(images / out.name / "HyprialOS.app", knob("FAKE_TAMPER"))
        out.write_bytes(b"fake dmg of " + source.name.encode())
        return 0
    if ARGS[0] == "attach":
        shutil.copytree(STATE / "images" / Path(ARGS[-1]).name, option("-mountpoint"), symlinks=True, dirs_exist_ok=True)
        return 0
    if ARGS[0] == "detach":
        shutil.rmtree(ARGS[-1])
        return 0
    return 2


def ditto():
    log({"tool": "ditto", "name": "ditto-zip" if "-c" in ARGS else "ditto", "argv": ARGS})
    if failed("ditto"):
        return 1
    if "-c" in ARGS:
        source, out = Path(ARGS[-2]), Path(ARGS[-1])
        with zipfile.ZipFile(out, "w") as archive:
            for item in sorted(source.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    archive.write(item, item.relative_to(source.parent).as_posix())
        return 0
    shutil.copytree(ARGS[-2], ARGS[-1], symlinks=True)
    return 0


def spctl():
    log({"tool": "spctl", "name": "spctl", "argv": ARGS})
    if failed("spctl", ARGS[-1]):
        print(ARGS[-1] + ": rejected", file=sys.stderr)
        return 3
    source = knob("FAKE_SPCTL_SOURCE", "Notarized Developer ID")
    print(ARGS[-1] + ": " + knob("FAKE_SPCTL_VERDICT", "accepted") + "\nsource=" + source + "\norigin=Developer ID Application: Example Developer (" + TEAM + ")", file=sys.stderr)
    return int(knob("FAKE_SPCTL_EXIT", "0"))


def xattr():
    log({"tool": "xattr", "name": "xattr", "argv": ARGS})
    return 1 if failed("xattr") else 0


def uname():
    log({"tool": "uname", "name": "uname", "argv": ARGS})
    if "-m" in ARGS:
        print(knob("FAKE_UNAME_M", "arm64"))
    else:
        print(knob("FAKE_UNAME_S", "Darwin"))
    return 0


sys.exit({"codesign": codesign, "security": security, "xcrun": xcrun, "hdiutil": hdiutil, "ditto": ditto, "spctl": spctl, "xattr": xattr, "uname": uname}[TOOL]())
'''


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` with ``mode``, creating parents."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def write_manifest(runtime: Path) -> None:
    """(Re)write the runtime manifest the way stage_runtime.py does."""

    lock = {"python": "3.12.12", "pbsTag": "20261110", "targets": {"aarch64-apple-darwin": {"sha256": "b" * 64}}}
    facts = {"file": f"hyprial-{VERSION}-cp312-abi3-macosx_13_0_arm64.whl", "version": VERSION, "sha256": "c" * 64, "sourceCommit": COMMIT}
    manifest = stage_runtime.build_manifest(runtime, lock, "aarch64-apple-darwin", facts)
    write(runtime / stage_runtime.MANIFEST_NAME, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def make_app(root: Path) -> Path:
    """A tiny HyprialOS.app whose Mach-O files are recognisable only by magic."""

    app = root / "HyprialOS.app"
    contents = app / "Contents"
    write(contents / "Info.plist", plistlib.dumps({"CFBundleExecutable": "hyprial-desktop", "CFBundleIdentifier": "com.hyprial.hyprialos", "CFBundleShortVersionString": VERSION}))
    write(contents / "MacOS" / "hyprial-desktop", MACHO + b"main", 0o755)
    write(contents / "Resources" / "icon.icns", b"icns")
    (contents / "Resources" / "main-link").symlink_to("../MacOS/hyprial-desktop")
    runtime = contents / "Resources" / "runtime"
    write(runtime / "bin" / "python3.12", MACHO + b"python", 0o755)
    write(runtime / "lib" / "libpython3.12.dylib", FAT + b"fat library")
    site = runtime / "lib" / "python3.12" / "site-packages" / "pkg"
    write(site / "_ext.cpython-312-darwin.so", MACHO + b"ext")
    write(site / "deep" / "deeper" / "_x.so", MACHO + b"x")
    write(site / "noext_binary", MACHO + b"no extension", 0o755)
    write(site / "__init__.py", b"VALUE = 1\n")
    write(site / "fake.so", b"a text file named like a library\n")
    write(site / "Thing.class", JAVA_CLASS + b"java")
    write_zip(site / "data.zip", {"readme.txt": b"plain text"})
    write_manifest(runtime)
    return app


def write_zip(path: Path, members: dict[str, bytes]) -> None:
    """Write a zip archive with the given members."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    path.chmod(0o644)


def tree(path: Path) -> dict[str, tuple[str, int]]:
    """Every entry under ``path`` with its content hash (or link target) and mode."""

    result = {}
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        stat = item.lstat()
        if item.is_symlink():
            result[relative] = ("link:" + str(item.readlink()), stat.st_mode)
        elif item.is_file():
            result[relative] = (hashlib.sha256(item.read_bytes()).hexdigest(), stat.st_mode)
        else:
            result[relative] = ("dir", stat.st_mode)
    return result


class Bench:
    """A from-scratch tool PATH of fakes, a source app and output locations."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        for directory in (self.bin, self.state):
            directory.mkdir(parents=True)
        fake = self.bin / "_fake_tool.py"
        fake.write_text(FAKE_TOOL, encoding="utf-8")
        for name in FAKE_NAMES:
            launcher = self.bin / name
            launcher.write_text(f'#!/bin/sh\nFAKE_TOOL_NAME={name} FAKE_STATE="{self.state}" exec "{sys.executable}" -I "{fake}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        self.app = make_app(tmp_path / "source")
        self.out = tmp_path / "release" / "signed"
        self.out.parent.mkdir()
        self.unfinished = self.out.with_name(self.out.name + ".unfinished")
        self.evidence = tmp_path / "evidence" / "sign.json"
        self.env = {"HYPRIAL_SIGN_BUNDLE_TOOL_PATH": str(self.bin)}
        self.knob(FAKE_IDENTITIES=json.dumps([[SHA1, DEVID, LEAF]]), FAKE_TEAM=TEAM)

    def knob(self, **values: str) -> None:
        """Set behaviour knobs of the fake tools."""

        path = self.state / "knobs.json"
        knobs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        knobs.update(values)
        path.write_text(json.dumps(knobs), encoding="utf-8")

    def argv(self, *extra: str, identity: str | None = SHA1) -> list[str]:
        """The tool's argv for this bench; ``identity=None`` leaves the choice to ``extra``."""

        base = ["--app", str(self.app), "--notary-profile", PROFILE, "--out", str(self.out), "--evidence", str(self.evidence), "--root-parent", str(self.tmp)]
        if identity is not None:
            base += ["--identity", identity]
        return base + list(extra)

    def calls(self) -> list[dict]:
        """Every recorded fake call, in order."""

        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def names(self) -> list[str]:
        """The names of the recorded calls, in order."""

        return [call["name"] for call in self.calls()]

    def of(self, name: str) -> list[dict]:
        """The recorded calls named ``name``."""

        return [call for call in self.calls() if call["name"] == name]

    def probes(self) -> list[list[str]]:
        """The argv of each test signature (a scratch ``probe`` file under an ``hsp`` directory)."""

        return [call["argv"] for call in self.of("codesign-sign") if Path(call["argv"][-1]).name == "probe" and Path(call["argv"][-1]).parent.name.startswith("hsp")]

    def signs(self) -> list[list[str]]:
        """The argv of each real signature: every ``codesign --sign`` but the test signatures, which must come first."""

        signs = [call["argv"] for call in self.of("codesign-sign")]
        probes = self.probes()
        assert signs[: len(probes)] == probes, "a test signature after a real one"
        return signs[len(probes):]

    def evidence_json(self) -> dict:
        """The evidence the tool wrote."""

        return json.loads(self.evidence.read_text(encoding="utf-8"))

    def image(self, name: str) -> Path:
        """What the fake hdiutil saw as the source folder of dmg ``name``."""

        return self.state / "images" / name

    def reset(self) -> None:
        """Forget recorded calls and outputs so a second run starts clean."""

        for path in (self.out, self.unfinished, self.state / "images"):
            shutil.rmtree(path, ignore_errors=True)
        for name in ("calls.jsonl", "signed.json", "notary-count", "submitted.txt"):
            (self.state / name).unlink(missing_ok=True)
        self.evidence.unlink(missing_ok=True)

    def install(self, monkeypatch, module, runtime_check=None) -> Bench:
        """Point ``module`` (sign_bundle) at this bench: tool PATH, HOME, root pin, launch-check stand-in."""

        for key, value in self.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("HOME", str(self.tmp))
        monkeypatch.setattr(module, "APPLE_ROOT_SHA256", hashlib.sha256(ROOT_CERT).hexdigest())
        monkeypatch.setattr(module, "runtime_check", runtime_check or (lambda runtime, root_parent: (0, "PASS", {})))
        return self

    def logged(self, name: str) -> Path:
        """A path the evidence recorded, with its ``~`` (HOME is the bench) expanded."""

        return Path(name.replace("~", str(self.tmp), 1)) if name.startswith("~") else Path(name)
