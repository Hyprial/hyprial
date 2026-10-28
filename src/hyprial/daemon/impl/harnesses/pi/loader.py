"""Typed boundary for Pi's controlled native resource loader.

The JavaScript side uses Pi's installed SDK, but its receipt is a Hyprial
adapter contract.  It deliberately contains only non-secret configuration
metadata; in particular it never serializes auth/model credential values.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

from hyprial.daemon.impl.harnesses.model_provider import pi_model_definitions
from hyprial.kernel import git_subprocess_environment

if TYPE_CHECKING:
    from hyprial.identity import AgentRuntimeContext

PI_CONTROLLED_LOADER = Path(__file__).parent / "resources" / "pi_controlled_loader.mjs"
PI_LOADER_RECEIPT_SCHEMA = "hyprial-pi-loader-v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    keys = set(value)
    if keys != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - keys)!r}, "
            f"unknown={sorted(keys - expected)!r}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _absolute_path(value: object, label: str) -> str:
    path = _string(value, label)
    if not Path(path).is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


@dataclass(frozen=True, slots=True)
class PiLoadedFile:
    scope: str
    path: str
    relative_path: str
    digest: str
    size: int

    @classmethod
    def from_json(cls, value: object) -> "PiLoadedFile":
        if not isinstance(value, Mapping):
            raise ValueError("pi loaded file must be an object")
        _exact_keys(
            value,
            {"scope", "path", "relativePath", "digest", "size"},
            "pi loaded file",
        )
        scope = _string(value["scope"], "pi loaded file scope")
        if scope not in {"native", "project", "approved"}:
            raise ValueError(f"unsupported pi loaded file scope: {scope!r}")
        digest = _string(value["digest"], "pi loaded file digest")
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("pi loaded file digest must be lowercase SHA-256")
        size = value["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("pi loaded file size must be a non-negative integer")
        return cls(
            scope=scope,
            path=_absolute_path(value["path"], "pi loaded file path"),
            relative_path=_string(
                value["relativePath"], "pi loaded file relative path"
            ),
            digest=digest,
            size=size,
        )


@dataclass(frozen=True, slots=True)
class PiLoadedSkill:
    name: str
    path: str
    scope: str

    @classmethod
    def from_json(cls, value: object) -> "PiLoadedSkill":
        if not isinstance(value, Mapping):
            raise ValueError("pi loaded skill must be an object")
        _exact_keys(value, {"name", "path", "scope"}, "pi loaded skill")
        scope = _string(value["scope"], "pi loaded skill scope")
        if scope not in {"native", "project"}:
            raise ValueError(f"unsupported pi loaded skill scope: {scope!r}")
        return cls(
            name=_string(value["name"], "pi loaded skill name"),
            path=_absolute_path(value["path"], "pi loaded skill path"),
            scope=scope,
        )


@dataclass(frozen=True, slots=True)
class PiEffectiveSettings:
    default_provider: str | None
    default_model: str | None
    default_thinking_level: str | None

    @classmethod
    def from_json(cls, value: object) -> "PiEffectiveSettings":
        if not isinstance(value, Mapping):
            raise ValueError("pi effective settings must be an object")
        _exact_keys(
            value,
            {"defaultProvider", "defaultModel", "defaultThinkingLevel"},
            "pi effective settings",
        )
        fields: list[str | None] = []
        for key in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
            item = value[key]
            if item is not None and (not isinstance(item, str) or not item):
                raise ValueError(f"pi effective setting {key} must be string or null")
            fields.append(item)
        return cls(*fields)


@dataclass(frozen=True, slots=True)
class PiSessionLoadProof:
    system_prompt_digest: str
    present_context_digests: tuple[str, ...]

    @classmethod
    def from_json(cls, value: object) -> "PiSessionLoadProof":
        if not isinstance(value, Mapping):
            raise ValueError("pi session load proof must be an object")
        _exact_keys(
            value,
            {"systemPromptDigest", "presentContextDigests"},
            "pi session load proof",
        )
        system_prompt_digest = _string(
            value["systemPromptDigest"], "pi system prompt digest"
        )
        if _SHA256.fullmatch(system_prompt_digest) is None:
            raise ValueError("pi system prompt digest must be lowercase SHA-256")
        digests = value["presentContextDigests"]
        if not isinstance(digests, list):
            raise ValueError("pi present context digests must be an array")
        for digest in digests:
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ValueError("pi present context digest must be lowercase SHA-256")
        return cls(system_prompt_digest, tuple(digests))


#: Highest pi package version this tree carries evidence for.
#:
#: Allen 2026-10-18 ruled the guard should admit a *range* instead of a
#: single build: the P23-pi capability arms were re-run against the installed
#: pi 1.3.0 and all thirteen pass (the only failure before the ruling was the
#: equality pin itself, not a behaviour).  Raising this constant is the one
#: place a future pi upgrade must be recorded -- the capability arms stay as
#: they are, so a version beyond this value still fails loudly.
PI_SUPPORTED_MAX_VERSION = "1.3.0"


def _version_parts(value: str) -> tuple[int, ...]:
    """Numeric parts of a dotted version, ignoring any non-numeric suffix."""

    parts: list[int] = []
    for chunk in value.split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def pi_version_supported(version: str) -> bool:
    """Whether ``version`` is within the proven range (<= max supported)."""

    return _version_parts(version) <= _version_parts(PI_SUPPORTED_MAX_VERSION)


@dataclass(frozen=True, slots=True)
class PiLoaderReceipt:
    pi_package_version: str
    cwd: str
    repository_root: str
    projection_root: str
    native_root: str
    home_root: str
    project_trusted: bool
    context_files: tuple[PiLoadedFile, ...]
    skills: tuple[PiLoadedSkill, ...]
    effective_settings: PiEffectiveSettings
    session_proof: PiSessionLoadProof

    @classmethod
    def from_json(cls, value: object) -> "PiLoaderReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("pi loader receipt must be an object")
        _exact_keys(
            value,
            {
                "schema",
                "piPackageVersion",
                "cwd",
                "repositoryRoot",
                "projectionRoot",
                "nativeRoot",
                "homeRoot",
                "projectTrusted",
                "contextFiles",
                "skills",
                "effectiveSettings",
                "sessionProof",
            },
            "pi loader receipt",
        )
        if value["schema"] != PI_LOADER_RECEIPT_SCHEMA:
            raise ValueError(f"unsupported pi loader receipt schema: {value['schema']!r}")
        trusted = value["projectTrusted"]
        if trusted is not True:
            raise ValueError("pi controlled loader receipt must record explicit trust")
        context_files = value["contextFiles"]
        skills = value["skills"]
        if not isinstance(context_files, list) or not isinstance(skills, list):
            raise ValueError("pi loader receipt resources must be arrays")
        return cls(
            pi_package_version=_string(
                value["piPackageVersion"], "pi package version"
            ),
            cwd=_absolute_path(value["cwd"], "pi loader cwd"),
            repository_root=_absolute_path(
                value["repositoryRoot"], "pi repository root"
            ),
            projection_root=_absolute_path(
                value["projectionRoot"], "pi projection root"
            ),
            native_root=_absolute_path(value["nativeRoot"], "pi native root"),
            home_root=_absolute_path(value["homeRoot"], "pi home root"),
            project_trusted=True,
            context_files=tuple(PiLoadedFile.from_json(item) for item in context_files),
            skills=tuple(PiLoadedSkill.from_json(item) for item in skills),
            effective_settings=PiEffectiveSettings.from_json(
                value["effectiveSettings"]
            ),
            session_proof=PiSessionLoadProof.from_json(value["sessionProof"]),
        )

    @classmethod
    def from_text(cls, text: str) -> "PiLoaderReceipt":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("pi loader receipt is not valid JSON") from error
        return cls.from_json(value)


def pi_loader_probe_argv(
    *,
    node: str,
    pi_package_root: Path,
    cwd: Path,
    repository_root: Path,
    projection_root: Path,
    native_root: Path,
    home_root: Path,
    trust_scope: str = "repository",
) -> tuple[str, ...]:
    """Build the model-free SDK probe command with every authority explicit."""

    payload = {
        "piPackageRoot": str(pi_package_root),
        "cwd": str(cwd),
        "repositoryRoot": str(repository_root),
        "trustScope": trust_scope,
        "projectionRoot": str(projection_root),
        "nativeRoot": str(native_root),
        "homeRoot": str(home_root),
        "projectTrusted": True,
    }
    return (
        node,
        str(PI_CONTROLLED_LOADER),
        "probe",
        json.dumps(payload, separators=(",", ":")),
    )


class PiStartPreconditionError(ValueError):
    """A deterministic P2 configuration refusal that retry cannot heal."""

    code = "HARNESS_START_FAILED"
    permanent_start_failure = True
    start_precondition_failure = True


class PiProjectTrustError(PiStartPreconditionError):
    """Pi P2 launch trust could not be signed for this cwd.

    A launch-validation refusal, not a transient harness failure: the code
    travels into the daemon's start-rejection path, and the streaming pump
    must stop retrying it.
    """

    code = "PI_P2_LAUNCH_UNTRUSTED"


def _lexical_path(path: Path) -> Path:
    """Normalize without resolving symlinks (resolve would erase the link)."""

    return Path(os.path.normpath(os.fspath(path)))


def _reject_workspace_symlink_chain(workspace: Path) -> None:
    """Refuse a workspace whose root or agent-home ancestors are symlinks.

    Anchored at the agent collection (``workspace.parent.parent``) so that
    system-level ancestors such as macOS ``/var`` are outside the check; the
    components the runtime context owns must be real directories.
    """

    if not workspace.is_absolute():
        raise ValueError("Pi workspace trust paths must be absolute")
    for directory in (workspace.parent.parent, workspace.parent, workspace):
        try:
            metadata = directory.lstat()
        except OSError as error:
            raise ValueError(
                f"Pi workspace trust path {directory} cannot be inspected: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(
                "Pi workspace trust root must be a real directory, not a symlink"
            )


@dataclass(frozen=True, slots=True)
class PiProjectTrustReceipt:
    """Explicit approved project boundary; never discovered from cwd here.

    ``scope`` distinguishes the two sanctioned roots: a Git worktree
    (``repository``) or the agent's own private P2 home workspace
    (``workspace``), which is a valid pi project even though it is not a
    repository.  Nothing else qualifies -- see ``resolve_approved_pi_project``.
    """

    cwd: Path
    repository_root: Path
    trusted: bool
    scope: str = "repository"

    def __post_init__(self) -> None:
        if self.trusted is not True:
            raise ValueError("Pi P2 launch requires explicit project trust")
        if self.scope not in {"repository", "workspace"}:
            raise ValueError("Pi project trust scope must be repository or workspace")
        if self.scope == "workspace":
            # Compare and inspect BEFORE resolving: resolve() erases exactly
            # the symlink this check exists to reject.
            if not self.cwd.is_absolute() or not self.repository_root.is_absolute():
                raise ValueError("Pi workspace trust paths must be absolute")
            if _lexical_path(self.cwd) != _lexical_path(self.repository_root):
                raise ValueError("Pi workspace trust root must equal cwd")
            _reject_workspace_symlink_chain(self.repository_root)
        cwd = self.cwd.resolve(strict=True)
        root = self.repository_root.resolve(strict=True)
        if not cwd.is_dir() or not root.is_dir():
            raise ValueError("Pi project trust paths must be directories")
        try:
            cwd.relative_to(root)
        except ValueError as error:
            raise ValueError("Pi project cwd is outside the approved root") from error
        if self.scope == "workspace":
            # The workspace root is the cwd itself and must stay private and
            # non-Git: a wider or repository-backed root is out of contract.
            if cwd != root:
                raise ValueError("Pi workspace trust root must equal cwd")
            if root.stat().st_mode & 0o077:
                raise ValueError("Pi workspace trust root must be private to its owner")
            if (root / ".git").exists():
                raise ValueError("Pi workspace trust root must not be a Git worktree")
            object.__setattr__(self, "cwd", cwd)
            object.__setattr__(self, "repository_root", root)
            return
        marker = root / ".git"
        if not marker.exists() or marker.is_symlink():
            raise ValueError("Pi approved repository root has no .git boundary")
        if not marker.is_file() and not marker.is_dir():
            raise ValueError("Pi approved repository root has invalid .git boundary")
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "repository_root", root)


@dataclass(frozen=True, slots=True)
class PiSdkLaunch:
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    projection_root: Path
    native_root: Path
    session_root: Path
    receipt_path: Path


def _runtime_root(value: object, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{label} must be path-like")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path.resolve(strict=True)


def find_pi_package_root(
    command: tuple[str, ...], environment: Mapping[str, str]
) -> Path:
    """Resolve the installed SDK beside the exact Pi command being replaced."""

    if not command:
        raise ValueError("Pi command must not be empty")
    executable = command[0]
    candidate = (
        shutil.which(executable, path=environment.get("PATH"))
        if os.sep not in executable
        else executable
    )
    if candidate is None:
        raise ValueError(f"Pi command is unavailable: {executable!r}")
    resolved = Path(candidate).resolve(strict=True)
    for parent in (resolved, *resolved.parents):
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if body.get("name") == "@earendil-works/pi-coding-agent":
            return parent
    raise ValueError(
        f"Pi SDK package root not found beside {resolved}; a pi installation "
        "with the @earendil-works/pi-coding-agent SDK is required for P2 launches"
    )


# The connector owns the output mode and the session identity; strip the
# caller's session controls while preserving unrelated passthrough flags.
_RESERVED_PI_VALUE_OPTIONS = frozenset(
    {"--mode", "--session-id", "--session", "--fork"}
)
_RESERVED_PI_FLAG_OPTIONS = frozenset(
    {"--print", "-p", "--continue", "-c", "--resume", "-r"}
)


def without_pi_reserved_arguments(args: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in _RESERVED_PI_VALUE_OPTIONS:
            index += 2
            continue
        if value in _RESERVED_PI_FLAG_OPTIONS:
            index += 1
            continue
        if any(value.startswith(f"{option}=") for option in _RESERVED_PI_VALUE_OPTIONS):
            index += 1
            continue
        result.append(value)
        index += 1
    return tuple(result)


def _pi_recovery_command(
    *,
    name: str | None,
    cwd: str,
    runtime_args: tuple[str, ...],
    headless: bool,
    tmux: bool,
    model_provider: str | None,
    model: str | None,
    session_ref: str | None,
    json_output: bool,
) -> str:
    argv = ["hyprial", "agent", "start", "pi", "--name", name or "<agent-name>"]
    if headless:
        argv.append("--headless")
    elif tmux:
        argv.append("--tmux")
    argv.extend(("--cwd", cwd))
    if model_provider is not None:
        argv.extend(("--provider", model_provider))
    if model is not None:
        argv.extend(("--model", model))
    if session_ref is not None:
        argv.extend(("--resume", session_ref))

    pi_args = ["--approve"]
    misplaced_json = False
    index = 0
    while index < len(runtime_args):
        value = runtime_args[index]
        if value in {"--provider", "--model"} and index + 1 < len(runtime_args):
            pi_args.extend((value, runtime_args[index + 1]))
            index += 2
            continue
        if value.startswith(("--provider=", "--model=")) or value in {
            "--approve",
            "--offline",
        }:
            if value not in pi_args:
                pi_args.append(value)
        elif value == "--json":
            misplaced_json = True
        index += 1
    if json_output or misplaced_json:
        argv.append("--json")
    return shlex.join([*argv, "--", *pi_args])


def resolve_approved_pi_project(
    *,
    cwd: str | None,
    runtime_args: tuple[str, ...],
    own_workspace: Path | None = None,
    command_name: str | None = None,
    command_headless: bool = True,
    command_tmux: bool = False,
    command_model_provider: str | None = None,
    command_model: str | None = None,
    command_session_ref: str | None = None,
    command_json_output: bool = False,
) -> PiProjectTrustReceipt:
    """Turn Pi's explicit per-launch approval into a bounded trust receipt.

    Two roots are sanctioned.  A cwd inside a Git worktree is trusted up to
    that worktree's top level.  Otherwise -- and only when ``own_workspace``
    is supplied -- the cwd is trusted if it IS this agent's own private P2
    home workspace (0700, non-Git, no symlink).  Neither rule widens to an
    arbitrary directory, and another agent's workspace never qualifies.
    """

    if cwd is None:
        raise PiProjectTrustError("Pi P2 launch requires an explicit cwd")

    def recovery_command(*, git_cwd: bool = False) -> str:
        return _pi_recovery_command(
            name=command_name,
            cwd=("/absolute/path/to/git-working-tree" if git_cwd else cwd),
            runtime_args=runtime_args,
            headless=command_headless,
            tmux=command_tmux,
            model_provider=command_model_provider,
            model=command_model,
            session_ref=command_session_ref,
            json_output=command_json_output,
        )

    approved = False
    index = 0
    while index < len(runtime_args):
        value = runtime_args[index]
        if value in {"--provider", "--model"}:
            if index + 1 >= len(runtime_args):
                raise ValueError(f"{value} requires a value")
            index += 2
            continue
        if value.startswith("--provider=") or value.startswith("--model="):
            index += 1
            continue
        if value == "--approve":
            approved = True
            index += 1
            continue
        if value == "--offline":
            index += 1
            continue
        raise PiStartPreconditionError(
            f"Pi P2 SDK bridge does not support argument {value!r}. "
            "Arguments after '--' go to Pi; hyprial options such as --json "
            f"belong before '--'. Run: `{recovery_command()}`."
        )
    if not approved:
        raise PiProjectTrustError(
            "Pi P2 headless launch requires explicit --approve trust. "
            "--headless and --tmux share this P2 precondition. "
            f"Run: `{recovery_command()}`."
        )
    resolved_cwd = Path(cwd).resolve(strict=True)
    completed = subprocess.run(
        ["git", "-C", str(resolved_cwd), "rev-parse", "--show-toplevel"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        env=git_subprocess_environment(),
    )
    if completed.returncode == 0:
        line = completed.stdout.strip()
        if not line or "\n" in line:
            raise ValueError("Pi P2 Git root discovery returned an invalid path")
        try:
            return PiProjectTrustReceipt(resolved_cwd, Path(line), True)
        except ValueError as error:
            raise PiProjectTrustError(str(error)) from error
    if own_workspace is not None:
        own = Path(own_workspace)
        lexical_cwd = Path(cwd)
        if own.is_absolute() and lexical_cwd.is_absolute():
            try:
                own.resolve(strict=True)
            except OSError:
                exists = False
            else:
                exists = True
            # Match the supplied paths without resolving: a workspace symlink
            # would otherwise canonicalize onto another agent's workspace.
            if exists and _lexical_path(lexical_cwd) == _lexical_path(own):
                try:
                    return PiProjectTrustReceipt(lexical_cwd, own, True, "workspace")
                except ValueError as error:
                    raise PiProjectTrustError(str(error)) from error
    raise PiProjectTrustError(
        "Pi P2 cwd is not inside an approved Git repository. A qualifying "
        "cwd is a directory inside a Git working tree. "
        f"Run: `{recovery_command(git_cwd=True)}`."
    )


def pi_sdk_launch_from_runtime_context(
    context: "AgentRuntimeContext",
    *,
    trust: PiProjectTrustReceipt,
    mode: Literal["rpc", "tui"],
    session_id: str,
    require_existing_session: bool = False,
    pi_package_root: Path,
    node: str = "node",
    model_provider: str | None = None,
    model: str | None = None,
    model_environment: Mapping[str, str] | None = None,
    additional_extension_paths: tuple[Path, ...] = (),
    additional_skill_paths: tuple[Path, ...] = (),
    append_system_prompt: tuple[str, ...] = (),
    session_name: str | None = None,
    initial_message: str | None = None,
    containerized: bool = False,
) -> PiSdkLaunch:
    """Consume P22's context without deriving or rewriting any agent root."""

    return _pi_sdk_launch(
        projection_root=context.roots.projection_root,
        native_root=context.roots.native_root,
        session_root=context.roots.session_root,
        environment=context.environment(),
        trust=trust,
        mode=mode,
        session_id=session_id,
        require_existing_session=require_existing_session,
        pi_package_root=pi_package_root,
        node=node,
        model_provider=model_provider,
        model=model,
        model_environment=model_environment,
        additional_extension_paths=additional_extension_paths,
        additional_skill_paths=additional_skill_paths,
        append_system_prompt=append_system_prompt,
        session_name=session_name,
        initial_message=initial_message,
        containerized=containerized,
    )


