"""Resolved agent-home P2 roots and non-secret tool environment profiles.

This module is the shared boundary consumed by provider adapters.  It resolves
one durable Agent identity into one immutable launch context; provider modules
do not derive roots from ``HOME``, cwd, or an actor URI themselves.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from hyprial.updates import DEFAULT_GIT_URL

from .config import (
    NATIVE_CONFIG_MAPPING_VERSION,
    ConfigManifest,
    ConfigProjectionReceipt,
    NativeConfigProjection,
    build_native_projection,
    materialize_native_projection,
    require_agent_config,
    validate_agent_config_location,
)
from .home import HomeReceipt

__all__ = [
    "AgentRuntimeContext",
    "AgentRuntimeError",
    "AgentRuntimeRoots",
    "AgentRuntimePreparation",
    "AgentToolProfile",
    "DEFAULT_AGENT_TOOL_PROFILE",
    "SHARED_CREDENTIAL_DIVERGED",
    "SHARED_CREDENTIAL_CONFLICT",
    "SHARED_CREDENTIAL_INVALID",
    "SHARED_CREDENTIAL_UNSUPPORTED",
    "SharedCredentialBinding",
    "SshToolAuthorization",
    "resolve_agent_runtime_context",
    "build_agent_runtime_preparation",
    "materialize_agent_runtime_context",
    "shared_credential_status",
    "validate_shared_credential_binding",
    "validate_shared_credential_environment",
]

_P2_HARNESSES = frozenset({"claude", "codex", "pi"})
_NATIVE_ROOT_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "pi": "PI_CODING_AGENT_DIR",
}
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SSH_TOKEN = re.compile(r"[A-Za-z0-9._@:-]+\Z")
_NATIVE_CREDENTIAL_NAME = {
    "claude": ".credentials.json",
    "codex": "auth.json",
    "pi": "auth.json",
}
SHARED_CREDENTIAL_DIVERGED = "SHARED_CREDENTIAL_DIVERGED"
SHARED_CREDENTIAL_CONFLICT = "SHARED_CREDENTIAL_CONFLICT"
SHARED_CREDENTIAL_INVALID = "SHARED_CREDENTIAL_INVALID"
SHARED_CREDENTIAL_UNSUPPORTED = "SHARED_CREDENTIAL_UNSUPPORTED"


def _current_uid() -> int:
    return os.getuid()


class AgentRuntimeError(ValueError):
    """A P2 root/profile combination cannot be represented safely."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _default_git_endpoint() -> tuple[str, str]:
    parsed = urlsplit(DEFAULT_GIT_URL)
    if parsed.hostname is None:
        raise AgentRuntimeError("default Git URL must name a host")
    return parsed.hostname, urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


_DEFAULT_GIT_HOSTNAME, _DEFAULT_GIT_REWRITE_TARGET = _default_git_endpoint()


@dataclass(frozen=True, slots=True)
class SshToolAuthorization:
    """Explicit non-secret selection metadata for one approved SSH signer."""

    auth_sock: str
    identity_file: str
    known_hosts_file: str
    host: str = "code.hyprial.com"
    hostname: str = _DEFAULT_GIT_HOSTNAME
    user: str = "git"

    def __post_init__(self) -> None:
        for label, value in (
            ("auth_sock", self.auth_sock),
            ("identity_file", self.identity_file),
            ("known_hosts_file", self.known_hosts_file),
        ):
            if not value or "\n" in value or not Path(value).is_absolute():
                raise AgentRuntimeError(f"SSH {label} must be an absolute path")
        if any(
            not value or _SSH_TOKEN.fullmatch(value) is None
            for value in (self.host, self.hostname, self.user)
        ):
            raise AgentRuntimeError("SSH host, hostname, and user must not be blank")


@dataclass(frozen=True, slots=True)
class AgentToolProfile:
    """Approved, non-secret tool identity and optional SSH capability."""

    profile_id: str
    git_author_name: str
    git_author_email: str
    git_rewrite_source: str = "https://code.hyprial.com/"
    git_rewrite_target: str = _DEFAULT_GIT_REWRITE_TARGET
    tea_login: str = "code.hyprial.com"
    ssh: SshToolAuthorization | None = None

    def __post_init__(self) -> None:
        values = (
            self.profile_id,
            self.git_author_name,
            self.git_author_email,
            self.git_rewrite_source,
            self.git_rewrite_target,
            self.tea_login,
        )
        if any(not value for value in values):
            raise AgentRuntimeError("tool profile fields must not be blank")
        if "\n" in "".join(values):
            raise AgentRuntimeError("tool profile fields must be single-line values")
        if _PROFILE_ID.fullmatch(self.profile_id) is None:
            raise AgentRuntimeError("tool profile id contains an unsafe path character")
        if any(character in self.git_rewrite_target for character in '\"]'):
            raise AgentRuntimeError("Git rewrite target contains an unsafe config character")
        if (
            self.git_rewrite_source != "https://code.hyprial.com/"
            or self.git_rewrite_target != _DEFAULT_GIT_REWRITE_TARGET
            or self.tea_login != "code.hyprial.com"
        ):
            raise AgentRuntimeError(
                "tool profile may not widen the approved Forgejo endpoints"
            )


