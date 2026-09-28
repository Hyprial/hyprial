"""Tag-governed Git update resolution with opt-in upgrade tracks.

Distribution is deliberately Git/uv based (``uv tool install git+...@<ref>``)
with no package registry. The persistent remote release is the public GitHub
repository; PEP 610 installation origin is reporting data only. Two resolution
modes share one probe:

- Legacy (default): the highest parseable ``vX.Y.Z...`` tag by PEP 440
  ordering, including development and release-candidate tags.  This is the
  exact pre-track behavior; an installation without an explicit track keeps
  it byte-for-byte.
- Track (opt-in): ``settings.json`` ``updateTrack`` set to ``internal``,
  ``nightly``, or ``stable`` resolves the movable tag of the same name —
  ``internal`` follows the dev branch after a green unit run, ``nightly`` the
  last dual-gate-green nightly, ``stable`` the last manual release.  A
  missing track tag is a loud error and NEVER falls back to the latest
  version tag: a fallback would silently change which code a machine
  tracks.  Track installs pin the resolved commit (``git+...@<commit>``)
  because a movable tag can shift between resolution and uv's fetch.

Branches never participate in resolution.  The installed commit is recovered
from the PEP 610 ``direct_url.json`` that uv writes into the installed
dist-info.  Legacy snake_case ``update_track`` values are detected only to
warn operators; they are never parsed, persisted, or used to choose an
update target.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.metadata import distribution as package_distribution
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

Json = dict[str, Any]

_LOG = logging.getLogger(__name__)

OFFICIAL_GIT_URL = "https://github.com/Hyprial/hyprial.git"
FORGEJO_GIT_URL = "ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git"
# Shared repository consumers (notably the generated agent Git profile) use the
# internal SSH endpoint. Upgrade policy has its own official-source constant
# above and must not change this compatibility default.
DEFAULT_GIT_URL = FORGEJO_GIT_URL
UPGRADE_SOURCES = {"official": OFFICIAL_GIT_URL, "forgejo": FORGEJO_GIT_URL}
# One cold SSH handshake on the hq Mac took 21.0s on 2026-09-27. Keep enough
# headroom for that first connection while bounding both attempts to 90s total.
LS_REMOTE_TIMEOUT = 45.0
LS_REMOTE_RETRY_COUNT = 1
LS_REMOTE_TIMEOUT_SETTINGS_KEY = "lsRemoteTimeoutSeconds"
LS_REMOTE_TIMEOUT_KEY_ALIASES = (
    LS_REMOTE_TIMEOUT_SETTINGS_KEY,
    "ls-remote-timeout",
)
# A full ``uv tool install`` of a git source can take minutes on a cold cache.
UV_INSTALL_TIMEOUT = 300.0
# ``uv tool dir`` is a directory listing (verified read-only on uv 0.11.7:
# no mkdir side effect), so it answers in well under a second; the timeout
# only bounds a wedged PATH lookup.
UV_TOOL_DIR_PROBE_TIMEOUT = 10.0

_TAG_REF = re.compile(r"^refs/tags/(.+?)(\^\{\})?$")

#: The only values ``settings.json`` ``updateTrack`` accepts; each names the
#: movable remote tag that track resolves.  Absent key = legacy latest-tag.
#:
#: ``dev`` was renamed to ``internal`` on 2026-09-18 (Allen): the internal tag
#: shared its name with the ``dev`` BRANCH, and Forgejo resolves a bare ``dev``
#: to the tag when computing a merge base -- which silently based two merges on
#: a stale commit. The rename removes the collision at its source.
TRACK_TAGS = ("internal", "nightly", "stable")

#: Retired track names that are still ACCEPTED, mapped to their replacement.
#:
#: Why accept them at all, when a missing track tag is deliberately a loud
#: error: the loudness is there to stop a machine silently following different
#: code. That reasoning does not apply to a machine whose settings still say
#: ``dev`` -- it wants exactly the track it always wanted, under the name it was
#: told to use. Refusing it would punish the one group that did nothing, and it
#: would do so at the worst moment: right after an upgrade, on a machine whose
#: operator is not watching.
#:
#: ⚠️ This is a migration alias, not a second name. It resolves to the CURRENT
#: tag, so the retired tag can be deleted the moment every node runs a client
#: that has this table. Remove the entry once no node reports ``updateTrack:
#: dev`` -- and the removal is the point: an alias with no removal condition is
#: how the previous rename ended up half-applied for two days.
DEPRECATED_TRACK_ALIASES = {"dev": "internal"}


def canonical_track(value: str) -> str | None:
    """Map a configured track name to its current tag, or None if unknown.

    Returns the value itself when it is current, the replacement when it is a
    retired-but-accepted alias, and None when it is neither -- callers decide
    whether that is an error (resolution) or a finding (doctor).
    """

    if value in TRACK_TAGS:
        return value
    return DEPRECATED_TRACK_ALIASES.get(value)


class UpdateProbeError(RuntimeError):
    """The remote tag or version could not be determined."""


@dataclass(frozen=True, slots=True)
class Installation:
    """What the current hyprial install is, per uv's packaging metadata."""

    version: str | None
    commit: str | None
    requested_revision: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class RemoteResolution:
    """One exact remote tag and the commit it names."""

    tag: str
    commit: str
    version: str | None = None


