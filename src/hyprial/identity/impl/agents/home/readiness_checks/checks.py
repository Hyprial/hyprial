"""Read-only home and config-revision checks."""

from __future__ import annotations

import stat
from pathlib import Path

from hyprial.kernel import private_metadata_violation

from hyprial.identity.impl.agents.home.config import AgentConfig, AgentConfigError
from hyprial.identity.impl.agents.home.readiness_checks.model import (
    BLOCKED,
    PASS,
    UNKNOWN,
    ReadinessCheck,
)


def _home_check(agent_home: Path | None) -> ReadinessCheck:
    if agent_home is None:
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail=(
                "the P2 agent home is not provisioned; HOME/XDG/native roots "
                "cannot be checked"
            ),
            action=(
                "provision the P2 home explicitly (agent ensure-home) and rerun; "
                "readiness never creates it"
            ),
        )
    try:
        metadata = agent_home.lstat()
    except OSError:
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail="the P2 agent home recorded by the registry is missing",
            action="provision the P2 home explicitly and rerun readiness",
        )
    if stat.S_ISLNK(metadata.st_mode):
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail="the P2 agent home is a symbolic link",
            action="replace it with an agent-owned 0700 directory",
        )
    if not stat.S_ISDIR(metadata.st_mode):
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail="the P2 agent home path is not a directory",
            action="repair the agent home",
        )
    try:
        violation = private_metadata_violation(agent_home, metadata, mode=0o700)
    except OSError:
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail="the P2 agent home's private access cannot be inspected",
            action="repair access control explicitly; readiness will not change it",
        )
    if violation == "wrong-owner":
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail="the P2 agent home is not owned by the daemon user",
            action="repair ownership explicitly; readiness will not chmod/chown",
        )
    if violation == "unsafe-mode":
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail="the P2 agent home is not mode 0700",
            action="repair the mode explicitly; readiness will not change it",
        )
    if violation is not None:
        return ReadinessCheck(
            name="home",
            status=BLOCKED,
            detail=f"the P2 agent home is not account-private ({violation})",
            action="repair access control explicitly; readiness will not change it",
        )
    return ReadinessCheck(
        name="home",
        status=PASS,
        detail="agent-owned P2 home is present with mode 0700; nothing was written",
    )


def _published_revisions(
    agent_home: Path | None, entity_token: str | None
) -> tuple[tuple[str, ...], str | None]:
    if agent_home is None or entity_token is None:
        return (), None
    root = agent_home / "state" / "config" / entity_token
    try:
        if not root.is_dir():
            return (), None
        revisions = tuple(
            sorted(
                child.name
                for child in root.iterdir()
                if child.is_dir() and not child.is_symlink()
            )
        )
    except OSError:
        return (), "cannot inspect published config revisions"
    return revisions, None


def _config_check(
    *,
    mode: str,
    config: AgentConfig | None,
    agent_home: Path | None,
    entity_token: str | None,
    harness: str,
    running: bool,
) -> tuple[ReadinessCheck, str | None, tuple[str, ...], bool | None, str | None]:
    """Return the config check plus revision/reload facts.

    The launch revision is not recorded on live bindings.  When a source change
    raced a running process this reports BLOCKED/UNKNOWN rather than pretending
    the process already uses the new revision.
    """

    if mode == "legacy":
        return (
            ReadinessCheck(
                name="config",
                status=UNKNOWN,
                detail=(
                    "legacy launch mode has no explicit P2 config revision; "
                    "stale-runtime visibility is unavailable"
                ),
                action=(
                    "opt this agent into an explicit config for revision-aware "
                    "readiness"
                ),
            ),
            None,
            (),
            None,
            None,
        )
    if config is None:
        return (
            ReadinessCheck(
                name="config",
                status=BLOCKED,
                detail="P2 mode was selected but the agent has no explicit config",
                action="declare one explicit config source for this agent",
            ),
            None,
            (),
            None,
            None,
        )
    try:
        revision = config.freeze_manifest().revision
    except AgentConfigError as error:
        return (
            ReadinessCheck(
                name="config",
                status=BLOCKED,
                detail=f"config cannot be frozen for the next launch: {error}",
                action="fix the explicit config source and rerun readiness",
            ),
            None,
            (),
            None,
            None,
        )
    published, scan_error = _published_revisions(agent_home, entity_token)
    if scan_error is not None:
        return (
            ReadinessCheck(
                name="config",
                status=UNKNOWN,
                detail=scan_error,
                action="make the published revision directory readable",
            ),
            revision,
            published,
            None,
            None,
        )
    projection = (
        agent_home / "state" / "config" / str(entity_token) / revision / "native" / harness
        if agent_home is not None and entity_token is not None
        else None
    )
    projection_exists = projection is not None and projection.is_dir()
    if not running:
        return (
            ReadinessCheck(
                name="config",
                status=PASS,
                detail=(
                    f"source revision {revision}; no active binding, so the next "
                    "start publishes and uses it"
                ),
            ),
            revision,
            published,
            False,
            None,
        )
    if revision not in published:
        return (
            ReadinessCheck(
                name="config",
                status=BLOCKED,
                detail=(
                    f"source revision {revision} is not published; the running "
                    f"process predates it. Published revisions: {len(published)}"
                ),
                action=(
                    "finish the task or reload/restart this agent at idle; "
                    "readiness never interrupts an active turn"
                ),
            ),
            revision,
            published,
            True,
            None,
        )
    if len(published) == 1 and projection_exists:
        return (
            ReadinessCheck(
                name="config",
                status=PASS,
                detail=(
                    f"source revision {revision} is the only published revision "
                    "for this harness; the running launch revision is not recorded, "
                    "but no other revision exists"
                ),
            ),
            revision,
            published,
            False,
            None,
        )
    return (
        ReadinessCheck(
            name="config",
            status=UNKNOWN,
            detail=(
                f"source revision {revision} is published among {len(published)} "
                "revisions; the running launch revision is not recorded, so "
                "staleness cannot be proven or excluded"
            ),
            action=(
                "reload this agent at idle if the task depends on the new config; "
                "do not interrupt an active turn"
            ),
        ),
        revision,
        published,
        None,
        None,
    )
