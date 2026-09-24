"""Typed boundary for Pi's controlled native resource loader.

The JavaScript side uses Pi's installed SDK, but its receipt is a Hyprial
adapter contract.  It deliberately contains only non-secret configuration
metadata; in particular it never serializes auth/model credential values.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

if TYPE_CHECKING:
    from hyprial.agents.runtime import AgentRuntimeContext

PI_CONTROLLED_LOADER = Path(__file__).with_name("pi_controlled_loader.mjs")
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
) -> tuple[str, ...]:
    """Build the model-free SDK probe command with every authority explicit."""

    payload = {
        "piPackageRoot": str(pi_package_root),
        "cwd": str(cwd),
        "repositoryRoot": str(repository_root),
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


@dataclass(frozen=True, slots=True)
class PiProjectTrustReceipt:
    """Explicit approved project boundary; never discovered from cwd here."""

    cwd: Path
    repository_root: Path
    trusted: bool

    def __post_init__(self) -> None:
        if self.trusted is not True:
            raise ValueError("Pi P2 launch requires explicit project trust")
        cwd = self.cwd.resolve(strict=True)
        root = self.repository_root.resolve(strict=True)
        if not cwd.is_dir() or not root.is_dir():
            raise ValueError("Pi project trust paths must be directories")
        try:
            cwd.relative_to(root)
        except ValueError as error:
            raise ValueError("Pi project cwd is outside the approved root") from error
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
    raise ValueError(f"Pi SDK package root not found beside {resolved}")


def resolve_approved_pi_project(
    *, cwd: str | None, runtime_args: tuple[str, ...]
) -> PiProjectTrustReceipt:
    """Turn Pi's explicit per-launch approval into a bounded Git trust receipt."""

    if cwd is None:
        raise ValueError("Pi P2 launch requires an explicit cwd")
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
        raise ValueError(f"Pi P2 SDK bridge does not support argument {value!r}")
    if not approved:
        raise ValueError("Pi P2 headless launch requires explicit --approve trust")
    resolved_cwd = Path(cwd).resolve(strict=True)
    completed = subprocess.run(
        ["git", "-C", str(resolved_cwd), "rev-parse", "--show-toplevel"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("Pi P2 cwd is not inside an approved Git repository")
    line = completed.stdout.strip()
    if not line or "\n" in line:
        raise ValueError("Pi P2 Git root discovery returned an invalid path")
    return PiProjectTrustReceipt(resolved_cwd, Path(line), True)


def pi_sdk_launch_from_runtime_context(
    context: "AgentRuntimeContext",
    *,
    trust: PiProjectTrustReceipt,
    mode: Literal["rpc", "tui"],
    session_id: str,
    pi_package_root: Path,
    node: str = "node",
    model_provider: str | None = None,
    model: str | None = None,
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
        pi_package_root=pi_package_root,
        node=node,
        model_provider=model_provider,
        model=model,
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
    pi_package_root: Path,
    node: str = "node",
    model_provider: str | None = None,
    model: str | None = None,
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
        pi_package_root=pi_package_root,
        node=node,
        model_provider=model_provider,
        model=model,
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
    pi_package_root: Path,
    node: str,
    model_provider: str | None,
    model: str | None,
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
        "projectionRoot": str(projection_root),
        "nativeRoot": str(native_root),
        "homeRoot": str(home_root),
        "projectTrusted": True,
        "sessionRoot": str(session_root),
        "sessionId": session_id,
        "receiptPath": str(receipt_path),
        "additionalExtensionPaths": [str(path) for path in extension_paths],
        "additionalSkillPaths": [str(path) for path in skill_paths],
        "appendSystemPrompt": list(append_system_prompt),
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