def installation_git_url(installation: Installation) -> str:
    """Return the persistent update source.

    ``direct_url.json`` describes where the currently installed bytes came
    from. It is reporting data, never an update-policy input. Plain upgrades
    always return to the official public release.
    """

    del installation
    return OFFICIAL_GIT_URL


def installation_origin(installation: Installation) -> str | None:
    """Return the PEP 610 origin exactly as recorded for reporting."""

    return installation.url


def installation_is_local(installation: Installation) -> bool:
    """True when this distribution was installed from a local path.

    Guard 2 of spec autoupdate-isolated-home-2026-09-15: a ``file://`` (or
    bare-path, or ``git+file://``, or editable) install source means this
    process runs from a working tree. The persistent remote is still official,
    but self-replacing a developer checkout's interpreter would overwrite the
    user's global uv tool directory. This safety guard reads the raw origin;
    it never chooses the remote.
    """

    url = installation.url
    if not url:
        return False
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    return scheme in ("", "file") or scheme.endswith("+file")


def read_installation(dist_name: str = "hyprial") -> Installation:
    """Read the installed distribution version and PEP 610 VCS origin."""

    try:
        dist = package_distribution(dist_name)
    except PackageNotFoundError:
        return Installation(
            version=None, commit=None, requested_revision=None, url=None
        )
    version = dist.version
    try:
        raw = dist.read_text("direct_url.json")
    except OSError:
        raw = None
    if not raw:
        return Installation(
            version=version, commit=None, requested_revision=None, url=None
        )
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return Installation(
            version=version, commit=None, requested_revision=None, url=None
        )
    if not isinstance(record, dict):
        return Installation(
            version=version, commit=None, requested_revision=None, url=None
        )
    vcs_info = record.get("vcs_info")
    commit = None
    requested = None
    if isinstance(vcs_info, dict) and vcs_info.get("vcs") == "git":
        # ⚠️ `commit_id` is the anchor the upgrade decision uses: it is compared
        # against whatever the configured track tag resolves to *now*.  The
        # `rev=` in uv's receipt -- and `requested_revision` below -- is only a
        # record of what was installed and takes no part in that decision.
        # Pinning a resolved commit rather than a tag name is deliberate and
        # has two separate reasons; both are written down, together with the
        # regression tests that hold them, in docs/upgrade-tracks.md under
        # "客户端行为".
        #
        # This pointer is here rather than in that document because the
        # document already said all of it: on 2026-08-30 two people read the
        # receipt, reasoned from `rev=`, and concluded that production could no
        # longer auto-upgrade -- neither of them finding that page. The gap was
        # never the writing; nothing pointed here from where the mistake gets
        # made.
        raw_commit = vcs_info.get("commit_id")
        commit = raw_commit if isinstance(raw_commit, str) and raw_commit else None
        raw_requested = vcs_info.get("requested_revision")
        requested = (
            raw_requested if isinstance(raw_requested, str) and raw_requested else None
        )
    raw_url = record.get("url")
    url = raw_url if isinstance(raw_url, str) and raw_url else None
    return Installation(
        version=version, commit=commit, requested_revision=requested, url=url
    )


