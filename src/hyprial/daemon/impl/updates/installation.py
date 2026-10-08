"""Installed-hyprial discovery, update tracks, uv tool guards, git export and lock receipts."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
from hyprial.kernel import FORGEJO_GIT_URL, OFFICIAL_GIT_URL
import json
import logging
import math
import os
import re
import subprocess
import sys
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


Json = dict[str, Any]


_LOG = logging.getLogger(__name__)


DEFAULT_GIT_URL = FORGEJO_GIT_URL


UPGRADE_SOURCES = {"official": OFFICIAL_GIT_URL, "forgejo": FORGEJO_GIT_URL}


LS_REMOTE_TIMEOUT = 45.0


LS_REMOTE_RETRY_COUNT = 1


LS_REMOTE_TIMEOUT_SETTINGS_KEY = "lsRemoteTimeoutSeconds"


LS_REMOTE_TIMEOUT_KEY_ALIASES = (
    LS_REMOTE_TIMEOUT_SETTINGS_KEY,
    "ls-remote-timeout",
)


UV_INSTALL_TIMEOUT = 300.0


UV_TOOL_DIR_PROBE_TIMEOUT = 10.0


_TAG_REF = re.compile(r"^refs/tags/(.+?)(\^\{\})?$")


TRACK_TAGS = ("internal", "nightly", "stable")


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


AUTOUPGRADE_SETTINGS_KEY = "autoUpgrade"


AUTOUPGRADE_KEY_ALIASES = (AUTOUPGRADE_SETTINGS_KEY, "auto-upgrade")


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
    # The public mirror needs no header.  A global ``http.extraHeader`` left
    # over from the internal forge (an ``Authorization: token ...`` line)
    # would be sent to GitHub too, which rejects it and asks for a username
    # the headless run can't give -- upgrade then fails with no hint (member
    # retest, 2026-09-28).  An empty value resets the header list, and the
    # URL scope keeps every other host's headers untouched.
    _append_git_config(env, f"http.{PUBLIC_GIT_ORIGIN}.extraHeader", "")
    return env


PUBLIC_GIT_ORIGIN = "https://github.com/"


def _append_git_config(env: dict[str, str], key: str, value: str) -> None:
    """Add one command-scope git config entry after any inherited ones."""

    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    count = max(count, 0)
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 1)


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