DEFAULT_AGENT_TOOL_PROFILE = AgentToolProfile(
    profile_id="agent-home-p2-allen-v1",
    git_author_name="Allen Woods",
    git_author_email="allenwoods@users.noreply.code.hyprial.com",
)


@dataclass(frozen=True, slots=True)
class AgentRuntimeRoots:
    """All roots for one selected harness, derived from one home receipt."""

    agent_home: Path
    config_source: Path
    projection_root: Path
    native_root: Path
    session_root: Path
    tool_home: Path
    xdg_config_home: Path
    xdg_cache_home: Path
    xdg_data_home: Path
    xdg_state_home: Path
    tool_profile_root: Path

    def __post_init__(self) -> None:
        for value in (
            self.agent_home,
            self.config_source,
            self.projection_root,
            self.native_root,
            self.session_root,
            self.tool_home,
            self.xdg_config_home,
            self.xdg_cache_home,
            self.xdg_data_home,
            self.xdg_state_home,
            self.tool_profile_root,
        ):
            if not value.is_absolute():
                raise AgentRuntimeError("runtime roots must be absolute")


@dataclass(frozen=True, slots=True)
class SharedCredentialBinding:
    """One explicit native link-to-target claim, containing no credential value."""

    actor: str
    harness: str
    native_path: Path
    target_path: Path
    agent_cwd: Path | None


def validate_shared_credential_binding(binding: SharedCredentialBinding) -> None:
    """Fail closed unless the native path is the exact designated private link."""

    native = binding.native_path
    target = binding.target_path
    identity = f"agent {binding.actor} shared credential {native}"
    if binding.harness == "claude":
        raise AgentRuntimeError(
            f"{identity} is unsupported on macOS because Claude Code uses a "
            "config-scoped Keychain item; use the explicit OAuth-token grant",
            code=SHARED_CREDENTIAL_UNSUPPORTED,
        )
    try:
        native_metadata = native.lstat()
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} cannot be inspected: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    if not stat.S_ISLNK(native_metadata.st_mode):
        code = (
            SHARED_CREDENTIAL_DIVERGED
            if stat.S_ISREG(native_metadata.st_mode)
            else SHARED_CREDENTIAL_INVALID
        )
        raise AgentRuntimeError(
            f"{identity} is no longer the designated symbolic link",
            code=code,
        )
    try:
        raw_destination = Path(os.readlink(native))
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} cannot be read: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    destination = (
        raw_destination
        if raw_destination.is_absolute()
        else native.parent / raw_destination
    ).resolve(strict=False)
    designated = target.resolve(strict=False)
    if destination != designated:
        raise AgentRuntimeError(
            f"{identity} points to {destination}, not designated target {target}",
            code=SHARED_CREDENTIAL_DIVERGED,
        )
    try:
        target_metadata = target.lstat()
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} designated target {target} cannot be inspected: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    if stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(
        target_metadata.st_mode
    ):
        raise AgentRuntimeError(
            f"{identity} designated target {target} must be a regular file",
            code=SHARED_CREDENTIAL_INVALID,
        )
    if stat.S_IMODE(target_metadata.st_mode) != 0o600:
        raise AgentRuntimeError(
            f"{identity} designated target {target} mode must be 0600",
            code=SHARED_CREDENTIAL_INVALID,
        )
    if target_metadata.st_uid != _current_uid():
        raise AgentRuntimeError(
            f"{identity} designated target {target} must be owned by the current user",
            code=SHARED_CREDENTIAL_INVALID,
        )
    if binding.agent_cwd is not None:
        try:
            designated.relative_to(binding.agent_cwd.resolve(strict=False))
        except ValueError:
            pass
        else:
            raise AgentRuntimeError(
                f"{identity} designated target {target} must be outside the agent cwd",
                code=SHARED_CREDENTIAL_INVALID,
            )