def read_update_track(hyprial_home: Path) -> str | None:
    """Return the explicit ``settings.json`` ``updateTrack``, or ``None``.

    ``None`` keeps the legacy latest-version-tag resolution byte-for-byte;
    the three-track semantics activate only on an explicit opt-in value.  A
    malformed ``settings.json`` or an out-of-set value fails loudly instead
    of guessing: silently substituting a track (or the legacy path) would
    change which code a machine upgrades to, the exact failure class the
    track field exists to govern.
    """

    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateProbeError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise UpdateProbeError(f"cannot parse {path}: top level is not an object")
    track = record.get("updateTrack")
    if track is None:
        return None
    resolved = canonical_track(str(track))
    if resolved is None:
        raise UpdateProbeError(
            f"{path} updateTrack must be one of "
            f"{', '.join(TRACK_TAGS)}; got {track!r}"
        )
    if resolved != track:
        # stderr, not a logger: this module deliberately has no logging
        # dependency (it is a probe), and stderr is where a CLI's warnings go.
        # Loud enough to be seen, quiet enough not to fail a run that is doing
        # exactly what it was configured to do.
        print(
            f"warning: updateTrack {track!r} was renamed to {resolved!r}; "
            f"update {path} -- the alias will be removed",
            file=sys.stderr,
        )
    return resolved


