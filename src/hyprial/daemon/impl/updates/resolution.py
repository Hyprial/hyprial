"""Remote tag/commit resolution, downgrade detection and upgrade availability."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import re
import subprocess
import time
from packaging.version import InvalidVersion, Version

from .installation import (
    Installation,
    LS_REMOTE_RETRY_COUNT,
    LS_REMOTE_TIMEOUT,
    RemoteResolution,
    UpdateProbeError,
    _LOG,
    _TAG_REF,
    _run_git,
    _valid_ls_remote_timeout,
)


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
