# Decisions (native actor engine stage, build/distribution ADR section 3):
# - initialize() first runs the native preflight (host-only target,
#   crates/ sources, abi3 feature and interpreter, a cargo), so a wheel that
#   cannot get its engine fails in seconds instead of after a Go cross-build
#   and `npm ci && npm run build`.  The native stage itself still runs after
#   tailcat and GUI so its cp312-abi3-<plat> tag is the final word; the tailcat stage's
#   py3-none-<plat> remains only for a wheel built with the explicit
#   HYPRIAL_BUILD_SKIP_NATIVE=1 opt-out.  The platform part comes from the
#   same tailcat target table (one target description for both payloads).
#   That declared platform tag also reaches cargo (macOS deployment target),
#   and the stage prints the SHA-256 of the module it force-includes.
# - The worker wheel (HYPRIAL_BUILD_WORKER_WHEEL=1) omits only the GUI and
#   the image-supplied sidecar; it keeps the native stage.
# - Editable builds verify an already staged src/hyprial/_core/actors.abi3.so
#   against its stamp and ABI version.  With HYPRIAL_BUILD_NATIVE=1 they first
#   rebuild a missing or stale module using the current build interpreter,
#   in-process through native_editable.build_editable (no shell script, so the
#   same path runs from PowerShell on Windows; KP-135 item 1).
#   Before building they touch scripts/build/native-engine.epoch: uv records
#   its cache-key state before the hook runs, so without that tracked file a
#   staged module deleted after the first build would match the recorded
#   "no module" state and uv would never re-run this hook.
# - The sdist carries crates/ through Hatch's default file selection; the
#   hook only checks the workspace manifest and lockfile are present, and
#   pyproject.toml excludes the cargo target directories.
# - Clean-source probes force core.fileMode=true on POSIX so a worktree chmod
#   cannot hide an edit.  On Windows (os.name == "nt") NTFS cannot represent
#   Git's executable bit: a checkout sees every tracked 100755 file as 100644
#   and the probe would refuse an otherwise clean target tree.  Windows runs
#   the same probes with core.fileMode=false; chmod is not a permission
#   boundary there, and content, index-flag and untracked-file guards remain
#   in force.  Verified by a Windows-only fix landing alongside the desktop
#   packaging line (2026-11-13).
"""Hatch adapter for the tailcat sidecar and the installed GUI assets.

The wheel carries three independent payloads:

* the Go sidecar under ``hyprial/_bin``;
* a *built* GUI snapshot under ``hyprial/_gui`` with ``release.json``; and
* the native actor engine ``hyprial/_core/actors.abi3.so`` (Windows:
  ``actors.pyd``), compiled from ``crates/`` by cargo.

The GUI snapshot is generated here from the exact clean Git tree being built.
Its manifest records the full source commit, the trusted source remote, the
package/GUI versions, and a SHA-256 for every served file.  Build failure is
fatal: an installed ``hyprial gui`` must never silently serve an empty or
fabricated asset directory.  The GUI build requires a tracked
``gui/package-lock.json`` and always runs a fresh ``npm ci --ignore-scripts``
(never reusing an existing ``node_modules``) before ``npm run build``.  The thin
worker wheel intentionally carries neither the GUI nor the sidecar, but it
does carry the native engine.

Decisions (2026-11-03, infra-ops/release-0.5.5.3.2, brief ``publish-gui``):

* The public snapshot is a ``git archive`` extraction with no ``.git``, so
  ``_stage_gui`` gains a checkout-free path gated by the publish stamp
  ``gui/gui-source.json`` that ``scripts/ci/github_publish.py`` writes into
  every snapshot carrying ``gui/`` (schema ``hyprial.gui-snapshot/v1``,
  writer ``scripts/ci/github_publish_policy/guistamp.py``).  The stamp binds
  every shipped ``gui/`` file by sha256 to the pinned internal commit and the
  publish target remote.
* The stamp replaces exactly the git-dependent checks (tracked inputs, clean
  source, ``rev-parse HEAD``, ``remote.origin.url``).  The lockfile
  requirement, fresh ``npm ci``, symlink rejection, dist checks, and the
  whole git-checkout path are unchanged -- no relaxation where ``.git``
  exists, and a stamp file in a checkout is just an untracked ``gui/`` file
  the existing clean-source check refuses.
* The stamp remote is the public mirror, never the builder's origin: an
  intranet ``remote.origin.url`` would leak infrastructure naming into the
  wheel's ``release.json``, which no publish gate scans.  The hook enforces
  the same no-credentials rule as the product manifest reader.

Decisions (2026-11-03, infra-ops/release-0.5.5.3.3, brief ``wheel-release``):

* ``.github/workflows/wheel-release.yml`` stamps the disposable build copy
  of ``VERSION`` to ``<base>+g<short sha>`` before ``uv build --wheel``,
  which the clean-source check rejected and red every wheel-release run
  since 10-30.  The git-checkout path now tolerates exactly one tracked
  modification -- ``VERSION`` whose content is exactly the committed value
  plus ``+g`` and a prefix of HEAD's full sha (>= 7 hex chars, any abbrev
  length git may produce).  Any other tracked change, and any other VERSION
  content, still fails; the ``gui/`` untracked-file rules are unchanged;
  the snapshot-stamp path never sees a checkout and is untouched.

Decisions (2026-11-16, release wheels and the GUI source remote):

* A checkout build records ``remote.origin.url`` as ``sourceRemote``, which
  is an intranet host for an internal builder and then reaches users through
  ``hyprial gui --json``.  ``HYPRIAL_BUILD_SOURCE_REMOTE`` is the one explicit,
  launcher-independent input that replaces it, read only here and only when
  building from a checkout.  It accepts exactly ``PUBLIC_SOURCE_REMOTE``
  (defined in ``shell/impl/gui/install/resources.py``, pinned equal to the
  snapshot stamp's publish target by a test); an empty, credential-bearing or
  any other value is a build error, as is setting it on a snapshot tree whose
  stamp already decides.
* Unset keeps the checkout remote so an internal developer's wheel still lets
  the installed ``hyprial gui dev`` fetch the exact ``sourceCommit`` from that
  remote; the stage prints a notice that such a wheel is for local use and must
  not be distributed.  ``sourceCommit`` is never altered by this variable.
* Every workflow step that builds a wheel which may be published sets the
  variable, and ``scripts/ci/verify_tailcat_wheels.py``,
  ``publish_wheels.py`` and ``attach_wheel_assets.py`` refuse a wheel whose
  ``release.json`` records another remote (decisions in
  ``.github/workflows/README.md``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_TAILCAT_TOOLS = runpy.run_path(
    str(Path(__file__).parent / "shell/impl/packaging/tailcat_wheel.py")
)
build_tailcat = _TAILCAT_TOOLS["build_tailcat"]
TailcatWheelError = _TAILCAT_TOOLS["TailcatWheelError"]
_GUI_TOOLS = runpy.run_path(
    str(Path(__file__).parent / "shell/impl/gui/install/resources.py")
)
_NATIVE_EDITABLE = runpy.run_path(
    str(Path(__file__).parent / "shell/impl/packaging/native_editable.py")
)
_NATIVE_TOOLS = _NATIVE_EDITABLE["ENGINE"]
NativeEngineError = _NATIVE_TOOLS["NativeEngineError"]
build_gui_manifest = _GUI_TOOLS["build_manifest"]
write_gui_manifest = _GUI_TOOLS["write_manifest"]
validate_gui_source_remote = _GUI_TOOLS["validate_source_remote"]
validate_build_source_remote = _GUI_TOOLS["validate_build_source_remote"]
redact_gui_remote = _GUI_TOOLS["redact_remote"]
PUBLIC_SOURCE_REMOTE = _GUI_TOOLS["PUBLIC_SOURCE_REMOTE"]
SOURCE_REMOTE_ENV = _GUI_TOOLS["SOURCE_REMOTE_ENV"]
GuiAssetError = _GUI_TOOLS["GuiAssetError"]

WHEEL_BIN_DIR = "hyprial/_bin"
WHEEL_GUI_DIR = "hyprial/_gui"
ALLOW_MISSING_ENV = "HYPRIAL_TAILCAT_ALLOW_MISSING"
WORKER_WHEEL_ENV = "HYPRIAL_BUILD_WORKER_WHEEL"
SKIP_NATIVE_ENV = _NATIVE_TOOLS["SKIP_ENV"]
BUILD_NATIVE_ENV = "HYPRIAL_BUILD_NATIVE"
_NATIVE_EPOCH = "scripts/build/native-engine.epoch"
_GUI_BUILD_TIMEOUT_SECONDS = 900.0
_GENERATED_GUI_DIRS = frozenset({"node_modules", "dist"})
_TRACKED_GUI_INPUTS = ("gui/package.json", "gui/package-lock.json")
SNAPSHOT_STAMP_SCHEMA = "hyprial.gui-snapshot/v1"
SNAPSHOT_STAMP_NAME = "gui-source.json"
_VERSION_WHITESPACE = re.compile(r"\s")
_HEX_ABBREV = re.compile(r"[0-9a-f]+")
def _trusted_git_config(platform_name: str | None = None) -> tuple[str, ...]:
    """Fixed, trusted Git semantics for every clean-source probe.

    A checkout's local config must not be able to hide edits from them:
    fsmonitor-valid bits are dropped when fsmonitor is off, and untracked
    caches are ignored.  POSIX keeps fileMode=true so chmod stays visible;
    Windows uses fileMode=false because NTFS cannot represent the executable
    bit (decision block at the top of this module).
    """
    name = os.name if platform_name is None else platform_name
    file_mode = "false" if name == "nt" else "true"
    return (
        "-c", "core.fsmonitor=false",
        "-c", f"core.fileMode={file_mode}",
        "-c", "core.untrackedCache=false",
        "-c", "diff.external=",
    )


def _trusted_git_env() -> dict[str, str]:
    """The caller's environment minus GIT_* (GIT_INDEX_FILE, GIT_DIR, ...).

    Threat model: these probes catch accidental dirt and the index/config
    states that hide it in ordinary checkouts.  A party that can rewrite the
    build host's git config, attributes or filters already controls the build
    and is out of scope.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