def _valid_ls_remote_timeout(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timeout = float(value)
    return timeout if math.isfinite(timeout) and timeout > 0 else None


def read_ls_remote_timeout(hyprial_home: Path) -> float:
    """Return the configured tag-probe budget, or the cold-SSH-safe default."""

    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LS_REMOTE_TIMEOUT
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateProbeError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise UpdateProbeError(f"cannot parse {path}: top level is not an object")
    raw_timeout = record.get(LS_REMOTE_TIMEOUT_SETTINGS_KEY, LS_REMOTE_TIMEOUT)
    timeout = _valid_ls_remote_timeout(raw_timeout)
    if timeout is None:
        raise UpdateProbeError(
            f"{path} {LS_REMOTE_TIMEOUT_SETTINGS_KEY} must be a positive "
            f"finite number; got {raw_timeout!r}"
        )
    return timeout


def write_ls_remote_timeout(timeout: float, hyprial_home: Path) -> Path:
    """Persist the tag-probe budget without replacing unrelated settings."""

    valid_timeout = _valid_ls_remote_timeout(timeout)
    if valid_timeout is None:
        raise ValueError(
            f"{LS_REMOTE_TIMEOUT_SETTINGS_KEY} must be a positive finite number; "
            f"got {timeout!r}"
        )
    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    record[LS_REMOTE_TIMEOUT_SETTINGS_KEY] = valid_timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def retired_track_warning(hyprial_home: Path) -> str | None:
    """Return one compatibility warning for legacy or broken track config.

    ``settings.json`` ``updateTrack`` is the live track selector; everything
    else is residue: the snake_case spelling, an unreadable
    ``settings.json``, or an out-of-set value.  The last two also make ``hyprial
    upgrade`` refuse, and the warning previews that.  This is intentionally
    read-only: ``hyprial version`` must remain an observation command, and
    deleting a legacy key could destroy unrelated old-client configuration.

    ⚠️ Every finding here is about ``settings.json``.  The legacy
    ``config.json`` is not read -- not even to warn -- so its presence or
    absence cannot change this result.  Do not reintroduce a read of it "just
    to warn": that is how it survived as a live input long after the
    TypeScript client that wrote it was gone.
    """

    findings: list[str] = []
    settings = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(settings.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = None
    except (OSError, json.JSONDecodeError):
        findings.append(
            "settings.json is unreadable; hyprial upgrade will refuse until it parses"
        )
        record = None
    if isinstance(record, dict):
        if "update_track" in record:
            findings.append(
                "update_track in settings.json is retired; use updateTrack"
            )
        value = record.get("updateTrack")
        if value is not None:
            resolved = canonical_track(str(value))
            if resolved is None:
                findings.append(
                    f"updateTrack {value!r} is not one of "
                    f"{', '.join(TRACK_TAGS)}; "
                    "hyprial upgrade will refuse until it is fixed"
                )
            elif resolved != value:
                findings.append(
                    f"updateTrack {value!r} was renamed to {resolved!r}; "
                    "upgrade still works through a deprecation alias, but set "
                    "the new name -- the alias will be removed"
                )
    if not findings:
        return None
    return "; ".join(findings)


#: ``settings.json`` key that gates every automatic self-upgrade (spec
#: autoupdate-isolated-home r1, Allen 2026-09-15: "autoupdate 我建议在代码中
#: 默认设置为关闭,有必要用户可以自行打开或请agent打开 hyprial config set
#: auto-upgrade true或者类似的方式").  Absent key = disabled.  CamelCase to
#: match the neighbouring ``updateTrack``.
AUTOUPGRADE_SETTINGS_KEY = "autoUpgrade"

#: Spellings ``hyprial config set`` accepts for the switch above: the
#: canonical settings.json key plus Allen's dashed spelling.
AUTOUPGRADE_KEY_ALIASES = (AUTOUPGRADE_SETTINGS_KEY, "auto-upgrade")

#: The skip reason recorded (scheduler event and ``autoupdate run`` last-run
#: record) when an automatic upgrade is due but the switch is off.
AUTOUPGRADE_DISABLED_REASON = "disabled"


def auto_upgrade_enabled(hyprial_home: Path) -> bool:
    """True only when ``settings.json`` explicitly sets ``autoUpgrade: true``.

    Default OFF, fail-closed on every ambiguity: a missing file, a missing
    key, a non-``true`` value, an unparseable file, or a non-object top
    level all mean disabled.  Unlike :func:`read_update_track` this never
    raises -- the scheduler asks this question on every timer fire, and a
    corrupt settings.json must silence the automatic upgrade, not crash the
    scheduler thread (a machine that cannot prove it opted in does not
    upgrade itself).
    """

    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    return record.get(AUTOUPGRADE_SETTINGS_KEY) is True


def write_auto_upgrade(enabled: bool, hyprial_home: Path) -> Path:
    """Persist the ``autoUpgrade`` switch and return the file written.

    Read-modify-write (the ``write_settings_owner`` discipline): one key is
    replaced, every unrelated key survives.  A settings.json that does not
    parse is REFUSED, not clobbered -- rewriting over an unparseable file
    would destroy the rest of the operator's configuration to set one flag.
    """

    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    record[AUTOUPGRADE_SETTINGS_KEY] = bool(enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_uv_tool_dir(
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> Json:
    """Resolve where ``uv tool install`` will write (uv's own precedence).

    Guard 3 of spec autoupdate-isolated-home-2026-09-15.  Measured on
    uv 0.11.7 (read-only probes, no install):

    1. ``UV_TOOL_DIR`` set  -> uv uses exactly that directory (even a
       relative value is accepted and printed absolute).
    2. else ``XDG_DATA_HOME`` set -> ``$XDG_DATA_HOME/uv/tools`` (respected
       on macOS as well).
    3. else ``$HOME/.local/share/uv/tools``.

    The resolution mirrors that precedence: explicit env first, then ask
    ``uv tool dir`` (the authoritative answer, validated to be an absolute
    path so garbage output cannot masquerade as a tool directory), then the
    same convention uv falls back to.  ``toolDirSource`` names which branch
    won, for the refusal record; ``unresolved`` means even the convention
    could not produce a directory, and the caller must refuse (fail closed).
    """

    env = os.environ if environ is None else environ
    explicit = env.get("UV_TOOL_DIR")
    if explicit:
        return {
            "toolDir": str(Path(explicit).expanduser()),
            "toolDirSource": "UV_TOOL_DIR",
        }
    run = runner if runner is not None else subprocess.run
    try:
        completed = run(
            ["uv", "tool", "dir"],
            text=True,
            capture_output=True,
            check=False,
            timeout=UV_TOOL_DIR_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        first = (completed.stdout or "").strip().splitlines()
        if first and os.path.isabs(first[0].strip()):
            return {
                "toolDir": first[0].strip(),
                "toolDirSource": "uv-tool-dir",
            }
    xdg = env.get("XDG_DATA_HOME")
    if xdg:
        conventional = Path(xdg).expanduser() / "uv" / "tools"
    elif env.get("HOME"):
        conventional = Path(env["HOME"]) / ".local" / "share" / "uv" / "tools"
    else:
        return {"toolDir": None, "toolDirSource": "unresolved"}
    return {
        "toolDir": str(conventional),
        "toolDirSource": "convention",
    }


def _prefix_within(prefix: str, tool_dir: Path) -> bool:
    """True when ``prefix`` is ``tool_dir`` itself or lives beneath it.

    Both sides are ``resolve()``d so symlinked spellings (/tmp vs
    /private/tmp) compare equal regardless of how each side was obtained.
    """

    try:
        resolved_prefix = Path(prefix).resolve()
        resolved_root = Path(tool_dir).expanduser().resolve()
    except OSError:
        return False
    return resolved_prefix == resolved_root or resolved_root in resolved_prefix.parents


def uv_tool_dir_guard(
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> Json:
    """Decide whether THIS process may run ``uv tool install``.

    Allowed only when the running interpreter's ``sys.prefix`` sits inside
    the directory uv would write -- i.e. the tool being upgraded is this
    very installation, not some other environment reaching into the user's
    global tool directory.  Both sides are ``resolve()``d so symlinked
    spellings (/tmp vs /private/tmp) compare equal.  This is the backstop
    guard: it fires even when the scheduler-level and source-level guards
    were bypassed, which is why it lives at the single ``uv tool install``
    call site shared by ``hyprial upgrade`` and the autoupdate child.
    """

    resolution = resolve_uv_tool_dir(environ, runner)
    tool_dir = resolution["toolDir"]
    prefix = str(Path(sys.prefix).resolve())
    allowed = tool_dir is not None and _prefix_within(prefix, Path(tool_dir))
    return {
        "allowed": allowed,
        "toolDir": str(tool_dir) if tool_dir is not None else None,
        "toolDirSource": resolution["toolDirSource"],
        "sysPrefix": prefix,
    }


def git_env() -> dict[str, str]:
    """Subprocess environment for any git-touching command (``uv`` included).

    Headless callers — the update timer above all — must never hang on a
    credential prompt, so git terminal prompting is disabled and ssh runs in
    BatchMode.  An inherited ``GIT_SSH_COMMAND`` is augmented, not replaced:
    ssh still reads ``~/.ssh/config``, so operator-configured identity flags
    keep working while BatchMode is guaranteed.
    """

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    ssh_command = env.get("GIT_SSH_COMMAND") or "ssh"
    if "BatchMode=yes" not in ssh_command:
        ssh_command = f"{ssh_command} -o BatchMode=yes"
    env["GIT_SSH_COMMAND"] = ssh_command
    return env


def _run_git(
    args: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    runner: object = None,
) -> str:
    run = runner if runner is not None else subprocess.run
    try:
        completed = run(  # type: ignore[operator]
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except OSError as error:
        raise UpdateProbeError(f"cannot run git: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UpdateProbeError(f"git {' '.join(args[:2])} failed: {detail}")
    return completed.stdout


def export_locked_constraints(
    url: str,
    commit: str,
    destination: Path,
    *,
    runner: object = None,
) -> Path:
    """Fetch one exact source commit and export its lock as constraints.

    The checkout is disposable and source-specific. The local checkout's
    lockfile is never consulted, and every subprocess is injectable so tests
    can prove the install path without network access.
    """

    run = runner if runner is not None else subprocess.run
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hyprial-upgrade-lock-") as raw:
        checkout = Path(raw) / "source"
        checkout.mkdir()
        _run_git(["init", "--quiet"], timeout=UV_INSTALL_TIMEOUT, cwd=checkout, runner=run)
        _run_git(
            ["remote", "add", "origin", url],
            timeout=UV_INSTALL_TIMEOUT,
            cwd=checkout,
            runner=run,
        )
        _run_git(
            ["fetch", "--quiet", "--depth", "1", "origin", commit],
            timeout=UV_INSTALL_TIMEOUT,
            cwd=checkout,
            runner=run,
        )
        _run_git(
            ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
            timeout=UV_INSTALL_TIMEOUT,
            cwd=checkout,
            runner=run,
        )
        fetched = _run_git(
            ["rev-parse", "HEAD"],
            timeout=UV_INSTALL_TIMEOUT,
            cwd=checkout,
            runner=run,
        ).strip()
        if fetched.lower() != commit.lower():
            raise UpdateProbeError(
                "fetched source commit did not match the resolved commit"
            )
        if not (checkout / "uv.lock").is_file():
            raise UpdateProbeError(
                "resolved source commit does not contain uv.lock; "
                "refusing an unpinned install"
            )
        argv = [
            "uv",
            "export",
            "--frozen",
            "--no-hashes",
            "--no-emit-project",
            "--no-dev",
            "--project",
            str(checkout),
            "--output-file",
            str(destination),
        ]
        try:
            completed = run(  # type: ignore[operator]
                argv,
                text=True,
                capture_output=True,
                check=False,
                env=git_env(),
                timeout=UV_INSTALL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise UpdateProbeError(
                f"uv export timed out after {UV_INSTALL_TIMEOUT:g}s"
            ) from error
        except OSError as error:
            raise UpdateProbeError(f"cannot run uv export: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise UpdateProbeError(f"uv export failed: {detail}")
        if not destination.is_file():
            raise UpdateProbeError("uv export did not create the constraints file")
    return destination


_LOCK_RECEIPT_FILENAME = "installed-lock.json"


def write_lock_receipt(
    hyprial_home: Path,
    *,
    source: str,
    commit: str,
    constraints: Path,
) -> Path:
    """Persist the exact exported pins used for the successful install."""

    path = Path(hyprial_home) / _LOCK_RECEIPT_FILENAME
    pins = Path(constraints).read_text(encoding="utf-8")
    path.write_text(
        json.dumps(
            {"version": 1, "source": source, "commit": commit, "pins": pins},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _applicable_exact_pins(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    logical = text.replace("\\\n", " ")
    for raw in logical.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        exact = [
            spec.version
            for spec in requirement.specifier
            if spec.operator in {"==", "==="} and "*" not in spec.version
        ]
        if len(exact) == 1:
            pins[canonicalize_name(requirement.name)] = exact[0]
    return pins


def dependency_lock_report(
    hyprial_home: Path, installation: Installation
) -> Json:
    """Compare installed distributions with the pins used for this commit."""

    path = Path(hyprial_home) / _LOCK_RECEIPT_FILENAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "unknown", "reason": "lock receipt not available"}
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "reason": "lock receipt unreadable"}
    if not isinstance(record, dict) or record.get("commit") != installation.commit:
        return {"status": "unknown", "reason": "lock receipt does not match installed commit"}
    raw_pins = record.get("pins")
    if not isinstance(raw_pins, str):
        return {"status": "unknown", "reason": "lock receipt has no pins"}
    mismatches: list[Json] = []
    for name, expected in sorted(_applicable_exact_pins(raw_pins).items()):
        try:
            installed = package_version(name)
        except PackageNotFoundError:
            installed = None
        if installed != expected:
            mismatches.append(
                {"name": name, "expected": expected, "installed": installed}
            )
    return {
        "status": "match" if not mismatches else "mismatch",
        "mismatches": mismatches,
    }


def _parse_ls_remote(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        sha, separator, ref = line.partition("\t")
        if separator and re.fullmatch(r"[0-9a-f]{40}", sha):
            refs[ref] = sha
    return refs


def _tag_commits(refs: dict[str, str]) -> dict[str, str]:
    """Collapse lightweight and annotated tag refs to exact target commits."""

    candidates: dict[str, str] = {}
    for ref, sha in refs.items():
        match = _TAG_REF.fullmatch(ref)
        if match is None:
            continue
        tag, peeled = match.groups()
        if peeled:
            candidates[tag] = sha
        else:
            candidates.setdefault(tag, sha)
    return candidates


def _version_tag(tag: str) -> Version | None:
    if not tag.startswith("v"):
        return None
    raw_version = tag[1:]
    # ``Version`` itself accepts another leading ``v`` and an epoch.  Neither
    # spelling belongs to the release-automation contract: default candidates
    # are exactly one lower-case ``v`` followed by an X.Y.Z-family version.
    if not raw_version[:1].isdigit() or "!" in raw_version:
        return None
    try:
        version = Version(raw_version)
    except InvalidVersion:
        return None
    # Release automation documents and accepts exactly the vX.Y.Z family.
    return version if len(version.release) == 3 else None


def select_latest_version_tag(candidates: dict[str, str]) -> tuple[str, str, Version]:
    """Select the highest vX.Y.Z-family tag by PEP 440 ordering.

    Development and other prereleases are included.  For PEP 440-equivalent
    spellings, the raw tag name is a deterministic lexical tie-breaker.
    """

    parsed = [
        (version, tag, commit)
        for tag, commit in candidates.items()
        if (version := _version_tag(tag)) is not None
    ]
    if not parsed:
        raise UpdateProbeError(
            "found no parseable vX.Y.Z PEP 440 version tags; "
            "the release source must publish a version tag before hyprial can upgrade"
        )
    version, tag, commit = max(parsed, key=lambda item: (item[0], item[1]))
    return tag, commit, version


def resolve_remote(
    url: str,
    *,
    tag: str | None = None,
    runner: object = None,
    timeout: float = LS_REMOTE_TIMEOUT,
) -> RemoteResolution:
    """Resolve a remote tag with one retry only when the probe times out.

    Each attempt has ``timeout`` seconds, so the subprocess wall-time maximum
    is ``timeout * 2``. Authentication, transport, and Git exit failures are
    returned immediately and never retried.
    """

    valid_timeout = _valid_ls_remote_timeout(timeout)
    if valid_timeout is None:
        raise UpdateProbeError(
            "git ls-remote --tags budget must be a positive finite number; "
            f"got {timeout!r}"
        )
    attempts = LS_REMOTE_RETRY_COUNT + 1
    timeout_elapsed: list[float] = []
    output: str | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            output = _run_git(
                ["ls-remote", "--tags", url],
                timeout=valid_timeout,
                runner=runner,
            )
        except subprocess.TimeoutExpired as error:
            elapsed = max(0.0, time.monotonic() - started)
            timeout_elapsed.append(elapsed)
            _LOG.info(
                "git ls-remote --tags attempt %d/%d timed out in %.1fs "
                "(budget %gs)",
                attempt,
                attempts,
                elapsed,
                valid_timeout,
            )
            if attempt < attempts:
                continue
            detail = ", ".join(
                f"attempt {index} {duration:.1f}s/{valid_timeout:g}s"
                for index, duration in enumerate(timeout_elapsed, 1)
            )
            maximum = valid_timeout * attempts
            raise UpdateProbeError(
                f"git ls-remote --tags timed out: {detail}; maximum {maximum:g}s"
            ) from error
        except UpdateProbeError:
            elapsed = max(0.0, time.monotonic() - started)
            _LOG.info(
                "git ls-remote --tags attempt %d/%d failed in %.1fs "
                "(budget %gs)",
                attempt,
                attempts,
                elapsed,
                valid_timeout,
            )
            raise
        else:
            elapsed = max(0.0, time.monotonic() - started)
            _LOG.info(
                "git ls-remote --tags attempt %d/%d succeeded in %.1fs "
                "(budget %gs)",
                attempt,
                attempts,
                elapsed,
                valid_timeout,
            )
            break
    assert output is not None
    candidates = _tag_commits(_parse_ls_remote(output))
    if tag is not None:
        commit = candidates.get(tag)
        if commit is None:
            raise UpdateProbeError(f"tag {tag!r} does not exist on {url}")
        parsed = _version_tag(tag)
        return RemoteResolution(
            tag=tag,
            commit=commit,
            version=str(parsed) if parsed is not None else None,
        )
    try:
        selected, commit, version = select_latest_version_tag(candidates)
    except UpdateProbeError as error:
        raise UpdateProbeError(
            f"{error} on {url}"
        ) from error
    return RemoteResolution(
        tag=selected, commit=commit, version=str(version)
    )


def resolution_is_a_downgrade(
    installation: Installation, resolution: RemoteResolution
) -> bool:
    """True only when both versions parse and the remote one is strictly older.

    ⚠️ Proof, not suspicion. Anything unparseable on either side means we
    cannot tell, and "cannot tell" must not become "refuse" -- a machine that
    stops upgrading because it could not read its own version number would go
    quiet in exactly the way that takes days to notice.
    """

    if installation.version is None or resolution.version is None:
        return False
    try:
        installed = Version(installation.version)
        remote = Version(resolution.version)
    except InvalidVersion:
        return False
    return remote < installed


def upgrade_available(
    installation: Installation,
    resolution: RemoteResolution,
    *,
    operator_chose_the_tag: bool = False,
) -> bool:
    """Return false when the install proves the resolved commit, or would move back.

    A matching version string is not enough: an old branch install or an
    untracked wheel can carry the same version while containing different
    code.  Without PEP 610 commit evidence, reinstall the resolved tag.

    ⭐ And a *moving* ref can move backwards. When a track tag is rolled back --
    a bad release withdrawn, a tag repointed at an earlier commit -- the commit
    differs, so the check above says "upgrade available" and every machine on
    that track quietly installs an older build. Nothing about that reads as an
    error at the time; it is the same log line as a normal upgrade.

    ⚠️ `operator_chose_the_tag` is what keeps this guard from eating the escape
    hatch. `hyprial upgrade --tag v0.4.1` is a person deliberately going back, and
    it must keep working -- a "never downgrade" implementation would satisfy
    every test about rollbacks while breaking the one path that is supposed to
    downgrade. The default is False so a caller that forgets the flag gets the
    guard rather than silently losing it.
    """

    if not operator_chose_the_tag and resolution_is_a_downgrade(
        installation, resolution
    ):
        return False
    if installation.commit is not None:
        return installation.commit != resolution.commit
    return True