def pi_sdk_launch_from_public_projection(
    projection: Mapping[str, object],
    *,
    trust: PiProjectTrustReceipt,
    mode: Literal["rpc", "tui"],
    session_id: str,
    require_existing_session: bool = False,
    pi_package_root: Path,
    node: str = "node",
    model_provider: str | None = None,
    model: str | None = None,
    model_environment: Mapping[str, str] | None = None,
    additional_extension_paths: tuple[Path, ...] = (),
    additional_skill_paths: tuple[Path, ...] = (),
    append_system_prompt: tuple[str, ...] = (),
    session_name: str | None = None,
    initial_message: str | None = None,
) -> PiSdkLaunch:
    """Consume P22's non-secret daemon projection for an interactive launch."""

    if projection.get("mode") != "agent-home-p2" or projection.get("harness") != "pi":
        raise ValueError("daemon projection is not a Pi agent-home P2 context")
    environment = projection.get("environment")
    if not isinstance(environment, Mapping) or any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in environment.items()
    ):
        raise ValueError("daemon projection has an invalid runtime environment")
    return _pi_sdk_launch(
        projection_root=projection.get("projectionRoot"),
        native_root=projection.get("nativeRoot"),
        session_root=projection.get("sessionRoot"),
        environment=environment,
        trust=trust,
        mode=mode,
        session_id=session_id,
        require_existing_session=require_existing_session,
        pi_package_root=pi_package_root,
        node=node,
        model_provider=model_provider,
        model=model,
        model_environment=model_environment,
        additional_extension_paths=additional_extension_paths,
        additional_skill_paths=additional_skill_paths,
        append_system_prompt=append_system_prompt,
        session_name=session_name,
        initial_message=initial_message,
        containerized=False,
    )