_STAMP_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_STAMP_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_generated_gui_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        normalized == f"gui/{name}" or normalized.startswith(f"gui/{name}/")
        for name in _GENERATED_GUI_DIRS
    )


def _stamp_relative_path_ok(path: str) -> bool:
    if not path or path != path.strip():
        return False
    if "\\" in path or path.startswith("/"):
        return False
    return all(
        part and part not in {".", ".."} and part not in _GENERATED_GUI_DIRS
        for part in path.split("/")
    )


def _porcelain_entries(output: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into (status, path) pairs."""
    items = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if not item:
            continue
        code = item[:2]
        path = item[3:] if len(item) > 3 else ""
        if code[:1] in {"R", "C"} and index < len(items):
            index += 1  # The source path follows a rename/copy entry.
        entries.append((code, path))
    return entries


def _editable_build_interpreter() -> str:
    """Return a stable interpreter path for the abi3 editable build."""

    candidate = getattr(sys, "_base_executable", "") or sys.executable
    return os.path.realpath(candidate)


class GuiWheelError(RuntimeError):
    """The wheel cannot carry a trustworthy GUI snapshot."""


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        self._validate_native_switches()
        root = Path(self.root)
        if version == "editable":
            self._verify_editable_native(root)
            return
        self._stages: list[tempfile.TemporaryDirectory] = []
        self._preflight_native(root)
        if os.environ.get(WORKER_WHEEL_ENV) != "1":
            self._stage_tailcat(root, build_data)
            if self.target_name == "wheel":
                self._stage_gui(root, build_data)
        self._stage_native(root, build_data)

    def _validate_native_switches(self) -> None:
        requested = os.environ.get(BUILD_NATIVE_ENV, "")
        skip = os.environ.get(SKIP_NATIVE_ENV, "")
        if requested and requested != "1":
            raise NativeEngineError(
                f"{BUILD_NATIVE_ENV} accepts only the value 1 when set; got {requested!r}"
            )
        if requested == "1" and skip == "1":
            raise NativeEngineError(
                f"{BUILD_NATIVE_ENV}=1 conflicts with {SKIP_NATIVE_ENV}=1"
            )

    def _preflight_native(self, root: Path) -> None:
        tools = _NATIVE_TOOLS
        if self.target_name != "wheel" or tools["skip_requested"]():
            return
        tailcat = _TAILCAT_TOOLS
        target = tailcat["resolve_target"]()
        if tools["prebuilt_directory"]() is not None:
            tools["verify_prebuilt"](
                root, target, platform_tag=tailcat["platform_tag"](target)
            )
            return
        tools["preflight"](root, target, host=tailcat["host_target"]())

    def _verify_editable_native(self, root: Path) -> None:
        tools = _NATIVE_TOOLS
        if tools["skip_requested"]():
            print(
                f"warning: {SKIP_NATIVE_ENV}=1 -- not verifying the editable "
                f"{tools['MODULE_NAME']} module",
                file=sys.stderr,
            )
            return
        python = _editable_build_interpreter()
        try:
            status = tools["verify_editable"](root, python=python)
        except NativeEngineError:
            if os.environ.get(BUILD_NATIVE_ENV) != "1":
                raise
            status = None
        if os.environ.get(BUILD_NATIVE_ENV) == "1" and status is None:
            self._build_editable_native(root, python)
            status = tools["verify_editable"](root, python=python)
        if status is None:
            print(
                f"notice: no {tools['EDITABLE_MODULE']} is staged; the native "
                "backend stays unavailable until `HYPRIAL_BUILD_NATIVE=1 uv sync` "
                "or scripts/build/build_native_engine.sh builds it",
                file=sys.stderr,
            )
            return
        print(status, file=sys.stderr)

    def _advance_native_epoch(self, root: Path) -> None:
        epoch = root / _NATIVE_EPOCH
        try:
            if epoch.is_file():
                os.utime(epoch)
            else:
                epoch.parent.mkdir(parents=True, exist_ok=True)
                epoch.write_text(
                    "mtime of this file marks the last editable native build\n",
                    encoding="utf-8",
                )
        except OSError as error:
            raise NativeEngineError(
                f"cannot update {epoch}: {error}; without it uv cannot notice a "
                "deleted staged module (recover with "
                "`HYPRIAL_BUILD_NATIVE=1 uv sync --reinstall-package hyprial`)"
            ) from error

    def _build_editable_native(self, root: Path, python: str) -> None:
        self._advance_native_epoch(root)
        module, library, stamp = _NATIVE_EDITABLE["build_editable"](root, python=python)
        print(f"staged {module} (from {library}); stamped {stamp}", file=sys.stderr)

    def _stage_native(self, root: Path, build_data: dict) -> None:
        tools = _NATIVE_TOOLS
        if tools["skip_requested"]():
            print(
                f"warning: {SKIP_NATIVE_ENV}=1 -- building without the native "
                f"actor engine {tools['MODULE_NAME']}; this {self.target_name} "
                "must not be published",
                file=sys.stderr,
            )
            return
        if self.target_name != "wheel":
            tools["require_sources"](root)
            return
        tailcat = _TAILCAT_TOOLS
        target = tailcat["resolve_target"]()
        declared = tailcat["platform_tag"](target)
        stage = tempfile.TemporaryDirectory(prefix="hyprial-native-")
        try:
            if tools["prebuilt_directory"]() is not None:
                module, glibc = tools["stage_prebuilt"](
                    root,
                    target,
                    output=Path(stage.name),
                    platform_tag=declared,
                )
            else:
                module, glibc = tools["build_extension"](
                    root,
                    target,
                    host=tailcat["host_target"](),
                    output=Path(stage.name),
                    platform_tag=declared,
                )
        except BaseException:
            stage.cleanup()
            raise
        self._stages.append(stage)
        platform_tag, warning = tools["platform_tag_for_floor"](declared, glibc)
        if warning:
            print(warning, file=sys.stderr)
        destination = tools["wheel_module_path"](target)
        digest = hashlib.sha256(Path(module).read_bytes()).hexdigest()
        print(f"{tools['MODULE_NAME']}: {destination} sha256 {digest}", file=sys.stderr)
        build_data.setdefault("force_include", {})[str(module)] = destination
        build_data["pure_python"] = False
        build_data["tag"] = tools["wheel_tag"](platform_tag)

    def _stage_tailcat(self, root: Path, build_data: dict) -> None:
        tools = _TAILCAT_TOOLS
        if self.target_name != "wheel":
            # LAX(tailnet-cutover): sdists ship the Go sources instead of a
            # binary; the formal arrangement is a from-source wheel build in CI
            # with the pinned toolchain, which is exactly what these sources
            # feed once the sdist is unpacked.
            source = root / tools["SIDECAR_SOURCE_DIR"]
            build_data.setdefault("force_include", {})[str(source)] = tools[
                "SIDECAR_SOURCE_DIR"
            ]
            return
        target = tools["resolve_target"]()
        stage = None
        try:
            stage = tempfile.TemporaryDirectory(prefix="hyprial-tailcat-")
            binary = build_tailcat(root, target, output=Path(stage.name))
        except TailcatWheelError as error:
            if stage is not None:
                stage.cleanup()
            if os.environ.get(ALLOW_MISSING_ENV) == "1":
                # LAX(tailnet-cutover): a Python-only development build may
                # have neither a Go toolchain nor a prebuilt sidecar; the
                # formal rule is that every published wheel carries one, so
                # release pipelines must not set this variable.
                print(
                    f"warning: {ALLOW_MISSING_ENV}=1 -- building a pure-Python "
                    f"wheel without the tailcat sidecar: {error}"
                )
                return
            raise
        self._stages.append(stage)
        build_data.setdefault("force_include", {})[
            str(binary)
        ] = f"{WHEEL_BIN_DIR}/{tools['binary_name'](target)}"
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{tools['platform_tag'](target)}"

    def _stage_gui(self, root: Path, build_data: dict) -> None:
        gui = root / "gui"
        package_file = gui / "package.json"
        lock_file = gui / "package-lock.json"
        if not package_file.is_file():
            raise GuiWheelError(f"GUI project is missing: {package_file}")
        if not lock_file.is_file():
            raise GuiWheelError(
                f"GUI dependency lockfile is missing: {lock_file}; run npm "
                "install and commit the lockfile so the wheel GUI build is "
                "reproducible"
            )
        checkout = (root / ".git").exists()
        remote_override = self._source_remote_override(checkout)
        if checkout:
            try:
                self._git(
                    root,
                    ["ls-files", "--error-unmatch", "--", *_TRACKED_GUI_INPUTS],
                )
            except GuiWheelError as error:
                raise GuiWheelError(
                    "GUI build inputs are not tracked at HEAD: "
                    f"{', '.join(_TRACKED_GUI_INPUTS)}"
                ) from error
            self._require_clean_source(root, gui)
        else:
            commit, remote = self._require_snapshot_stamp(gui)
        self._reject_symlinks(gui, skip_generated=True)

        npm = shutil.which("npm")
        if not npm:
            raise GuiWheelError("npm is required to build the wheel GUI assets")

        # Never reuse a previous node_modules or dist: the wheel must reflect
        # a fresh locked install and a build of the current committed source.
        self._remove_generated(gui / "node_modules")
        self._remove_generated(gui / "dist")
        self._run([npm, "ci", "--ignore-scripts"], cwd=gui, label="npm ci")
        self._run([npm, "run", "build"], cwd=gui, label="npm run build")

        dist = gui / "dist"
        self._reject_symlinks(dist, skip_generated=False)
        if not (dist / "index.html").is_file():
            raise GuiWheelError(
                f"GUI build did not produce {dist / 'index.html'}"
            )
        if checkout:
            self._require_clean_source(root, gui)
            commit = self._git(root, ["rev-parse", "HEAD"]).strip()
            remote = remote_override or self._git(
                root, ["config", "--get", "remote.origin.url"]
            ).strip()
            if remote != PUBLIC_SOURCE_REMOTE:
                print(
                    "notice: this wheel records the GUI source remote "
                    f"{redact_gui_remote(remote)}, which is not the public "
                    f"repository {PUBLIC_SOURCE_REMOTE}; it is for local use "
                    "and must not be distributed (a release build sets "
                    f"{SOURCE_REMOTE_ENV}={PUBLIC_SOURCE_REMOTE})",
                    file=sys.stderr,
                )
        gui_version = json.loads(package_file.read_text(encoding="utf-8")).get(
            "version", ""
        )
        if not isinstance(gui_version, str) or not gui_version:
            raise GuiWheelError("gui/package.json has no version")

        stage = tempfile.TemporaryDirectory(prefix="hyprial-gui-")
        self._stages.append(stage)
        staged = Path(stage.name) / "gui"
        shutil.copytree(dist, staged, symlinks=True)
        self._reject_symlinks(staged, skip_generated=False)
        manifest = build_gui_manifest(
            staged,
            package_version=str(self.metadata.version),
            source_commit=commit,
            source_remote=remote,
            gui_version=gui_version,
        )
        write_gui_manifest(staged, manifest)
        force_include = build_data.setdefault("force_include", {})
        for path in sorted(staged.rglob("*")):
            if path.is_symlink():
                raise GuiWheelError(
                    "the staged GUI payload contains a symbolic link; "
                    f"refusing to follow it: {path}"
                )
            if not path.is_file():
                continue
            relative = path.relative_to(staged).as_posix()
            force_include[str(path)] = f"{WHEEL_GUI_DIR}/{relative}"

    def _source_remote_override(self, checkout: bool) -> str | None:
        if SOURCE_REMOTE_ENV not in os.environ:
            return None
        if not checkout:
            raise GuiWheelError(
                f"{SOURCE_REMOTE_ENV} is set but this is a snapshot tree "
                "without .git: its GUI provenance stamp already decides the "
                "source remote; unset the variable"
            )
        value = os.environ[SOURCE_REMOTE_ENV]
        try:
            validate_build_source_remote(value)
        except GuiAssetError as error:
            raise GuiWheelError(f"{SOURCE_REMOTE_ENV} is not accepted: {error}") from error
        return value

    def _require_snapshot_stamp(self, gui: Path) -> tuple[str, str]:
        """Validate the publish stamp and return its ``(commit, remote)``.

        A snapshot tree has no ``.git``, so the only accepted provenance is
        the stamp ``scripts/ci/github_publish.py`` writes into every snapshot
        carrying ``gui/``.  Validation is fail-closed and byte-exact: the
        stamp must list EVERY regular file under ``gui/`` (the stamp itself
        and the generated ``node_modules``/``dist`` trees aside) and every
        listed file must hash-match on disk, so inputs cannot drift between
        the scanned snapshot and this build.
        """
        path = gui / SNAPSHOT_STAMP_NAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise GuiWheelError(
                "GUI provenance stamp is missing: "
                f"{path}; this tree has no .git checkout, so only the publish "
                "snapshot stamp can attest the GUI inputs"
            ) from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GuiWheelError(
                f"cannot read the GUI provenance stamp {path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise GuiWheelError(
                "the GUI provenance stamp must contain a JSON object"
            )
        if payload.get("schema") != SNAPSHOT_STAMP_SCHEMA:
            raise GuiWheelError(
                "unsupported GUI provenance stamp schema "
                f"{payload.get('schema')!r}; expected {SNAPSHOT_STAMP_SCHEMA!r}"
            )
        commit = payload.get("commit")
        if not isinstance(commit, str) or not _STAMP_COMMIT_RE.fullmatch(commit):
            raise GuiWheelError(
                "the GUI provenance stamp commit must be a full 40-character "
                f"Git object id: {commit!r}"
            )
        remote = payload.get("remote")
        if not isinstance(remote, str) or not remote.strip():
            raise GuiWheelError(
                "the GUI provenance stamp remote must be a non-empty string"
            )
        try:
            validate_gui_source_remote(remote)
        except GuiAssetError as error:
            raise GuiWheelError(
                f"the GUI provenance stamp remote is not publishable: {error}"
            ) from error
        entries = payload.get("files")
        if not isinstance(entries, list) or not entries:
            raise GuiWheelError(
                "the GUI provenance stamp must list the gui/ files it attests"
            )
        stamped: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise GuiWheelError(
                    "GUI provenance stamp entries must be JSON objects"
                )
            relative = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(relative, str) or not _stamp_relative_path_ok(
                relative
            ):
                raise GuiWheelError(
                    "GUI provenance stamp contains an unsafe file path: "
                    f"{relative!r}"
                )
            if not isinstance(digest, str) or not _STAMP_DIGEST_RE.fullmatch(
                digest
            ):
                raise GuiWheelError(
                    f"GUI provenance stamp has an invalid sha256 for {relative}"
                )
            if relative in stamped:
                raise GuiWheelError(
                    f"the GUI provenance stamp lists {relative} twice"
                )
            stamped[relative] = digest
        self._verify_stamp_covers_tree(gui, stamped)
        return commit, remote

    def _verify_stamp_covers_tree(
        self, gui: Path, stamped: dict[str, str]
    ) -> None:
        """Refuse unless stamp and tree describe exactly the same bytes."""
        actual: dict[str, str] = {}
        errors: list[OSError] = []
        for directory, dirnames, filenames in os.walk(
            gui, followlinks=False, onerror=errors.append
        ):
            current = Path(directory)
            if current == gui:
                dirnames[:] = sorted(
                    name for name in dirnames if name not in _GENERATED_GUI_DIRS
                )
            for name in sorted(filenames):
                path = current / name
                relative = path.relative_to(gui).as_posix()
                if relative == SNAPSHOT_STAMP_NAME:
                    continue
                try:
                    data = path.read_bytes()
                except OSError as error:
                    raise GuiWheelError(
                        f"cannot read the GUI snapshot input {path}: {error}"
                    ) from error
                actual[relative] = hashlib.sha256(data).hexdigest()
        if errors:
            raise GuiWheelError(
                f"cannot inspect the GUI tree {gui}: {errors[0]}"
            )
        absent = sorted(set(stamped) - set(actual))
        if absent:
            raise GuiWheelError(
                "the GUI provenance stamp lists files missing from the tree: "
                + ", ".join(absent[:5])
            )
        changed = sorted(
            relative
            for relative, digest in stamped.items()
            if relative in actual and actual[relative] != digest
        )
        if changed:
            raise GuiWheelError(
                "GUI snapshot inputs changed after the provenance stamp: "
                + ", ".join(changed[:5])
            )
        extra = sorted(set(actual) - set(stamped))
        if extra:
            raise GuiWheelError(
                "GUI snapshot files the provenance stamp does not cover: "
                + ", ".join(extra[:5])
            )

    def _require_clean_source(self, root: Path, gui: Path) -> None:
        self._reject_hidden_index_state(root)
        if not self._git_ok(root, ["diff", "--quiet", "HEAD", "--"]):
            if not self._wheel_release_stamp_is_only_change(root):
                raise GuiWheelError(
                    "the source tree has tracked modifications; a wheel GUI "
                    "snapshot must identify the exact clean Git tree"
                )
        output = self._git_output(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all",
             "--", "gui"],
        )
        for code, path in _porcelain_entries(output):
            if code == "??" and _is_generated_gui_path(path):
                continue
            raise GuiWheelError(
                "the GUI source has uncommitted or untracked files "
                f"({path or '(empty path)'}); a wheel GUI snapshot must contain "
                "only the committed source plus generated node_modules/dist"
            )

    def _reject_hidden_index_state(self, root: Path) -> None:
        """Refuse index state that ``git diff HEAD`` cannot see.

        assume-unchanged / skip-worktree entries hide worktree edits from the
        diff, and index-only mode or type changes (chmod, gitlink) are not
        visible in a HEAD-vs-worktree diff.  The only index change tolerated
        is a content-only (same mode) update of VERSION, which the stamp
        check below then validates against the worktree bytes.
        """
        flags = self._git_output(root, ["ls-files", "-v", "-z"])
        for entry in (item for item in flags.split("\0") if item):
            tag, _, path = entry.partition(" ")
            if tag.islower() or tag == "S":
                raise GuiWheelError(
                    "the source tree has tracked modifications; index entry "
                    f"{path!r} is marked assume-unchanged or skip-worktree"
                )
        raw = self._git_output(
            root, ["diff", "--cached", "--raw", "--no-renames", "-z", "HEAD", "--"]
        )
        parts = [item for item in raw.split("\0") if item]
        for header, path in zip(parts[0::2], parts[1::2]):
            fields = header.lstrip(":").split(" ")
            same_mode = len(fields) >= 2 and fields[0] == fields[1]
            if path != "VERSION" or not same_mode or fields[0] not in ("100644", "100755"):
                raise GuiWheelError(
                    "the source tree has tracked modifications; a wheel GUI "
                    "snapshot must identify the exact clean Git tree"
                )

    def _wheel_release_stamp_is_only_change(self, root: Path) -> bool:
        output = self._git_output(
            root,
            ["diff", "--name-only", "--no-renames", "-z", "HEAD", "--"],
        )
        modified = [name for name in output.split("\0") if name]
        if modified != ["VERSION"]:
            return False
        # Content only: a mode change (e.g. chmod 0755) on VERSION is not the
        # wheel-release stamp and must stay a tracked modification.
        raw = self._git_output(
            root, ["diff", "--raw", "--no-renames", "-z", "HEAD", "--", "VERSION"]
        )
        modes = raw.split("\0", 1)[0].lstrip(":").split(" ")
        if len(modes) < 2 or modes[0] != modes[1]:
            return False
        try:
            committed_raw = self._git(root, ["show", "HEAD:VERSION"])
        except GuiWheelError:
            return False
        # Byte-exact: wheel-release writes ``printf '%s\n' "<base>+g<sha>"``
        # where <base> is the committed value with whitespace removed.  No
        # stripping here, so whitespace variants of a valid stamp are refused.
        committed = committed_raw[:-1] if committed_raw.endswith("\n") else committed_raw
        if not committed or _VERSION_WHITESPACE.search(committed):
            return False
        stamped = (root / "VERSION").read_bytes()
        head = self._git(root, ["rev-parse", "HEAD"]).strip()
        prefix = f"{committed}+g".encode("ascii", "strict")
        if not (stamped.startswith(prefix) and stamped.endswith(b"\n")):
            return False
        abbrev = stamped[len(prefix):-1].decode("ascii", "replace")
        return (
            len(abbrev) >= 7
            and _HEX_ABBREV.fullmatch(abbrev) is not None
            and head.startswith(abbrev)
        )

    def _reject_symlinks(self, root: Path, *, skip_generated: bool) -> None:
        if root.is_symlink():
            raise GuiWheelError(
                "the GUI tree contains a symbolic link; refusing to follow it: "
                f"{root}"
            )
        if not root.exists():
            return
        errors: list[OSError] = []
        for directory, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=errors.append
        ):
            current = Path(directory)
            for name in list(dirnames):
                path = current / name
                if path.is_symlink():
                    raise GuiWheelError(
                        "the GUI tree contains a symbolic link; refusing to "
                        f"follow it: {path}"
                    )
                if skip_generated and current == root and name in _GENERATED_GUI_DIRS:
                    dirnames.remove(name)
            for name in filenames:
                path = current / name
                if path.is_symlink():
                    raise GuiWheelError(
                        "the GUI tree contains a symbolic link; refusing to "
                        f"follow it: {path}"
                    )
        if errors:
            raise GuiWheelError(
                f"cannot inspect the GUI tree {root}: {errors[0]}"
            ) from errors[0]

    def _remove_generated(self, path: Path) -> None:
        if path.is_symlink():
            raise GuiWheelError(
                "the GUI tree contains a symbolic link; refusing to follow it: "
                f"{path}"
            )
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as error:
            raise GuiWheelError(
                f"cannot remove generated GUI path {path}: {error}"
            ) from error

    def _run(self, argv: list[str], *, cwd: Path, label: str) -> None:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                check=False,
                capture_output=True,
                text=True,
                timeout=_GUI_BUILD_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GuiWheelError(f"{label} failed to start: {error}") from error
        if completed.returncode != 0:
            detail = "\n".join(
                part.strip()
                for part in (completed.stdout, completed.stderr)
                if part and part.strip()
            )[-1200:]
            raise GuiWheelError(
                f"{label} failed with exit {completed.returncode}: {detail}"
            )

    def _git_result(self, root: Path, args: list[str]):
        try:
            return subprocess.run(
                ["git", "-C", str(root), *_trusted_git_config(), *args],
                env=_trusted_git_env(),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GuiWheelError(f"git {' '.join(args)} failed: {error}") from error

    def _git(self, root: Path, args: list[str]) -> str:
        completed = self._git_result(root, args)
        if completed.returncode != 0:
            raise GuiWheelError(
                f"git {' '.join(args)} failed with exit {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[-500:]}"
            )
        return completed.stdout or ""

    def _git_output(self, root: Path, args: list[str]) -> str:
        """Raw stdout for NUL-delimited status output (no stripping)."""
        completed = self._git_result(root, args)
        if completed.returncode != 0:
            raise GuiWheelError(
                f"git {' '.join(args)} failed with exit {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[-500:]}"
            )
        return completed.stdout or ""

    def _git_ok(self, root: Path, args: list[str]) -> bool:
        return self._git_result(root, args).returncode == 0

    def finalize(self, version, build_data, artifact_path):
        for stage in getattr(self, "_stages", []):
            stage.cleanup()
