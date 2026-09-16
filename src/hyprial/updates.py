"""Tag-governed Git update resolution with opt-in upgrade tracks.

Distribution is deliberately Git/uv based (``uv tool install git+...@<ref>``)
with no package registry.  The remote release decision is the set of Forgejo
tags.  Two resolution modes share one probe:

- Legacy (default): the highest parseable ``vX.Y.Z...`` tag by PEP 440
  ordering, including development and release-candidate tags.  This is the
  exact pre-track behavior; an installation without an explicit track keeps
  it byte-for-byte.
- Track (opt-in): ``settings.json`` ``updateTrack`` set to ``dev``,
  ``nightly``, or ``stable`` resolves the movable tag of the same name —
  ``dev`` follows the dev branch after a green unit run, ``nightly`` the
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
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import distribution as package_distribution
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

Json = dict[str, Any]

DEFAULT_GIT_URL = "ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git"
_PRE_RENAME_GIT_HOST = "git.internal.hyprial.com"
_PRE_RENAME_GIT_PATHS = {
    "/HyprialOS/harness-bridge-py",
    "/HyprialOS/harness-bridge-py.git",
}

LS_REMOTE_TIMEOUT = 15.0
# A full ``uv tool install`` of a git source can take minutes on a cold cache.
UV_INSTALL_TIMEOUT = 300.0
# ``uv tool dir`` is a directory listing (verified read-only on uv 0.11.7:
# no mkdir side effect), so it answers in well under a second; the timeout
# only bounds a wedged PATH lookup.
UV_TOOL_DIR_PROBE_TIMEOUT = 10.0

_TAG_REF = re.compile(r"^refs/tags/(.+?)(\^\{\})?$")

#: The only values ``settings.json`` ``updateTrack`` accepts; each names the
#: movable remote tag that track resolves.  Absent key = legacy latest-tag.
TRACK_TAGS = ("dev", "nightly", "stable")


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


def _is_pre_rename_git_url(url: str) -> bool:
    """Match only known spellings of the retired Python repository.

    PEP 610 normally records the canonical ``ssh://`` URL, while Git also
    accepts the scp-like form and installers may retain a ``git+ssh`` prefix.
    Hostname case, default port 22, a missing ``.git``, and one trailing slash
    are transport-equivalent.  Credentials, repository owner/path, query, and
    fragment must match exactly enough that custom registries are never
    silently retargeted.
    """

    scp_match = re.fullmatch(
        r"git@([^:]+):(HyprialOS/harness-bridge-py(?:\.git)?)/?", url
    )
    if scp_match is not None:
        return scp_match.group(1).lower() == _PRE_RENAME_GIT_HOST

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"ssh", "git+ssh"}
        and parsed.username == "git"
        and parsed.password is None
        and parsed.hostname is not None
        and parsed.hostname.lower() == _PRE_RENAME_GIT_HOST
        and port in {None, 22}
        and parsed.path.rstrip("/") in _PRE_RENAME_GIT_PATHS
        and not parsed.query
        and not parsed.fragment
    )


def installation_git_url(installation: Installation) -> str:
    """Return the effective update source, healing the repository rename.

    Old installed wheels retain their source in ``direct_url.json``.  Rewrite
    only the known retired HyprialOS Python repository; every custom source is
    returned byte-for-byte unchanged.
    """

    url = installation.url or DEFAULT_GIT_URL
    return DEFAULT_GIT_URL if _is_pre_rename_git_url(url) else url


def installation_is_local(installation: Installation) -> bool:
    """True when this distribution was installed from a local path.

    Guard 2 of spec autoupdate-isolated-home-2026-09-15: a ``file://`` (or
    bare-path, or ``git+file://``, or editable) install source means this
    process runs from a working tree, so an upgrade would resolve tags
    against that tree and then overwrite the USER'S GLOBAL uv tool
    directory with it.  The check reads the raw PEP 610 url BEFORE
    :func:`installation_git_url` heals the repository rename -- healing
    must not turn a local source into an apparently remote one.
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
    if track not in TRACK_TAGS:
        raise UpdateProbeError(
            f"{path} updateTrack must be one of "
            f"{', '.join(TRACK_TAGS)}; got {track!r}"
        )
    return str(track)


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
        if value is not None and value not in TRACK_TAGS:
            findings.append(
                f"updateTrack {value!r} is not one of {', '.join(TRACK_TAGS)}; "
                "hyprial upgrade will refuse until it is fixed"
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
    except subprocess.TimeoutExpired as error:
        raise UpdateProbeError(
            f"git {' '.join(args[:2])} timed out after {timeout:g}s"
        ) from error
    except OSError as error:
        raise UpdateProbeError(f"cannot run git: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UpdateProbeError(f"git {' '.join(args[:2])} failed: {detail}")
    return completed.stdout


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
            "Forgejo must publish a version tag before hyprial can upgrade"
        )
    version, tag, commit = max(parsed, key=lambda item: (item[0], item[1]))
    return tag, commit, version


def resolve_remote(
    url: str,
    *,
    tag: str | None = None,
    runner: object = None,
) -> RemoteResolution:
    """Resolve the latest or an explicitly named remote tag with one probe."""

    output = _run_git(
        ["ls-remote", "--tags", url], timeout=LS_REMOTE_TIMEOUT, runner=runner
    )
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