def _pi_sdk_launch(
    *,
    projection_root: object,
    native_root: object,
    session_root: object,
    environment: Mapping[str, str],
    trust: PiProjectTrustReceipt,
    mode: Literal["rpc", "tui"],
    session_id: str,
    require_existing_session: bool,
    pi_package_root: Path,
    node: str,
    model_provider: str | None,
    model: str | None,
    model_environment: Mapping[str, str] | None,
    additional_extension_paths: tuple[Path, ...],
    additional_skill_paths: tuple[Path, ...],
    append_system_prompt: tuple[str, ...],
    session_name: str | None,
    initial_message: str | None,
    containerized: bool,
) -> PiSdkLaunch:
    if containerized:
        raise ValueError("Pi controlled SDK bridge is not supported in containers")
    if not session_id:
        raise ValueError("Pi session id must be non-empty")
    if (model_provider is None) != (model is None):
        raise ValueError("Pi model provider and model must be supplied together")
    projection_root = _runtime_root(projection_root, "Pi projection root")
    native_root = _runtime_root(native_root, "Pi native root")
    session_root = _runtime_root(session_root, "Pi session root")
    if projection_root == native_root:
        raise ValueError("Pi projection and native roots must remain distinct")
    environment = dict(environment)
    expected_native = environment.get("PI_CODING_AGENT_DIR")
    if expected_native != str(native_root):
        raise ValueError("P22 runtime environment does not bind Pi native root")
    home_value = environment.get("HOME")
    if not isinstance(home_value, str) or not Path(home_value).is_absolute():
        raise ValueError("P22 runtime environment does not provide an absolute HOME")
    home_root = Path(home_value).resolve(strict=True)
    package_root = _runtime_root(pi_package_root, "Pi package root")
    extension_paths = tuple(
        _runtime_root(path, "Pi additional extension")
        for path in additional_extension_paths
    )
    skill_paths = tuple(
        _runtime_root(path, "Pi additional skill") for path in additional_skill_paths
    )
    receipt_name = sha256(session_id.encode("utf-8")).hexdigest() + ".json"
    receipt_path = session_root / ".hyprial-loader-receipts" / receipt_name
    payload: dict[str, object] = {
        "piPackageRoot": str(package_root),
        "cwd": str(trust.cwd),
        "repositoryRoot": str(trust.repository_root),
        "trustScope": trust.scope,
        "projectionRoot": str(projection_root),
        "nativeRoot": str(native_root),
        "homeRoot": str(home_root),
        "projectTrusted": True,
        "sessionRoot": str(session_root),
        "sessionId": session_id,
        "requireExistingSession": require_existing_session,
        "receiptPath": str(receipt_path),
        "additionalExtensionPaths": [str(path) for path in extension_paths],
        "additionalSkillPaths": [str(path) for path in skill_paths],
        "appendSystemPrompt": list(append_system_prompt),
        # The agent's native root has no models config under P2, so hyprial's
        # canonical model definitions travel with every launch.
        "modelDefinitions": pi_model_definitions(model_environment or environment),
    }
    if model_provider is not None:
        payload["modelProvider"] = model_provider
        payload["model"] = model
    if session_name is not None:
        payload["sessionName"] = session_name
    if initial_message is not None:
        payload["initialMessage"] = initial_message
    return PiSdkLaunch(
        argv=(
            node,
            str(PI_CONTROLLED_LOADER),
            f"run-{mode}",
            json.dumps(payload, separators=(",", ":")),
        ),
        environment=environment,
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        receipt_path=receipt_path,
    )
