#!/usr/bin/env python3
# Decisions (milestone 1 of Developer ID signing; packaging/clients/README.md
# "Signing"; KP-100):
# - Input is a HyprialOS.app built by scripts/build/package-macos-shell.sh
#   (ad hoc, Tauri). The tool never modifies it: everything happens on a
#   copy in `<out>.unfinished/`, which is renamed to `<out>` only after every
#   step passed. Any failure exits 1 and removes `<out>.unfinished`, then
#   confirms it is gone (else `cleanup: incomplete` and a problem); a dmg
#   still attached there is never removed. With --keep-unfinished it stays:
#   the dmg is deleted, every .app and .zip outside a bundle is renamed
#   `<name>-UNFINISHED` (renaming keeps a signature and a stapled ticket
#   valid, so the name and FAILED.txt are the marker; deleting the copies
#   would lose what a diagnosis needs), and FAILED.txt states whether app and
#   dmg were notarized and stapled, with the submission ids ("unknown" when a
#   submission exists that was not seen Accepted with exit 0). Path refusals
#   are usage errors (exit 2) that write nothing, evidence included, since
#   the evidence path is what is in question: --out, its .unfinished
#   sibling, --evidence and --root-parent resolved inside the input app, the
#   app inside --out, .unfinished or --evidence, an --evidence that already
#   exists. The app may sit under --root-parent: the launch check creates and
#   removes only its own mkdtemp directory there (the default /tmp would
#   otherwise refuse every app under /tmp). There is no ad-hoc fallback:
#   the only identity ever passed to `codesign --sign` is the 40-hex SHA-1
#   given or discovered, and an identity that is not a valid "Developer ID
#   Application" identity in `security find-identity -v -p codesigning` is
#   refused. Exit 0 PASS, 1 FAIL, 2 refused before anything was copied or
#   signed (same convention as the packaging script). Evidence JSON is
#   written on every path, outside the output directory, and notary logs go
#   next to it, so cleanup never removes them; an --evidence path inside
#   <out> or <out>.unfinished is a usage error (it would create <out> and
#   make the final rename fail after the notary submissions were spent).
#   An unexpected exception is a FAIL like any other (evidence, cleanup).
# - Tools come from /usr/bin:/bin:/usr/sbin:/sbin, never the caller's PATH;
#   the test-only variable HYPRIAL_SIGN_BUNDLE_TOOL_PATH replaces that PATH
#   (the pattern of setup-macos-signing-identity.sh). When it is set the run
#   warns on stderr and records `tools: overridden` in the evidence and in
#   BUILD-INFO.txt with "NOT a release"; chosen over a second test-only
#   switch because every caller of the fakes sets only the variable, and a
#   mis-set variable can then not yield an artifact that reads as a release.
#   Whatever is printed or recorded (argv, problems, evidence, FAILED.txt)
#   has every home directory prefix written `~` (scrub), notary logs are
#   recorded by file name only, so no account name reaches a log. Children get an
#   environment built from nothing but HOME (the account's real home: the
#   login keychain and the notarytool keychain profile live there; the
#   earlier temp-keychain harness hid both by isolating HOME), PATH, LANG and
#   TMPDIR. Every child has a timeout; a codesign that hangs most likely
#   waits for a keychain dialog and is reported as such. argv carries only
#   paths, the identity SHA-1 and the profile name, never a secret; recorded
#   commands replace the SHA-1 with `<identity>`. spctl's `origin=` line (the
#   certificate's common name) is not printed or recorded.
# - Mach-O is found by magic, not by name: thin MH_MAGIC/MH_MAGIC_64 in
#   either byte order, fat FAT_MAGIC/FAT_MAGIC_64 (big-endian) only when the
#   architecture count is 1..29, because a Java class file starts with the
#   same 0xCAFEBABE followed by a version >= 45. Symlinks are never
#   followed. Signing is inside out, deepest path first, each file on its
#   own with `codesign --force --options runtime --timestamp --sign <sha1>`;
#   `--deep` is never used to sign (only to verify). The main executable of
#   the app (and of any nested bundle with a Contents/Info.plist) is left to
#   its bundle's signature: signing it by path would seal the bundle before
#   the manifest is regenerated. Nested bundles (.app .framework .bundle
#   .xpc .appex .plugin) are signed after their contents because they are
#   shallower; the measured Tauri bundle has none.
# - Entitlements: none. Decision 2026-11-19: start with none and add one only
#   for a measured failure, each with its reason here. The milestone-1 run on
#   the packaging Mac (hardened runtime on every Mach-O, verify_runtime's
#   launch of the bundled interpreter, both launch smokes) needed none.
# - Runtime manifest: verified against the files before anything else
#   (refusal), and on the copy. After inner signing it is regenerated: the
#   file set, every mode and every key other than `files` must be identical
#   and the set of files whose SHA-256 changed must equal the set of signed
#   files under the runtime, which may not be empty (the file set is
#   stage_runtime.manifest_files); then verify_runtime.check_manifest must report
#   nothing. Written like stage_runtime.py (indent 2, sorted keys, trailing
#   newline, mode 0644). Only then is the app bundle signed, sealing it.
# - Before any notarization request: `codesign --verify --deep --strict` of
#   the app, an audit of every signed Mach-O and the app with `codesign -dvv`
#   (a Developer ID Application authority, the app's Team ID, the `runtime`
#   flag, a secure timestamp, not ad hoc, `Authority=` lines exactly the
#   Developer ID Application leaf, Developer ID Certification Authority and
#   Apple Root CA), the app's extracted chain must be three certificates, the
#   leaf with the given SHA-1 and the root with the SHA-256 of Apple Root CA
#   (APPLE_ROOT_SHA256, a public certificate; the intermediate is checked by
#   name only, Apple has issued more than one generation), and verify_runtime.run launches the
#   signed bundled interpreter (hardened runtime breaks extension loading
#   there first). Notarization requests are the scarce resource, so all of
#   this is proven locally first.
# - Archives (signing/archives.py, also run by the ad-hoc packaging path):
#   zip-family archives are opened and their members checked for Mach-O;
#   tar, gzip, bzip2, xz, zstd, xar and static libraries are refused by
#   magic. Apple's notary service inspects archives and Mach-O inside an
#   archive cannot be signed in place, so an archive holding Mach-O, an
#   unreadable one or one of those kinds fails the run before any signature:
#   removing or repacking it is a design decision, not something this tool
#   does.
# - Order, chosen so that the app works offline when copied out of the dmg:
#   notarize the app (zip made with `ditto -c -k --keepParent`), staple and
#   validate the app, build the dmg from the stapled app, sign the dmg
#   (`--timestamp`, no runtime option: it is not code), notarize the dmg,
#   staple and validate the dmg, then Gatekeeper: `spctl --assess --type
#   execute` on the app and `--type open --context context:primary-signature`
#   on the dmg must both say `accepted` and `source=Notarized Developer ID`.
#   A stapled dmg alone covers a user who opens the dmg; the app's own
#   ticket covers the copied app on a first launch without network. Last,
#   the final dmg is attached (`hdiutil attach -readonly -nobrowse
#   -noautoopen -mountpoint work/mnt`) and the app a user gets from it is
#   checked: `codesign --verify --deep --strict`, `stapler validate`,
#   check_manifest and `spctl --assess --type execute`; it is detached
#   afterwards, again with -force if needed, and a dmg that cannot be
#   detached fails the run. Two submissions per run. This tool does not run
#   the launch smokes (smoke_bundle.py): the packaging script runs them
#   against the final signed artifact.
# - Notary (PR #2980 review; signing/notary.py holds the details):
#   `notarytool submit` WITHOUT --wait uploads (bounded NOTARY_SUBMIT_SECONDS);
#   while it runs, `notarytool history` is asked every NOTARY_POLL_SECONDS
#   for the new submission, whose id is printed and saved at once; then
#   `notarytool wait <id> --timeout <m>m` waits (bounded m minutes plus
#   NOTARY_WAIT_SLACK_SECONDS) and the log is fetched (NOTARY_LOG_SECONDS; a
#   log that cannot be fetched is recorded as `logError` and does not fail
#   the run). The runner kills a cancelled or
#   timed-out step with SIGKILL, so nothing may depend on a handler: the
#   evidence JSON is rewritten atomically after every step and as soon as a
#   submission id is known (`verdict: INCOMPLETE` until the end). A status
#   other than Accepted with exit 0 fails. A wait that runs out or is cut
#   short by SIGTERM, SIGHUP or SIGINT is not resubmitted: the failure names
#   the id with "do not resubmit" (measured 2026-11-19: the first submission
#   of this team stayed In Progress past 30 minutes).
#   --notary-timeout-minutes lengthens the wait (default 30, at least 1).
# - Before the first real signature a test signature of a scratch file
#   (signing/modes.py, bounded 120 s) runs again, so a keychain that locked
#   during the long build fails in two minutes instead of CODESIGN_TIMEOUT.
# - Cleanup: once it has begun, a second SIGTERM, SIGHUP or SIGINT is
#   ignored. A dmg that cannot be detached leaves `<out>.unfinished` in place
#   but its signed copies are still renamed and FAILED.txt written (never
#   inside the mount). FAILED.txt says `stapled: yes (the staple's
#   validation failed)` when only the validation failed. With the test tool PATH the
#   first line of BUILD-INFO.txt says so. `--out` inside `--evidence`, and
#   `--root-parent` inside `--out` or its `.unfinished` directory, are usage
#   errors like the other path refusals.
# - dmg layout: the stapled app plus an `Applications` symlink to
#   /Applications, volume name HyprialOS, `hdiutil create -fs HFS+ -format
#   UDZO`, the same filesystem and format as Tauri's dmg (measured
#   2026-11-19). Tauri's dmg also carries a custom volume icon
#   (`.VolumeIcon.icns`); it is not reproduced (known gap, cosmetic).
# - Output naming: `HyprialOS_<version>_aarch64-signed.dmg` (the ad-hoc build
#   is `HyprialOS_<version>_aarch64.dmg`), the signed app under `app/`,
#   the regenerated runtime-manifest.json, SHA256SUMS of dmg and manifest,
#   and BUILD-INFO.txt stating signing method, Team ID, notary submission
#   ids and the checksums. The Team ID appears only in that generated file
#   and the evidence, never in this repository.
# - Two more modes (milestone 2, 2026-11-19), `--check-only` and
#   `--verify-only`, live in packaging/clients/signing/modes.py (its header
#   holds their decisions); this file only parses them and dispatches.
# - `--mark-unfinished` (PR #2980 review 38872; the package script passes
#   it): `UNFINISHED.txt` (UNFINISHED_NOTE) is written into
#   `<out>.unfinished` before its rename, so `<out>` never exists without
#   it, and with `--verify-only --extract-to` into that directory before the
#   app is copied there. The caller removes it once its own run passed. The
#   rules on `--out` and `--extract-to` (new directories) are unchanged; it
#   is refused where no signed copy is made. Without it nothing is written.
"""Sign, notarize and staple a built HyprialOS.app with a Developer ID (macOS arm64).

Exit status 0 PASS, 1 FAIL, 2 refused; one verdict line at the end.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import plistlib
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
TOOL_PATH_ENV = "HYPRIAL_SIGN_BUNDLE_TOOL_PATH"
MANIFEST_NAME = "runtime-manifest.json"
UNFINISHED_MARKER = "UNFINISHED.txt"
UNFINISHED_NOTE = "NOT a release: the run that signed this has not finished; only an --out without NOT-SIGNED.txt or NOT-BUILT.txt counts\n"
RUNTIME = Path("Contents") / "Resources" / "runtime"
NOTARY_TIMEOUT_MINUTES = 30
CODESIGN_TIMEOUT = 600.0
DEFAULT_TIMEOUT = 300.0
HDIUTIL_TIMEOUT = 1200.0
DETACH_TIMEOUT = 120.0
NOTARY_SUBMIT_SECONDS = 600.0
NOTARY_POLL_SECONDS = 15.0
NOTARY_WAIT_SLACK_SECONDS = 120.0
NOTARY_LOG_SECONDS = 120.0
SHA1_PATTERN = re.compile(r"^[0-9A-Fa-f]{40}$")
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z.+-]+$")
UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
IDENTITY_LINE = re.compile(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"(.*)"\s*$')
DEVELOPER_ID = "Developer ID Application:"
APPLE_ROOT_SHA256 = "b0b1730ecbc7ff4505142c49f1295e6eda6bcaed7e2c68c5be91b5a11001f024"
CHAIN = ["Developer ID Certification Authority", "Apple Root CA"]
UNFINISHED_MARK = "-UNFINISHED"
BUNDLE_SUFFIXES = {".app", ".framework", ".bundle", ".xpc", ".appex", ".plugin"}
NOTARIZED_SOURCE = "source=Notarized Developer ID"
DITTO_COPY = ("--norsrc", "--noextattr", "--noqtn", "--noacl")


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"_hyprialos_{name}", HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_runtime = _load("verify_runtime")
stage_runtime = _load("stage_runtime")
archives_module = _load("signing/archives")
modes = _load("signing/modes")
failure = _load("signing/failure")
notary = _load("signing/notary")
is_macho_header = archives_module.is_macho_header
archive_kind = archives_module.archive_kind
scan_archives = archives_module.scan_archives


def _homes() -> list[str]:
    found = {os.environ.get("HOME", ""), pwd.getpwuid(os.getuid()).pw_dir}
    found |= {os.path.realpath(home) for home in found if home}
    return sorted({home.rstrip("/") for home in found if home.rstrip("/")}, key=len, reverse=True)


def scrub(value: Any) -> Any:
    """``value`` with every home directory prefix written ``~`` (strings, lists and dicts of them)."""

    if isinstance(value, str):
        for home in _homes():
            value = re.sub(re.escape(home) + r"(?![A-Za-z0-9._-])", "~", value)
        return value
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def say(text: str, *, err: bool = False) -> None:
    """Print ``text`` scrubbed of home paths."""

    print(scrub(text), file=sys.stderr if err else sys.stdout, flush=True)


class Refused(Exception):
    """An input or host the tool will not start on (exit 2)."""


class SignError(Exception):
    """A step that failed after work started (exit 1)."""


class Tools:
    """Runs host tools from a fixed PATH with a from-nothing environment and records each call."""

    def __init__(self, identity: str | None = None) -> None:
        self.overridden = TOOL_PATH_ENV in os.environ
        self.path = os.environ.get(TOOL_PATH_ENV, SYSTEM_PATH)
        self.env = {
            "HOME": pwd.getpwuid(os.getuid()).pw_dir,
            "PATH": self.path,
            "LANG": "C",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        self.identity = identity
        self.log: list[dict[str, Any]] = []

    def redact(self, text: str) -> str:
        """Replace the identity SHA-1 and home directory prefixes in ``text``."""

        if self.identity:
            text = re.sub(re.escape(self.identity), "<identity>", text, flags=re.IGNORECASE)
        return scrub(text)

    def run(self, name: str, *args: str, timeout: float = DEFAULT_TIMEOUT, sink: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run ``name`` with ``args`` (output to files in ``sink`` when given); a missing tool or a timeout is a SignError."""

        executable = shutil.which(name, path=self.path)
        if executable is None:
            raise SignError(f"{name} not found in {self.path}")
        started = time.monotonic()
        record: dict[str, Any] = {"argv": [self.redact(arg) for arg in [name, *args]]}
        self.log.append(record)
        try:
            with contextlib.ExitStack() as files:
                streams: dict[str, Any] = {"capture_output": True, "text": True} if sink is None else {part: files.enter_context((sink / part).open("w")) for part in ("stdout", "stderr")}
                result = subprocess.run([executable, *args], env=self.env, timeout=timeout, check=False, stdin=subprocess.DEVNULL, **streams)
            if sink is not None:
                result.stdout, result.stderr = (_text(sink / part) for part in ("stdout", "stderr"))
        except subprocess.TimeoutExpired as error:
            record.update({"exit": None, "seconds": round(time.monotonic() - started, 1)})
            hint = " (a keychain dialog may be waiting)" if name in {"codesign", "security"} else ""
            raise SignError(f"{name} {args[0] if args else ''} timeout after {timeout:.0f}s{hint}") from error
        record.update({"exit": result.returncode, "seconds": round(time.monotonic() - started, 1)})
        return result

    def start(self, name: str, *args: str, sink: Path) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
        """Start ``name`` in the background with output to files in ``sink``; the caller bounds and records it."""

        executable = shutil.which(name, path=self.path)
        if executable is None:
            raise SignError(f"{name} not found in {self.path}")
        record: dict[str, Any] = {"argv": [self.redact(arg) for arg in [name, *args]]}
        self.log.append(record)
        return notary.start(self, executable, list(args), sink), record

    def must(self, name: str, *args: str, timeout: float = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
        """Run and fail on a non-zero exit, with the tool's own last lines (redacted)."""

        result = self.run(name, *args, timeout=timeout)
        if result.returncode != 0:
            tail = " | ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
            raise SignError(f"{name} {args[0] if args else ''} failed (exit {result.returncode}): {self.redact(tail)}")
        return result


def is_macho(path: Path) -> bool:
    """Whether ``path`` is a regular file (not a symlink) holding Mach-O."""

    if not stat.S_ISREG(path.lstat().st_mode):
        return False
    with path.open("rb") as stream:
        return is_macho_header(stream.read(8))


def _walk(root: Path) -> list[Path]:
    found = []
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        found.extend(base / name for name in names)
        found.extend(base / name for name in files)
    return sorted(found)


def _bundle_executable(bundle: Path) -> Path | None:
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
    return bundle / "Contents" / "MacOS" / name if isinstance(name, str) else None


def signing_order(app: Path) -> list[Path]:
    """Every Mach-O and nested bundle to sign before the app itself, deepest first."""

    entries = _walk(app / "Contents")
    bundles = [path for path in entries if path.suffix in BUNDLE_SUFFIXES and not path.is_symlink() and path.is_dir()]
    left_to_bundle = {executable for executable in (_bundle_executable(path) for path in [app, *bundles]) if executable is not None}
    machos = [path for path in entries if path not in left_to_bundle and is_macho(path)]
    return sorted([*machos, *bundles], key=lambda path: (-len(path.parts), str(path)))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regenerate_manifest(runtime: Path, signed: set[str]) -> dict[str, Any]:
    """Rewrite the runtime manifest after signing; only the signed files' hashes may change."""

    if not signed:
        raise SignError("runtime manifest: nothing under the runtime was signed (no Mach-O found)")
    path = runtime / MANIFEST_NAME
    original = path.read_bytes()
    before = json.loads(original)
    files = stage_runtime.manifest_files(runtime)
    old = before["files"]
    if set(files) != set(old):
        added, removed = sorted(set(files) - set(old)), sorted(set(old) - set(files))
        raise SignError(f"runtime manifest: file set changed while signing (added {added[:5]}, removed {removed[:5]})")
    modes = sorted(name for name in files if files[name]["mode"] != old[name]["mode"])
    if modes:
        raise SignError(f"runtime manifest: mode changed while signing: {modes[:5]}")
    changed = {name for name in files if files[name]["sha256"] != old[name]["sha256"]}
    if changed != signed:
        raise SignError(f"runtime manifest: changed files are not the signed files (unexpected {sorted(changed - signed)[:5]}, unchanged {sorted(signed - changed)[:5]})")
    after = dict(before, files=files)
    text = json.dumps(after, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)
    problems, _ = verify_runtime.check_manifest(runtime)
    if problems:
        path.write_bytes(original)
        raise SignError(f"runtime manifest: regenerated manifest does not verify: {problems[:5]}")
    return {"beforeSha256": hashlib.sha256(original).hexdigest(), "afterSha256": hashlib.sha256(text.encode()).hexdigest(), "changedFiles": len(changed)}


def developer_id_identities(tools: Tools) -> set[str]:
    """SHA-1s of the valid Developer ID Application identities ``security`` lists."""

    result = tools.must("security", "find-identity", "-v", "-p", "codesigning")
    found = set()
    for line in result.stdout.splitlines():
        match = IDENTITY_LINE.match(line)
        if match and match.group(2).startswith(DEVELOPER_ID):
            found.add(match.group(1).upper())
    return found


def discover_identity(tools: Tools) -> str:
    """The single valid Developer ID Application identity, else Refused."""

    try:
        found = developer_id_identities(tools)
    except SignError as error:
        raise Refused(str(error)) from error
    if len(found) != 1:
        raise Refused(f"expected exactly one valid {DEVELOPER_ID[:-1]} identity, found {len(found)}; pass --identity <sha1>")
    return next(iter(found))


def runtime_check(runtime: Path, root_parent: Path) -> tuple[int, str, dict[str, Any]]:
    """Launch the signed bundled interpreter in an isolated home (verify_runtime.run)."""

    return verify_runtime.run(runtime, root_parent=root_parent, startup_timeout=200.0, stop_timeout=60.0, keep=False)


def audit(tools: Tools, path: Path, label: str, *, team: str | None, runtime: bool) -> str:
    """Check one signature with ``codesign -dvv``; returns its Team ID."""

    result = tools.must("codesign", "-dvv", str(path))
    lines = (result.stderr + result.stdout).splitlines()
    found_team = next((line.split("=", 1)[1] for line in lines if line.startswith("TeamIdentifier=")), "not set")
    problems = []
    if any(line.startswith("Signature=adhoc") for line in lines):
        problems.append("ad hoc")
    authorities = [line.split("=", 1)[1] for line in lines if line.startswith("Authority=")]
    if not authorities or not authorities[0].startswith(DEVELOPER_ID):
        problems.append("no Developer ID Application authority")
    if authorities[1:] != CHAIN:
        problems.append("unexpected certificate chain")
    if not any(line.startswith("Timestamp=") for line in lines):
        problems.append("no secure timestamp")
    if found_team == "not set" or (team is not None and found_team != team):
        problems.append("unexpected Team ID")
    if runtime and not any(line.startswith("CodeDirectory") and "runtime" in line for line in lines):
        problems.append("no hardened runtime")
    if problems:
        raise SignError(f"signature audit of {label}: {', '.join(problems)}")
    return found_team


def check_leaf(tools: Tools, app: Path, identity: str, work: Path) -> None:
    """The app's leaf certificate must be the identity that was asked for, its root Apple Root CA."""

    prefix = work / "cert"
    tools.must("codesign", "-d", f"--extract-certificates={prefix}", str(app))
    leaf, _, root, extra = (Path(f"{prefix}{index}") for index in range(4))
    if not leaf.is_file() or hashlib.sha1(leaf.read_bytes()).hexdigest().upper() != identity:
        raise SignError("the app's leaf certificate is not the requested identity")
    if extra.exists() or not root.is_file() or hashlib.sha256(root.read_bytes()).hexdigest() != APPLE_ROOT_SHA256:
        raise SignError("the app's certificate chain is not three certificates ending in the pinned Apple Root CA root certificate")


def gatekeeper(tools: Tools, *args: str) -> list[str]:
    """Run spctl; it must accept with source Notarized Developer ID. Returns the verdict lines."""

    result = tools.run("spctl", *args)
    lines = [line.strip() for line in (result.stderr + result.stdout).splitlines() if not line.strip().startswith("origin=")]
    kept = [line.replace(args[-1], Path(args[-1]).name) for line in lines if line.endswith(": accepted") or line.endswith(": rejected") or line.startswith("source=")]
    if result.returncode != 0 or not any(line.endswith(": accepted") for line in lines) or NOTARIZED_SOURCE not in lines:
        raise SignError(f"Gatekeeper ({args[2]}) did not accept with {NOTARIZED_SOURCE}: exit {result.returncode}, {kept}")
    return kept


def found_id(scratch: Path) -> str:
    """The first submission id in notarytool's output files in ``scratch``."""

    found = UUID_PATTERN.search(_text(scratch / "stdout") + "\n" + _text(scratch / "stderr"))
    return found.group(0) if found else ""


def notarize(tools: Tools, path: Path, label: str, profile: str, evidence_path: Path, evidence: dict[str, Any], minutes: int = NOTARY_TIMEOUT_MINUTES, saved: Any = None) -> None:
    """Submit, wait and log one notarization (signing/notary.py); the id is saved as soon as Apple lists it."""

    notary.notarize(sys.modules[__name__], tools, path, label, profile, evidence_path, evidence, minutes, saved or (lambda: None))


def staple(tools: Tools, path: Path, label: str, evidence: dict[str, Any]) -> None:
    """Staple the ticket to ``path`` and validate it, recording how far it got."""

    tools.must("xcrun", "stapler", "staple", str(path))
    evidence.setdefault("stapled", {})[label] = "stapled"
    tools.must("xcrun", "stapler", "validate", str(path))
    evidence["stapled"][label] = "validated"


def detach(tools: Tools, mount: Path) -> None:
    """Detach the image at ``mount``, forcing it when a plain detach fails."""

    if tools.run("hdiutil", "detach", str(mount), timeout=DETACH_TIMEOUT).returncode != 0:
        tools.must("hdiutil", "detach", "-force", str(mount), timeout=DETACH_TIMEOUT)


def verify_mounted(tools: Tools, dmg: Path, mount: Path, name: str) -> dict[str, Any]:
    """Attach the final dmg read-only and check the app a user gets from it."""

    mount.mkdir()
    tools.must("hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", str(mount), str(dmg), timeout=DEFAULT_TIMEOUT)
    try:
        app = mount / name
        tools.must("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))
        tools.must("xcrun", "stapler", "validate", str(app))
        problems, _ = verify_runtime.check_manifest(app / RUNTIME)
        if problems:
            raise SignError(f"runtime manifest of the app in the dmg does not verify: {problems[:5]}")
        result = {"gatekeeper": gatekeeper(tools, "--assess", "--type", "execute", "-vv", str(app))}
    except BaseException:
        with contextlib.suppress(SignError):
            detach(tools, mount)
        raise
    detach(tools, mount)
    return result


def sign(tools: Tools, identity: str, path: Path, *, runtime: bool = True) -> None:
    """Sign one path with the identity; hardened runtime unless it is not code (the dmg)."""

    options = ["--options", "runtime"] if runtime else []
    tools.must("codesign", "--force", *options, "--timestamp", "--sign", identity, str(path), timeout=CODESIGN_TIMEOUT)


class Run:
    """One signing run: preflight, then the steps, recording evidence."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.app = Path(args.app or ".").absolute()
        self.out = Path(args.out or ".").absolute()
        self.unfinished = self.out.with_name(self.out.name + ".unfinished")
        self.evidence_path = Path(args.evidence).absolute()
        self.tools = Tools()
        self.identity = ""
        self.version = ""
        self.team = ""
        self.created = False
        self.evidence: dict[str, Any] = {"tool": "sign_bundle", "verdict": None, "steps": [], "problems": [], "notary": [], "archives": None, "manifest": None, "machoSigned": 0, "entitlements": [], "tools": "overridden" if self.tools.overridden else "system"}

    def step(self, name: str, action: Any) -> Any:
        """Run ``action`` as a named step with its duration in the evidence, saved after it."""

        print(f"==> {name}", flush=True)
        started = time.monotonic()
        entry = {"step": name, "status": "FAIL"}
        self.evidence["steps"].append(entry)
        try:
            result = action()
        finally:
            entry["seconds"] = round(time.monotonic() - started, 1)
            self.save()
        entry["status"] = "PASS"
        self.save()
        return result

    def save(self) -> None:
        """Write the evidence as it stands, atomically (``verdict`` INCOMPLETE until the end)."""

        current = scrub({**self.evidence, "verdict": self.evidence["verdict"] or "INCOMPLETE", "commands": self.tools.log})
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.evidence_path.with_name(self.evidence_path.name + ".partial")
        partial.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(partial, self.evidence_path)

    def host(self) -> None:
        """macOS arm64 only."""

        system = self.tools.must("uname", "-s").stdout.strip()
        machine = self.tools.must("uname", "-m").stdout.strip()
        if (system, machine) != ("Darwin", "arm64"):
            raise Refused(f"macOS arm64 only (this host: {system} {machine})")

    def preflight_signer(self) -> None:
        """Profile name and identity refusals (every mode)."""

        if not self.args.verify_only and not PROFILE_PATTERN.match(self.args.notary_profile or ""):
            raise Refused("notary profile name must be letters, digits, '.', '_' or '-'")
        if self.args.discover_identity:
            self.identity = discover_identity(self.tools)
        else:
            if not SHA1_PATTERN.match(self.args.identity or ""):
                raise Refused("--identity must be the 40-hex SHA-1 of a Developer ID Application identity")
            self.identity = self.args.identity.upper()
            try:
                known = developer_id_identities(self.tools)
            except SignError as error:
                raise Refused(str(error)) from error
            if self.identity not in known:
                raise Refused(f"the identity is not a valid {DEVELOPER_ID[:-1]} identity in the keychain search list")
        self.tools.identity = self.identity

    def preflight(self) -> None:
        """Every refusal, before anything is created."""

        self.host()
        if not (self.app / "Contents" / "Info.plist").is_file():
            raise Refused(f"app not found or not a bundle: {self.app}")
        self.preflight_signer()
        for path in (self.out, self.unfinished):
            if os.path.lexists(path):
                raise Refused(f"{path} already exists; remove it or choose another --out")
        problems, _ = verify_runtime.check_manifest(self.app / RUNTIME)
        if problems:
            raise Refused(f"runtime manifest does not match the bundle before signing: {problems[:5]}")
        info = plistlib.loads((self.app / "Contents" / "Info.plist").read_bytes())
        self.version = str(info.get("CFBundleShortVersionString", ""))
        if not VERSION_PATTERN.match(self.version):
            raise Refused("the app's CFBundleShortVersionString is missing or malformed")

    def execute(self) -> None:
        """All steps after preflight; raises SignError on the first failure."""

        self.unfinished.mkdir(parents=True)
        self.created = True
        work = self.unfinished / "work"
        work.mkdir()
        app = self.unfinished / "app" / self.app.name
        app.parent.mkdir()
        runtime = app / RUNTIME
        dmg = self.unfinished / f"HyprialOS_{self.version}_aarch64-signed.dmg"
        tools = self.tools

        def copy() -> None:
            tools.must("ditto", *DITTO_COPY, str(self.app), str(app), timeout=HDIUTIL_TIMEOUT)
            problems, _ = verify_runtime.check_manifest(runtime)
            if problems:
                raise SignError(f"runtime manifest of the copy does not verify: {problems[:5]}")

        def archives() -> None:
            report = scan_archives(app)
            self.evidence["archives"] = report
            say(archives_module.summary(report))
            if archives_module.blocking(report):
                raise SignError(f"archives holding Mach-O {report['withMacho']}, unreadable {report['unreadable']} or of other kinds {report['otherArchives']}: removing or repacking them is a design decision")

        def inner() -> list[Path]:
            order = signing_order(app)
            for path in order:
                sign(tools, self.identity, path)
            self.evidence["machoSigned"] = len(order)
            say(f"signed {len(order)} inner items")
            return order

        self.step("copy", copy)
        self.step("archives", archives)
        self.step("test-signature", lambda: modes.probe_signature(self, sys.modules[__name__]))
        order = self.step("sign-inner", inner)
        signed = {path.relative_to(runtime).as_posix() for path in order if path.is_relative_to(runtime) and path.is_file()}
        self.evidence["manifest"] = self.step("manifest", lambda: regenerate_manifest(runtime, signed))
        self.step("sign-app", lambda: sign(tools, self.identity, app))

        def verify() -> None:
            tools.must("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))
            self.team = audit(tools, app, "the app", team=None, runtime=True)
            for path in order:
                audit(tools, path, path.relative_to(app).as_posix(), team=self.team, runtime=True)
            check_leaf(tools, app, self.identity, work)

        self.step("verify-signatures", verify)

        def launch() -> None:
            parent = Path(tempfile.mkdtemp(prefix="hsb", dir=self.args.root_parent))
            try:
                code, line, _ = runtime_check(runtime, parent)
            finally:
                shutil.rmtree(parent, ignore_errors=True)
            if code != 0:
                raise SignError(f"launch check of the signed runtime failed: {line}")

        self.step("runtime-launch", launch)

        def notarize_app() -> None:
            archive = work / f"{app.stem}.zip"
            tools.must("ditto", "-c", "-k", "--keepParent", str(app), str(archive), timeout=HDIUTIL_TIMEOUT)
            notarize(tools, archive, "app", self.args.notary_profile, self.evidence_path, self.evidence, self.args.notary_timeout_minutes, self.save)
            archive.unlink()

        self.step("notarize-app", notarize_app)
        self.step("staple-app", lambda: staple(tools, app, "app", self.evidence))

        def build_dmg() -> None:
            root = work / "dmg-root"
            root.mkdir()
            tools.must("ditto", *DITTO_COPY, str(app), str(root / app.name), timeout=HDIUTIL_TIMEOUT)
            os.symlink("/Applications", root / "Applications")
            tools.must("hdiutil", "create", "-volname", "HyprialOS", "-srcfolder", str(root), "-fs", "HFS+", "-format", "UDZO", str(dmg), timeout=HDIUTIL_TIMEOUT)
            shutil.rmtree(root)

        self.step("build-dmg", build_dmg)

        def sign_dmg() -> None:
            sign(tools, self.identity, dmg, runtime=False)
            tools.must("codesign", "--verify", "--strict", "--verbose=2", str(dmg))
            audit(tools, dmg, "the dmg", team=self.team, runtime=False)

        self.step("sign-dmg", sign_dmg)
        self.step("notarize-dmg", lambda: notarize(tools, dmg, "dmg", self.args.notary_profile, self.evidence_path, self.evidence, self.args.notary_timeout_minutes, self.save))
        self.step("staple-dmg", lambda: staple(tools, dmg, "dmg", self.evidence))

        def assess() -> None:
            self.evidence["gatekeeper"] = {
                "app": gatekeeper(tools, "--assess", "--type", "execute", "-vv", str(app)),
                "dmg": gatekeeper(tools, "--assess", "--type", "open", "--context", "context:primary-signature", "-vv", str(dmg)),
            }
            tools.must("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))
            problems, _ = verify_runtime.check_manifest(runtime)
            if problems:
                raise SignError(f"runtime manifest of the stapled app does not verify: {problems[:5]}")

        self.step("gatekeeper", assess)
        self.evidence["dmgApp"] = self.step("verify-dmg-app", lambda: verify_mounted(tools, dmg, work / "mnt", app.name))
        self.step("collect", lambda: self.collect(work, runtime, dmg))

    def collect(self, work: Path, runtime: Path, dmg: Path) -> None:
        """Write manifest copy, SHA256SUMS and BUILD-INFO.txt, then move into place."""

        shutil.rmtree(work)
        manifest = self.unfinished / MANIFEST_NAME
        shutil.copyfile(runtime / MANIFEST_NAME, manifest)
        manifest.chmod(0o644)
        sums = "".join(f"{_sha256(path)}  {path.name}\n" for path in (dmg, manifest))
        (self.unfinished / "SHA256SUMS").write_text(sums, encoding="utf-8")
        source = json.loads(manifest.read_text(encoding="utf-8"))
        notary = "".join(f"  {item['label']}: {item['id']} {item['status']}\n" for item in self.evidence["notary"])
        gate = self.evidence["gatekeeper"]
        tools = f"overridden by {TOOL_PATH_ENV} (test only, NOT a release)" if self.tools.overridden else f"system ({SYSTEM_PATH})"
        info = (
            ("TEST TOOLS (" + TOOL_PATH_ENV + " set): NOT a release\n" if self.tools.overridden else "")
            + "HyprialOS desktop bundle, Developer ID signed (not ad hoc), notarized and stapled\n"
            "signing method: Developer ID Application, codesign --options runtime (hardened runtime) --timestamp, "
            "inside out, each Mach-O on its own, no --deep, no entitlements\n"
            f"team id: {self.team}\n"
            f"inner items signed: {self.evidence['machoSigned']}\n"
            f"version: {self.version}\n"
            f"wheel: {source.get('wheel', {}).get('file', 'unknown')}\n"
            f"source commit: {source.get('sourceCommit', 'unknown')}\n"
            f"runtime manifest regenerated: {self.evidence['manifest']['changedFiles']} file hashes changed by signing\n"
            "notary submissions:\n" + notary
            + "stapled: app, dmg\n"
            f"gatekeeper app: {' '.join(gate['app'])}\n"
            f"gatekeeper dmg: {' '.join(gate['dmg'])}\n"
            f"app in the mounted dmg: verified, stapled, manifest checked, {' '.join(self.evidence['dmgApp']['gatekeeper'])}\n"
            f"tools: {tools}\n"
            "SHA256SUMS:\n" + sums
        )
        (self.unfinished / "BUILD-INFO.txt").write_text(info, encoding="utf-8")
        if self.args.mark_unfinished:
            (self.unfinished / UNFINISHED_MARKER).write_text(UNFINISHED_NOTE, encoding="utf-8")
        self.unfinished.rename(self.out)
        self.created = False
        self.evidence["output"] = str(self.out)
        self.evidence["dmg"] = {"name": dmg.name, "sha256": _sha256(self.out / dmg.name)}
        say(f"signed dmg: {self.out / dmg.name}")

    def fail_cleanup(self, reason: str) -> None:
        """Remove or mark the unfinished directory (signing/failure.py)."""

        failure.fail_cleanup(self, sys.modules[__name__], reason)


def path_problem(args: argparse.Namespace) -> str | None:
    """Why the paths given cannot be used together (outputs inside the input app and the like), or None."""

    evidence = Path(args.evidence).resolve()
    named = {"--evidence": evidence}
    if args.out is not None:
        out = Path(args.out).resolve()
        unfinished = out.with_name(out.name + ".unfinished")
        named = {"--out": out, "its .unfinished directory": unfinished, "--evidence": evidence}
    if args.app is not None:
        app = Path(args.app).resolve()
        for name, path in [*named.items(), ("--root-parent", Path(args.root_parent).resolve())]:
            if path.is_relative_to(app):
                return f"{name} {path} is inside the input app {app}"
        for name, path in named.items():
            if app.is_relative_to(path):
                return f"the input app {app} is inside {name} {path}"
    if args.out is not None and (evidence.is_relative_to(out) or evidence.is_relative_to(unfinished)):
        return "--evidence must be outside --out and its .unfinished directory"
    if args.out is not None and out.is_relative_to(evidence):
        return "--out must be outside --evidence"
    if args.out is not None and any(Path(args.root_parent).resolve().is_relative_to(path) for path in (out, unfinished)):
        return "--root-parent must be outside --out and its .unfinished directory"
    if os.path.lexists(evidence):
        return f"--evidence {evidence} already exists; give a new path"
    return None


def _interrupt(number: int, _frame: Any) -> None:
    raise SignError(f"interrupted by signal {number}")


def _cleanup_ignoring_signals(run: Run, handlers: dict[int, Any], reason: str) -> None:
    for number in handlers:
        signal.signal(number, signal.SIG_IGN)
    run.save()
    run.fail_cleanup(reason)


def _minutes(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 24 * 60:
        raise argparse.ArgumentTypeError("minutes must be 1..1440")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="the built HyprialOS.app (never modified); signing only")
    chooser = parser.add_mutually_exclusive_group(required=True)
    chooser.add_argument("--identity", help="40-hex SHA-1 of the Developer ID Application identity")
    chooser.add_argument("--discover-identity", action="store_true", help="use the single valid Developer ID Application identity")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true", help="only check the identity and that the notary profile answers (no app, no --out)")
    mode.add_argument("--verify-only", action="store_true", help="only re-check the finished output directory --out (no --app)")
    parser.add_argument("--notary-profile", help="notarytool keychain profile name (signing and --check-only)")
    parser.add_argument("--out", type=Path, help="output directory (must not exist; with --verify-only the finished one)")
    parser.add_argument("--evidence", type=Path, required=True, help="evidence JSON path (outside --out)")
    parser.add_argument("--root-parent", type=Path, default=Path("/tmp"), help="short parent for the isolated launch check (AF_UNIX limit)")
    parser.add_argument("--notary-timeout-minutes", type=_minutes, default=NOTARY_TIMEOUT_MINUTES, help="notarytool --wait --timeout in minutes (default 30)")
    parser.add_argument("--keep-unfinished", action="store_true", help="on failure keep <out>.unfinished marked FAILED, without its dmg")
    parser.add_argument("--extract-to", type=Path, help="--verify-only: copy the app out of the attached final dmg into this new directory and quarantine it")
    parser.add_argument("--mark-unfinished", action="store_true", help="write UNFINISHED.txt into the signed output (before its rename) or the --extract-to directory (before the copy)")
    args = parser.parse_args(argv)
    if args.check_only and (args.app is not None or args.out is not None):
        parser.error("--check-only takes neither --app nor --out")
    if args.verify_only and (args.app is not None or args.out is None):
        parser.error("--verify-only needs --out and takes no --app")
    if not (args.check_only or args.verify_only) and (args.app is None or args.out is None):
        parser.error("signing needs --app and --out")
    if args.extract_to is not None and (not args.verify_only or os.path.lexists(args.extract_to) or args.extract_to.resolve().is_relative_to(args.out.resolve()) or args.out.resolve().is_relative_to(args.extract_to.resolve())):
        parser.error("--extract-to needs --verify-only and a new directory outside --out")
    if args.mark_unfinished and (args.check_only or (args.verify_only and args.extract_to is None)):
        parser.error("--mark-unfinished needs signing or --verify-only --extract-to")
    if not args.verify_only and args.notary_profile is None:
        parser.error("--notary-profile is required (signing and --check-only)")
    problem = path_problem(args)
    if problem:
        parser.error(scrub(problem))
    run = Run(args)
    run.evidence["mode"] = "check" if args.check_only else "verify" if args.verify_only else "sign"
    if run.tools.overridden:
        say(f"WARNING: {TOOL_PATH_ENV} is set: tools come from {run.tools.path}, not {SYSTEM_PATH}; test only, the result is NOT a release", err=True)
    previous = {number: signal.signal(number, _interrupt) for number in (signal.SIGTERM, signal.SIGHUP)}
    previous[signal.SIGINT] = signal.getsignal(signal.SIGINT)
    code = 1
    try:
        if args.check_only:
            modes.check(run, sys.modules[__name__])
        elif args.verify_only:
            modes.verify_output(run, sys.modules[__name__])
        else:
            run.preflight()
            run.execute()
        code = 0
    except Refused as error:
        code = 2
        run.evidence["problems"].append(str(error))
    except (SignError, KeyboardInterrupt) as error:
        reason = str(error) or "interrupted"
        run.evidence["problems"].append(reason)
        _cleanup_ignoring_signals(run, previous, reason)
    except Exception as error:
        reason = f"unexpected {type(error).__name__}: {run.tools.redact(str(error))}"
        run.evidence["problems"].append(reason)
        _cleanup_ignoring_signals(run, previous, reason)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    run.evidence["verdict"] = {0: "PASS", 1: "FAIL", 2: "REFUSED"}[code]
    run.evidence["exit"] = code
    run.evidence["commands"] = run.tools.log
    run.evidence_path.parent.mkdir(parents=True, exist_ok=True)
    run.evidence = scrub(run.evidence)
    run.evidence_path.write_text(json.dumps(run.evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for line in run.evidence["problems"]:
        say(f"problem: {line}")
    say(run.evidence["verdict"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