def validate_shared_credential_environment(
    subject: AgentRuntimeContext | SharedCredentialBinding,
    environment: Mapping[str, str],
) -> None:
    """Keep explicit environment credentials mutually exclusive with the link."""

    binding = (
        subject.shared_credential
        if isinstance(subject, AgentRuntimeContext)
        else subject
    )
    if binding is None:
        return
    from .secrets import SECRET_ENVIRONMENT_NAMES

    conflicting = sorted(
        name for name in SECRET_ENVIRONMENT_NAMES if environment.get(name)
    )
    if conflicting:
        raise AgentRuntimeError(
            f"agent {binding.actor} native shared credential and explicit "
            f"environment credential {conflicting[0]} may not coexist",
            code=SHARED_CREDENTIAL_CONFLICT,
        )


def shared_credential_status(*, registry: Any, agent: Any) -> list[dict[str, object]]:
    """Project configured link health for ``agent list`` without reading values."""

    config = agent.config
    if config is None or not config.shared_credentials:
        return []
    try:
        agent_home = Path(registry.home_receipt(agent.actor).path)
    except (OSError, RuntimeError, ValueError) as error:
        return [
            {
                "harness": harness,
                "authMode": "native-shared-link",
                "targetPath": target,
                "status": "error",
                "code": SHARED_CREDENTIAL_INVALID,
                "error": f"agent {agent.actor} shared credential home is unavailable: {error}",
            }
            for harness, target in config.shared_credentials
        ]
    rows: list[dict[str, object]] = []
    for harness, target in config.shared_credentials:
        native = (
            agent_home
            / "secrets"
            / "native"
            / harness
            / _NATIVE_CREDENTIAL_NAME[harness]
        )
        binding = SharedCredentialBinding(
            actor=agent.actor,
            harness=harness,
            native_path=native,
            target_path=Path(target),
            agent_cwd=None if agent.cwd is None else Path(agent.cwd),
        )
        row: dict[str, object] = {
            "harness": harness,
            "authMode": "native-shared-link",
            "nativePath": str(native),
            "targetPath": target,
        }
        try:
            validate_shared_credential_binding(binding)
        except AgentRuntimeError as error:
            row.update(
                status="error",
                code=error.code or SHARED_CREDENTIAL_INVALID,
                error=str(error),
            )
        else:
            row["status"] = "warning" if harness == "pi" else "ready"
            if harness == "pi":
                row["warning"] = (
                    "Pi 0.85.1 locks the per-agent link path, so concurrent "
                    "refreshes are not mutually excluded"
                )
        rows.append(row)
    return rows


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """One incarnation-bound P2 context carried to the final exec boundary."""

    actor: str
    entity_token: str = field(repr=False)
    harness: str
    manifest: ConfigManifest
    projection: NativeConfigProjection
    projection_receipt: ConfigProjectionReceipt
    roots: AgentRuntimeRoots
    tool_profile_id: str
    environment_items: tuple[tuple[str, str], ...] = field(repr=False)
    auth_method: str | None = None
    auth_revision: str | None = None
    shared_credential: SharedCredentialBinding | None = field(
        default=None, repr=False
    )
    authority_prepared: bool = False
    home_resource_token: str | None = field(default=None, repr=False)
    launch_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.harness not in _P2_HARNESSES:
            raise AgentRuntimeError(f"unsupported P2 harness {self.harness!r}")
        if not self.actor or not self.entity_token or not self.tool_profile_id:
            raise AgentRuntimeError("runtime context identity must not be blank")
        names = [name for name, _value in self.environment_items]
        if names != sorted(names) or len(names) != len(set(names)):
            raise AgentRuntimeError("runtime environment names must be sorted and unique")

    @property
    def config_revision(self) -> str:
        return self.manifest.revision

    def environment(self) -> dict[str, str]:
        """Return only generated roots/profile selectors; never credentials."""

        return dict(self.environment_items)

    def public_projection(self) -> dict[str, object]:
        """Non-secret CLI handoff with an opaque incarnation launch fence."""

        return {
            "mode": "agent-home-p2",
            "authorityPrepared": self.authority_prepared,
            **(
                {"launchToken": self.launch_token}
                if self.authority_prepared and self.launch_token is not None
                else {}
            ),
            "actor": self.actor,
            "harness": self.harness,
            "configRevision": self.config_revision,
            "mappingVersion": NATIVE_CONFIG_MAPPING_VERSION,
            "projectionRoot": str(self.roots.projection_root),
            "nativeRoot": str(self.roots.native_root),
            "sessionRoot": str(self.roots.session_root),
            "toolProfileId": self.tool_profile_id,
            "environment": self.environment(),
            "auth": (
                {"method": self.auth_method, "revision": self.auth_revision}
                if self.auth_method is not None and self.auth_revision is not None
                else None
            ),
            "sharedCredential": (
                {
                    "authMode": "native-shared-link",
                    "nativePath": str(self.shared_credential.native_path),
                    "targetPath": str(self.shared_credential.target_path),
                }
                if self.shared_credential is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class AgentRuntimePreparation:
    """Immutable home-incarnation input admitted to the filesystem authority."""

    actor: str
    actor_name: str
    entity_token: str
    config: object
    home_receipt: HomeReceipt
    harness: str
    cwd: str | None
    tool_profile: AgentToolProfile
    containerized: bool = False


def build_agent_runtime_preparation(
    *,
    registry: Any,
    agent_name: str,
    harness: str,
    cwd: str | None,
    tool_profile: AgentToolProfile,
    containerized: bool = False,
    validate_home: bool = True,
) -> AgentRuntimePreparation | None:
    agent = registry.require(agent_name)
    if agent.config is None or harness not in _P2_HARNESSES:
        return None
    receipt = registry.home_receipt(
        agent.actor, validate_mirror=validate_home
    )
    return AgentRuntimePreparation(
        actor=agent.uri,
        actor_name=agent.actor,
        entity_token=agent.entity_token,
        config=agent.config,
        home_receipt=receipt,
        harness=harness,
        cwd=cwd,
        tool_profile=tool_profile,
        containerized=containerized,
    )


def resolve_agent_runtime_context(
    *,
    registry: Any,
    agent_name: str,
    harness: str,
    cwd: str | None,
    tool_profile: AgentToolProfile,
    containerized: bool = False,
) -> AgentRuntimeContext | None:
    """Resolve/materialize one P2 context, or return ``None`` for legacy mode.

    An explicit Agent config opts that identity into P2 for the three supported
    harnesses.  DSH/residents without an explicit C stay untouched.  A P2
    context never silently falls back when its entry cannot carry the contract.
    """

    preparation = build_agent_runtime_preparation(
        registry=registry,
        agent_name=agent_name,
        harness=harness,
        cwd=cwd,
        tool_profile=tool_profile,
        containerized=containerized,
    )
    if preparation is None:
        return None
    return materialize_agent_runtime_context(preparation)


def materialize_agent_runtime_context(
    preparation: AgentRuntimePreparation,
) -> AgentRuntimeContext:
    """Materialize one already-fenced preparation on the home FS owner."""

    if preparation.containerized:
        raise AgentRuntimeError(
            "agent-home P2 is not supported for containerized launches"
        )
    config = require_agent_config(preparation.config, actor=preparation.actor_name)
    receipt = preparation.home_receipt
    agent_home = Path(receipt.path)
    validate_agent_config_location(
        config, agent_home=agent_home, cwd=preparation.cwd
    )
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, preparation.harness)

    revision_root = (
        agent_home / "state" / "config" / preparation.entity_token / manifest.revision
    )
    projection_parent = revision_root / "native"
    projection_root = projection_parent / preparation.harness
    native_root = agent_home / "secrets" / "native" / preparation.harness
    session_root = (
        agent_home / "state" / "pi"
        if preparation.harness == "pi"
        else agent_home / "state" / "sessions" / preparation.harness
    )
    tool_home = agent_home / "state" / "home"
    xdg_config_home = agent_home / "secrets" / "tools" / "xdg"
    xdg_root = agent_home / "state" / "xdg"
    tool_profile_root = revision_root / "tools" / preparation.tool_profile.profile_id

    for directory in (
        revision_root,
        projection_parent,
        native_root,
        session_root,
        tool_home,
        xdg_config_home,
        xdg_root / "cache",
        xdg_root / "data",
        xdg_root / "state",
        tool_profile_root,
    ):
        _ensure_private_directory(agent_home, directory)

    incumbent = None
    if projection_root.exists() or projection_root.is_symlink():
        incumbent = ConfigProjectionReceipt(
            actor=preparation.actor,
            entity_token=preparation.entity_token,
            source_digest=projection.source_digest,
            harness=projection.harness,
            mapping_version=projection.mapping_version,
            projection_digest=projection.digest,
            projection_root=str(projection_root),
            items=projection.items,
        )
    projection_receipt = materialize_native_projection(
        config,
        manifest,
        projection,
        actor=preparation.actor,
        entity_token=preparation.entity_token,
        projection_root=projection_root,
        incumbent=incumbent,
    )
    tool_environment = _materialize_tool_profile(
        tool_profile_root, preparation.tool_profile
    )
    roots = AgentRuntimeRoots(
        agent_home=agent_home,
        config_source=Path(config.source),
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        tool_home=tool_home,
        xdg_config_home=xdg_config_home,
        xdg_cache_home=xdg_root / "cache",
        xdg_data_home=xdg_root / "data",
        xdg_state_home=xdg_root / "state",
        tool_profile_root=tool_profile_root,
    )
    environment = {
        "HOME": str(tool_home),
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "XDG_CACHE_HOME": str(xdg_root / "cache"),
        "XDG_DATA_HOME": str(xdg_root / "data"),
        "XDG_STATE_HOME": str(xdg_root / "state"),
        _NATIVE_ROOT_ENV[preparation.harness]: str(native_root),
        **tool_environment,
    }
    shared_target = config.shared_credential_path(preparation.harness)
    shared_credential = (
        None
        if shared_target is None
        else SharedCredentialBinding(
            actor=preparation.actor_name,
            harness=preparation.harness,
            native_path=native_root / _NATIVE_CREDENTIAL_NAME[preparation.harness],
            target_path=Path(shared_target),
            agent_cwd=None if preparation.cwd is None else Path(preparation.cwd),
        )
    )
    context = AgentRuntimeContext(
        actor=preparation.actor,
        entity_token=preparation.entity_token,
        harness=preparation.harness,
        manifest=manifest,
        projection=projection,
        projection_receipt=projection_receipt,
        roots=roots,
        tool_profile_id=preparation.tool_profile.profile_id,
        environment_items=tuple(sorted(environment.items())),
        auth_method="native-shared-link" if shared_credential is not None else None,
        auth_revision="designated-v1" if shared_credential is not None else None,
        shared_credential=shared_credential,
        home_resource_token=receipt.resource_token,
    )
    if shared_credential is not None:
        validate_shared_credential_binding(shared_credential)
    return context


def _materialize_tool_profile(
    root: Path, profile: AgentToolProfile
) -> dict[str, str]:
    gitconfig = root / "gitconfig"
    git_body = (
        "[user]\n"
        f"\tname = {profile.git_author_name}\n"
        f"\temail = {profile.git_author_email}\n"
    ).encode()
    if profile.ssh is not None:
        git_body += (
            f"[url \"{profile.git_rewrite_target}\"]\n"
            f"\tinsteadOf = {profile.git_rewrite_source}\n"
        ).encode()
    _write_or_verify_private_file(gitconfig, git_body)
    environment = {
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": profile.git_author_name,
        "GIT_AUTHOR_EMAIL": profile.git_author_email,
        "GIT_COMMITTER_NAME": profile.git_author_name,
        "GIT_COMMITTER_EMAIL": profile.git_author_email,
        "HYPRIAL_TOOL_PROFILE_ID": profile.profile_id,
        "HYPRIAL_TEA_LOGIN": profile.tea_login,
    }
    if profile.ssh is None:
        return environment
    ssh = profile.ssh
    ssh_config = root / "ssh_config"
    ssh_body = (
        f"Host {ssh.host} {ssh.hostname}\n"
        f"    HostName {ssh.hostname}\n"
        f"    User {ssh.user}\n"
        "    IdentitiesOnly yes\n"
        f"    IdentityFile {ssh.identity_file}\n"
        f"    IdentityAgent {ssh.auth_sock}\n"
        f"    UserKnownHostsFile {ssh.known_hosts_file}\n"
        "    StrictHostKeyChecking yes\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
    ).encode()
    _write_or_verify_private_file(ssh_config, ssh_body)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": (
                f"url.{profile.git_rewrite_target}.insteadOf"
            ),
            "GIT_CONFIG_VALUE_0": profile.git_rewrite_source,
            "SSH_AUTH_SOCK": ssh.auth_sock,
            "GIT_SSH_COMMAND": f"ssh -F {shlex.quote(str(ssh_config))}",
        }
    )
    return environment


def _ensure_private_directory(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise AgentRuntimeError("runtime directory escaped the agent home") from error
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            metadata = current.lstat()
        except OSError as error:
            raise AgentRuntimeError(f"cannot inspect runtime directory {part}") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise AgentRuntimeError(f"runtime directory {part} is not private")


def _write_or_verify_private_file(path: Path, body: bytes) -> None:
    expected = hashlib.sha256(body).digest()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            metadata = path.lstat()
            actual = path.read_bytes()
        except OSError as error:
            raise AgentRuntimeError(f"cannot verify tool profile file {path.name}") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or hashlib.sha256(actual).digest() != expected
        ):
            raise AgentRuntimeError(f"tool profile file {path.name} drifted")
        return
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
