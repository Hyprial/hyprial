#!/usr/bin/env python3
# Decisions:
# - Two more modes of packaging/clients/sign_bundle.py (milestone 2,
#   2026-11-19), so that the packaging script
#   (scripts/build/package-macos-shell.sh --developer-id) and its workflow
#   never call an Apple tool themselves: every codesign --sign, notarytool,
#   stapler and spctl call stays in sign_bundle.py, this file and
#   signing/notary.py.
#   `--check-only` runs before the long build: host, profile name, identity
#   (given or discovered, as in sign_bundle.py), a test signature of a scratch copy of /usr/bin/true with the identity
#   (`--timestamp`, bounded to 120 s, verified, then removed) and
#   `notarytool history --keychain-profile <p>`, which proves the profile
#   answers without spending a submission; a failure there is a refusal (2).
#   The login keychain is unlocked only inside the logged-in desktop session;
#   after a reboot without a GUI login it is locked and the run must FAIL,
#   not hang: the test signature fails (or, if codesign waits for a dialog,
#   its 120 s bound runs out) before the long build, every child has a
#   timeout and stdin is /dev/null, so no prompt can be answered. The
#   reboot case itself was not tested (KP-100). `--verify-only` re-checks a
#   finished output directory, the final artifact: `codesign --verify --deep --strict` and
#   the audit of the app and of every inner item, the leaf certificate, the
#   dmg's signature, `stapler validate` of app and dmg, both Gatekeeper
#   assessments, SHA256SUMS, the copied manifest equal to the app's and the
#   Team ID in BUILD-INFO.txt, then the launch check of the signed runtime.
#   Neither mode changes --out; both write evidence. The evidence of
#   `--verify-only` records the SHA-256 of the dmg, the manifest and
#   SHA256SUMS it verified, so the packaging script can require that what it
#   collects is exactly that.
# - `--verify-only --extract-to <dir>` (PR #2980 review, item D): last, the
#   final dmg is attached read-only under `<dir>/mnt`, the app is copied out
#   with `ditto` to `<dir>/app` the way a user drags it, the copy gets a
#   `com.apple.quarantine` attribute of the form a browser download carries
#   (`0081;<hex time>;Safari;<UUID>`, written with `xattr -w -r`), and the
#   copy is checked (`codesign --verify --deep --strict`, `stapler
#   validate`, `spctl --assess --type execute`). The image is detached on
#   every path (plain, then -force). The dmg's SHA-256 is taken before the
#   attach and again after the detach and must not differ; it is recorded as
#   `finalCopy.dmgSha256`. The packaging script runs both launch smokes on
#   that quarantined copy. A smoke executes the binary directly, which does
#   not go through LaunchServices: no app translocation and no first-launch
#   dialog happen, so a quarantined copy started this way is not the
#   Finder first launch, which stays an owner check. With `--mark-unfinished`
#   `<dir>/UNFINISHED.txt` is written before the copy (sign_bundle.py header).
# - The test signature is also used by sign_bundle.py right before its first
#   real signature (probe_signature), so a keychain that locked during the
#   long build fails within the 120 s bound.
# - The functions take the running sign_bundle Run and the sign_bundle module
#   itself, so the helpers (audit, gatekeeper, the launch check) stay in one
#   place and a test that replaces one of them there is seen here too. Moved
#   out of sign_bundle.py to keep that file under the 1000-line limit.
"""--check-only and --verify-only of the Developer ID signing tool (macOS arm64)."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

PROBE_SIGN_TIMEOUT = 120.0
PROBE_SOURCE = Path("/usr/bin/true")


def probe_signature(run: Any, sb: Any) -> None:
    """Sign and verify a scratch copy of /usr/bin/true with the identity, bounded; SignError when it fails."""

    scratch = Path(tempfile.mkdtemp(prefix="hsp"))
    target = scratch / "probe"
    try:
        shutil.copyfile(PROBE_SOURCE, target)
        target.chmod(0o755)
        run.tools.must("codesign", "--force", "--timestamp", "--sign", run.identity, str(target), timeout=PROBE_SIGN_TIMEOUT)
        run.tools.must("codesign", "--verify", "--strict", str(target))
    except sb.SignError as error:
        raise sb.SignError(f"a test signature with the identity failed (a locked keychain fails here): {error}") from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check(run: Any, sb: Any) -> None:
    """--check-only: identity and notary profile usable, nothing signed or submitted."""

    run.host()
    run.preflight_signer()

    def probe() -> None:
        try:
            probe_signature(run, sb)
        except sb.SignError as error:
            raise sb.Refused(str(error)) from error

    def profile() -> None:
        try:
            run.tools.must("xcrun", "notarytool", "history", "--keychain-profile", run.args.notary_profile, "--output-format", "json")
        except sb.SignError as error:
            raise sb.Refused(f"the notary profile does not answer: {error}") from error

    run.step("test-signature", probe)
    run.step("notary-profile", profile)
    print("signing preflight: the identity is a valid Developer ID Application identity and the notary profile answers")


def quarantine_value() -> str:
    """A com.apple.quarantine value of the form a browser download carries."""

    return f"0081;{int(time.time()):08x};Safari;{str(uuid.uuid4()).upper()}"


def extract(run: Any, sb: Any, dmg: Path, target: Path) -> dict[str, Any]:
    """Attach the final dmg read-only, ditto its app out, quarantine and check the copy, detach on every path."""

    tools = run.tools
    before = sb._sha256(dmg)
    mount = target / "mnt"
    copy = target / "app" / "HyprialOS.app"
    mount.mkdir(parents=True)
    if run.args.mark_unfinished:
        (target / sb.UNFINISHED_MARKER).write_text(sb.UNFINISHED_NOTE, encoding="utf-8")
    copy.parent.mkdir()
    tools.must("hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", str(mount), str(dmg), timeout=sb.DEFAULT_TIMEOUT)
    try:
        tools.must("ditto", str(mount / copy.name), str(copy), timeout=sb.HDIUTIL_TIMEOUT)
    except BaseException:
        with contextlib.suppress(sb.SignError):
            sb.detach(tools, mount)
        raise
    sb.detach(tools, mount)
    if sb._sha256(dmg) != before:
        raise sb.SignError("the dmg changed while it was attached")
    value = quarantine_value()
    tools.must("xattr", "-w", "-r", "com.apple.quarantine", value, str(copy))
    tools.must("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(copy))
    tools.must("xcrun", "stapler", "validate", str(copy))
    verdict = sb.gatekeeper(tools, "--assess", "--type", "execute", "-vv", str(copy))
    return {"app": str(copy), "dmg": dmg.name, "dmgSha256": before, "quarantine": value, "gatekeeper": verdict}


def verify_output(run: Any, sb: Any) -> None:
    """--verify-only: every check of a finished output directory, the final artifact."""

    run.host()
    run.preflight_signer()
    dmgs = sorted(run.out.glob("HyprialOS_*_aarch64-signed.dmg"))
    apps = sorted((run.out / "app").glob("*.app"))
    if len(dmgs) != 1 or len(apps) != 1:
        raise sb.Refused(f"{run.out} is not a signed output directory (dmgs {len(dmgs)}, apps {len(apps)})")
    dmg, app = dmgs[0], apps[0]
    runtime = app / sb.RUNTIME
    tools = run.tools

    def signatures() -> None:
        tools.must("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))
        run.team = sb.audit(tools, app, "the app", team=None, runtime=True)
        inner = sb.signing_order(app)
        for path in inner:
            sb.audit(tools, path, path.relative_to(app).as_posix(), team=run.team, runtime=True)
        run.evidence["machoSigned"] = len(inner)
        work = Path(tempfile.mkdtemp(prefix="hsv"))
        try:
            sb.check_leaf(tools, app, run.identity, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        tools.must("codesign", "--verify", "--strict", "--verbose=2", str(dmg))
        sb.audit(tools, dmg, "the dmg", team=run.team, runtime=False)

    def stapled() -> None:
        tools.must("xcrun", "stapler", "validate", str(app))
        tools.must("xcrun", "stapler", "validate", str(dmg))

    def assess() -> None:
        run.evidence["gatekeeper"] = {
            "app": sb.gatekeeper(tools, "--assess", "--type", "execute", "-vv", str(app)),
            "dmg": sb.gatekeeper(tools, "--assess", "--type", "open", "--context", "context:primary-signature", "-vv", str(dmg)),
        }

    def contents() -> None:
        manifest = run.out / sb.MANIFEST_NAME
        expected = "".join(f"{sb._sha256(path)}  {path.name}\n" for path in (dmg, manifest))
        if (run.out / "SHA256SUMS").read_text(encoding="utf-8") != expected:
            raise sb.SignError("SHA256SUMS does not match the dmg and the manifest")
        if manifest.read_bytes() != (runtime / sb.MANIFEST_NAME).read_bytes():
            raise sb.SignError("runtime-manifest.json is not the signed app's manifest")
        problems, _ = sb.verify_runtime.check_manifest(runtime)
        if problems:
            raise sb.SignError(f"runtime manifest of the signed app does not verify: {problems[:5]}")
        if f"team id: {run.team}\n" not in (run.out / "BUILD-INFO.txt").read_text(encoding="utf-8"):
            raise sb.SignError("BUILD-INFO.txt does not name the Team ID of the signature")
        run.evidence["verifiedSha256"] = {name: sb._sha256(run.out / name) for name in (dmg.name, sb.MANIFEST_NAME, "SHA256SUMS")}

    def launch() -> None:
        parent = Path(tempfile.mkdtemp(prefix="hsb", dir=run.args.root_parent))
        try:
            code, line, _ = sb.runtime_check(runtime, parent)
        finally:
            shutil.rmtree(parent, ignore_errors=True)
        if code != 0:
            raise sb.SignError(f"launch check of the signed runtime failed: {line}")

    run.step("verify-signatures", signatures)
    run.step("stapler-validate", stapled)
    run.step("gatekeeper", assess)
    run.step("checksums", contents)
    run.step("runtime-launch", launch)
    if run.args.extract_to is not None:
        run.evidence["finalCopy"] = run.step("final-copy", lambda: extract(run, sb, dmg, run.args.extract_to.absolute()))
    run.evidence["verified"] = {"output": str(run.out), "dmg": dmg.name, "teamId": run.team}
    print(f"verified: {dmg.name}, app and dmg Developer ID signed, notarized, stapled, accepted by Gatekeeper")
