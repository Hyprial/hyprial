"""Idempotent seven-step Squire setup orchestration."""

from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import shutil
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Protocol, runtime_checkable

from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.management import (
    EnsureSquireRegistryCommand,
    RegistryManagementHandler,
    SquireRegistryResult,
)

from .profile import UserProfileStore
# The one string that tells a rendered skill whether it knows about
# attachments. Checked rather than a version number: the file is the user's and
# may have been edited, so what matters is whether the instruction is present,
# not which template it came from.
SKILL_ATTACHMENT_MARKER = "--image"


BINDING_TTL_SECONDS = 10 * 60
SETUP_STEPS = (
    "create-squire",
    "create-app",
    "visibility",
    "owner-open-id",
    "owner-access",
    "claude-plugin",
    "service",
)


def identity_slug(value: str) -> str:
    """A host-side lookup-key slug for an identity segment (owner/machine).

    ``owner_key``/``machine_key`` are not credentials: they are dict keys for
    the binding store and components of the squire session reference
    (``squire:<owner_key>:<machine_key>``), so a deterministic normalization
    of the identity they label is a sound default for them.
    """

    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a lookup key from {value!r}")
    return slug


def node_machine_id(environ: Mapping[str, str] | None = None) -> str:
    """The daemon node id setup targets: ``HYPRIAL_NODE_ID``, else hostname.

    Same source order ``_cross_checks`` reads, so a derived ``machine`` can
    never disagree with the node-id check by construction; only an explicit
    ``--machine`` override can, and that keeps its loud error.
    """

    source = os.environ if environ is None else environ
    configured = source.get("HYPRIAL_NODE_ID", "").strip()
    return configured or socket.gethostname()


def platform_login_name() -> str:
    """This account's OS login: an OS fact, not the owner identity.

    The login *name* is a host-side lookup/display key (``users.json``
    ``loginName`` has always meant the host login), so unlike
    ``resolve_node_owner`` there is no "no host-login fallback" rule to
    violate — but the same discipline applies: read the current UID, not the
    spoofable ``USER`` env, mirroring ``transfer.orchestrator``.
    """

    return pwd.getpwuid(os.getuid()).pw_name


def derive_setup_identity(
    owner: str,
    *,
    owner_key: str | None = None,
    login_name: str | None = None,
    machine: str | None = None,
    machine_key: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SetupIdentity:
    """Fill the four host-side lookup fields that were once required flags.

    Derivation (Allen, 2026-09-18): ``owner`` comes from the login identity
    as before; ``machine`` = ``HYPRIAL_NODE_ID`` else hostname;
    ``owner_key``/``machine_key`` = slug of owner/machine; ``login_name`` =
    the platform (OS) login. Explicit values override their derivation one
    for one — an explicit ``--machine`` that disagrees with a configured node
    id still fails in ``_cross_checks``.
    """

    resolved_machine = node_machine_id(environ) if machine is None else machine
    return SetupIdentity(
        owner=owner,
        owner_key=identity_slug(owner) if owner_key is None else owner_key,
        login_name=platform_login_name() if login_name is None else login_name,
        machine=resolved_machine,
        machine_key=(
            identity_slug(resolved_machine) if machine_key is None else machine_key
        ),
    )


@dataclass(frozen=True, slots=True)
class SetupIdentity:
    owner: str
    owner_key: str
    login_name: str
    machine: str
    machine_key: str

    @property
    def actor(self) -> str:
        # The one constructor, always: URI 产生点唯一 (Allen).
        from hyprial.uri import canonical_agent_uri

        return canonical_agent_uri(self.owner, self.machine, "squire")


@dataclass(frozen=True, slots=True)
class AppOnboardingResult:
    status: str
    code: str
    detail: str


@runtime_checkable
class AppOnboardingPort(Protocol):
    """Future app-creation seam; P4 deliberately ships no platform workflow."""

    def prepare(self, identity: SetupIdentity) -> AppOnboardingResult: ...


class DeferredAppOnboarding:
    def prepare(self, identity: SetupIdentity) -> AppOnboardingResult:
        del identity
        return AppOnboardingResult(
            status="deferred",
            code="APP_CREATION_DEFERRED",
            detail=(
                "Create and authorize the dedicated app externally, then rerun "
                "setup with its channel URI."
            ),
        )


@runtime_checkable
class SquireManagementPort(Protocol):
    """Typed live-or-fenced authority for Squire registry mutations."""

    def ensure_squire(
        self, command: EnsureSquireRegistryCommand
    ) -> SquireRegistryResult: ...


@dataclass(frozen=True, slots=True)
class PendingBinding:
    channel: str
    code: str
    expires_at: int

    def to_json(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "code": self.code,
            "expiresAt": self.expires_at,
        }


class SetupStateStore:
    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self._lock = threading.RLock()

    def ensure_binding(self, owner_key: str, channel: str) -> PendingBinding:
        with self._lock:
            bindings = self._load()
            current = bindings.get(owner_key)
            now = int(self._now())
            if (
                current is not None
                and current.channel == channel
                and current.expires_at > now
            ):
                return current
            binding = PendingBinding(
                channel=channel,
                code=secrets.token_urlsafe(6),
                expires_at=now + BINDING_TTL_SECONDS,
            )
            bindings[owner_key] = binding
            self._save(bindings)
            return binding

    def verify(self, owner_key: str, channel: str, code: str) -> None:
        with self._lock:
            binding = self._load().get(owner_key)
            if (
                binding is None
                or binding.channel != channel
                or binding.code != code
                or binding.expires_at <= int(self._now())
            ):
                raise ValueError("binding code is invalid or expired")

    def consume(self, owner_key: str) -> None:
        with self._lock:
            bindings = self._load()
            if owner_key not in bindings:
                return
            del bindings[owner_key]
            self._save(bindings)

    def _load(self) -> dict[str, PendingBinding]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("unsupported squire setup state; expected version 1")
        values = raw.get("pendingBindings")
        if not isinstance(values, dict):
            raise TypeError("squire setup pendingBindings must be an object")
        result: dict[str, PendingBinding] = {}
        for owner_key, candidate in values.items():
            if not isinstance(owner_key, str) or not isinstance(candidate, dict):
                raise TypeError("invalid squire setup binding")
            channel = candidate.get("channel")
            code = candidate.get("code")
            expires_at = candidate.get("expiresAt")
            if (
                not isinstance(channel, str)
                or not isinstance(code, str)
                or not isinstance(expires_at, int)
            ):
                raise TypeError("invalid squire setup binding")
            result[owner_key] = PendingBinding(channel, code, expires_at)
        return result

    def _save(self, bindings: dict[str, PendingBinding]) -> None:
        payload = {
            "version": 1,
            "pendingBindings": {
                key: bindings[key].to_json() for key in sorted(bindings)
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


class SquireSetup:
    def __init__(
        self,
        *,
        hyprial_home: Path,
        state_dir: Path,
        app_onboarding: AppOnboardingPort | None = None,
        claude_available: Callable[[], bool] | None = None,
        management_port: SquireManagementPort | RegistryManagementHandler | None = None,
        configured_adapters: Callable[[], tuple[str, ...]] | None = None,
        skill_template: str | None = None,
    ) -> None:
        self.hyprial_home = Path(hyprial_home)
        self.state_dir = Path(state_dir)
        self.profiles = UserProfileStore(self.state_dir / "users.json")
        self.setup_state = SetupStateStore(self.state_dir / "squire-setup.json")
        self.app_onboarding = app_onboarding or DeferredAppOnboarding()
        self.claude_available = claude_available or (
            lambda: shutil.which("claude") is not None
        )
        self.management_port = management_port
        self.configured_adapters = configured_adapters or self._configured_adapters
        self.skill_template = skill_template

    def run(
        self,
        identity: SetupIdentity,
        *,
        channel: str | None = None,
        owner_open_id: str | None = None,
        binding_code: str | None = None,
        squire_home: Path | None = None,
        display_name: str | None = None,
        adapter: str | None = None,
        dm_route: str = "owner",
        provider: str = "deepseek",
        model: str = "deepseek-flash",
        preferred_harness: str = "pi",
        start: bool = False,
        step: str | None = None,
    ) -> dict[str, object]:
        self._validate(
            identity,
            channel,
            owner_open_id,
            binding_code,
            display_name,
            adapter,
            dm_route,
            provider,
            model,
            preferred_harness,
            start,
            step,
        )
        if (step is None or step == "create-squire") and self.management_port is None:
            raise ValueError("create-squire requires a management authority")
        if adapter is not None:
            available_adapters = self.configured_adapters()
            if adapter not in available_adapters:
                raise ValueError(
                    f"--adapter {adapter!r} is not configured in "
                    f"{self.hyprial_home / 'channels.json'} (available: "
                    f"{', '.join(available_adapters) or 'none'}); run "
                    f"'hyprial adapter onboard {adapter} --new' first"
                )
        return self._run(
            identity,
            channel=channel,
            owner_open_id=owner_open_id,
            binding_code=binding_code,
            squire_home=squire_home,
            display_name=display_name,
            adapter=adapter,
            dm_route=dm_route,
            provider=provider,
            model=model,
            preferred_harness=preferred_harness,
            start=start,
            step=step,
        )

    def _run(
        self,
        identity: SetupIdentity,
        *,
        channel: str | None,
        owner_open_id: str | None,
        binding_code: str | None,
        squire_home: Path | None,
        display_name: str | None,
        adapter: str | None,
        dm_route: str,
        provider: str,
        model: str,
        preferred_harness: str,
        start: bool,
        step: str | None,
    ) -> dict[str, object]:
        self._validate(
            identity,
            channel,
            owner_open_id,
            binding_code,
            display_name,
            adapter,
            dm_route,
            provider,
            model,
            preferred_harness,
            start,
            step,
        )
        warnings = self._cross_checks(identity, channel)
        selected = set(SETUP_STEPS) if step is None else {step}
        changed: list[str] = []
        squire_cwd = (squire_home or Path.home() / "squire").expanduser().resolve()
        skill_dir = squire_cwd / "skills" / "squire"
        resolved_display_name = display_name or identity.owner
        expected_adapter = f"{identity.owner}-squire"
        scaffold_adapter = adapter or expected_adapter
        session_ref = f"squire:{identity.owner_key}:{identity.machine_key}"
        runtime_args = self._runtime_args(
            preferred_harness,
            provider=provider,
            model=model,
            skill_dir=skill_dir,
        )
        spec = HarnessLaunchSpec(
            harness=preferred_harness,
            name="squire",
            headless=True,
            args=runtime_args,
            nickname="Squire",
            cwd=str(squire_cwd),
            session_ref=session_ref,
        )
        adapter_pinned = False
        worker_result: dict[str, object] = {"started": False}
        profile = self.profiles.get(identity.owner_key)
        binding_already_matches = (
            owner_open_id is not None
            and channel is not None
            and profile is not None
            and profile.owner_open_id is not None
            and profile.owner_open_id.channel == channel
            and profile.owner_open_id.open_id == owner_open_id
        )
        if owner_open_id is not None and not binding_already_matches:
            assert channel is not None and binding_code is not None
            # Verify before every local mutation. A stale or wrong-channel code
            # must not partially change desired state or the user profile.
            self.setup_state.verify(identity.owner_key, channel, binding_code)
        # ⚠️ Read what is on disk BEFORE the scaffold runs. A freshly created
        # skill carries the instructions, so checking afterwards would never
        # report anything -- and the case worth reporting is precisely the one
        # where an older file was left in place untouched.
        stale_skill = self._stale_skill_warning(squire_cwd)
        if "create-squire" in selected:
            changed.extend(
                self._ensure_scaffold(
                    squire_cwd,
                    display_name=resolved_display_name,
                    login_name=identity.login_name,
                    owner=identity.owner,
                    adapter=scaffold_adapter,
                    dm_route=dm_route,
                )
            )
            assert self.management_port is not None
            managed = self.management_port.ensure_squire(
                EnsureSquireRegistryCommand(
                    identity.owner,
                    identity.machine,
                    spec,
                    str(squire_cwd),
                    provider,
                    model,
                    preferred_harness,
                    adapter,
                    start,
                )
            )
            changed.extend(managed.changed)
            worker_result = managed.worker
            adapter_pinned = adapter is not None
            profile, fields = self.profiles.ensure(
                owner=identity.owner,
                owner_key=identity.owner_key,
                login_name=identity.login_name,
                machine=identity.machine,
                machine_key=identity.machine_key,
            )
            changed.extend(fields)
            profile, fields = self.profiles.associate_agent(
                identity.owner_key, identity.actor
            )
            changed.extend(fields)

        binding_step: dict[str, object] = {
            "id": "owner-open-id",
            "status": "actionRequired",
            "detail": "Run this setup step to configure the owner open_id.",
        }
        if "owner-open-id" in selected and profile is None:
            profile, fields = self.profiles.ensure(
                owner=identity.owner,
                owner_key=identity.owner_key,
                login_name=identity.login_name,
                machine=identity.machine,
                machine_key=identity.machine_key,
            )
            changed.extend(fields)
        if "owner-open-id" in selected and channel is not None:
            assert profile is not None
            profile, fields = self.profiles.set_squire_channel(
                identity.owner_key, channel
            )
            changed.extend(fields)

        if "owner-open-id" in selected and owner_open_id is not None:
            assert channel is not None and binding_code is not None
            assert profile is not None
            profile, fields = self.profiles.bind_owner_open_id(
                identity.owner_key, channel=channel, open_id=owner_open_id
            )
            changed.extend(fields)
            self.setup_state.consume(identity.owner_key)
            binding_step = {
                "id": "owner-open-id",
                "status": "complete",
                "detail": "App-scoped owner open_id is bound to the Squire channel.",
            }
        elif (
            "owner-open-id" in selected
            and channel is not None
            and profile is not None
            and profile.owner_open_id is None
        ):
            pending = self.setup_state.ensure_binding(identity.owner_key, channel)
            binding_step = {
                "id": "owner-open-id",
                "status": "actionRequired",
                "detail": (
                    "DM the dedicated app, then rerun setup with the captured "
                    "open_id. The sender open_id is logged as senderOpenId in "
                    "the daemon send.received log entry "
                    "(state/logs/daemon.jsonl) once the adapter forwards the DM."
                ),
                "bindingCode": pending.code,
                "expiresAt": pending.expires_at,
            }
        elif (
            "owner-open-id" in selected
            and profile is not None
            and profile.owner_open_id is not None
        ):
            binding_step = {
                "id": "owner-open-id",
                "status": "complete",
                "detail": "App-scoped owner open_id is already bound.",
            }
        elif "owner-open-id" in selected:
            binding_step = {
                "id": "owner-open-id",
                "status": "actionRequired",
                "detail": "Rerun setup with the dedicated Squire channel URI.",
            }

        if "create-app" not in selected:
            app = AppOnboardingResult("deferred", "APP_CREATION_DEFERRED", "")
        elif adapter_pinned:
            app = AppOnboardingResult(
                "complete",
                "ADAPTER_PINNED",
                f"Configured adapter {adapter!r} is pinned to the Squire agent.",
            )
        elif adapter is not None:
            app = AppOnboardingResult(
                "actionRequired",
                "ADAPTER_PIN_PENDING",
                "Run the create-squire step to pin the configured adapter.",
            )
        else:
            deferred = self.app_onboarding.prepare(identity)
            app = AppOnboardingResult(
                deferred.status,
                deferred.code,
                (
                    f"{deferred.detail} Next: run 'hyprial adapter onboard "
                    f"{expected_adapter} --new', then rerun 'hyprial squire setup "
                    f"--adapter {expected_adapter}'."
                ),
            )
        plugin_available = (
            bool(self.claude_available()) if "claude-plugin" in selected else False
        )
        all_steps: list[dict[str, object]] = [
            {
                "id": "create-squire",
                "status": "complete",
                "changed": "desiredState.providers" in changed,
                "detail": (
                    "Squire home, agent entity, and managed worker desired state "
                    "are configured."
                ),
            },
            {
                "id": "create-app",
                "status": app.status,
                "code": app.code,
                "detail": app.detail,
            },
            {
                "id": "visibility",
                "status": "complete",
                "detail": "Choose the app visibility range appropriate for this user.",
            },
            binding_step,
            {
                "id": "owner-access",
                "status": "complete",
                "detail": "No owner-only or cross-user ACL is enforced on the trusted intranet.",
            },
            {
                "id": "claude-plugin",
                "status": "actionRequired" if plugin_available else "notApplicable",
                "detail": (
                    "Install the hyprial plugin in Claude Code."
                    if plugin_available
                    else "Claude Code was not detected."
                ),
            },
            {
                "id": "service",
                "status": "actionRequired",
                "detail": "Run hyprial service after reviewing the host service configuration.",
            },
        ]
        steps = [candidate for candidate in all_steps if candidate["id"] in selected]
        return {
            "ok": True,
            "setup": "squire",
            "schemaVersion": 1,
            "complete": all(step["status"] == "complete" for step in steps),
            "changed": changed,
            **(
                {"warnings": warnings + ([stale_skill] if stale_skill else [])}
                if warnings or stale_skill
                else {}
            ),
            "squire": {
                "provider": provider,
                "preferredHarness": preferred_harness,
                "name": "squire",
                "actor": identity.actor,
                "cwd": str(squire_cwd),
                "home": str(squire_cwd),
                "model": model,
                "skill": str(skill_dir),
                "sessionRef": session_ref,
            },
            "adapter": {
                "name": adapter,
                "pinned": adapter_pinned,
                "dmRoute": dm_route,
            },
            "worker": worker_result,
            **({"profile": profile.to_json()} if profile is not None else {}),
            "steps": steps,
        }

    def _stale_skill_warning(self, home: Path) -> str | None:
        """Say so when an installed skill predates the attachment instructions.

        ⛔ Deliberately does not rewrite it. `_ensure_scaffold` promises never to
        touch an existing byte, and that promise is worth more than this
        paragraph: the file is the user's, and people edit theirs. Breaking it
        to deliver a doc fix would be the worse trade.

        ⚠️ But staying silent is not the alternative. On 2026-08-31 a squire was
        asked for a screenshot, read its own skill, found only the text command,
        and told the user hyprial could not send images -- while `hyprial send --image`
        had worked five days earlier. An install that quietly keeps the old file
        looks exactly like one that is up to date, so the difference has to be
        said out loud.
        """

        skill_path = home / "skills" / "squire" / "SKILL.md"
        if not skill_path.is_file():
            return None
        try:
            existing = skill_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if SKILL_ATTACHMENT_MARKER in existing:
            return None
        return (
            f"{skill_path} predates the image/file instructions and was left "
            "untouched (setup never rewrites an existing skill). Without them a "
            "squire may tell its owner that attachments are unsupported. Add "
            "the `hyprial send --image/--file` section from "
            "docs/squire/SKILL-template.md, or move the file aside and re-run "
            "setup to regenerate it."
        )

    def _ensure_scaffold(
        self,
        home: Path,
        *,
        display_name: str,
        login_name: str,
        owner: str,
        adapter: str,
        dm_route: str,
    ) -> list[str]:
        """Create missing user-owned files and never rewrite an existing byte."""

        skill_path = home / "skills" / "squire" / "SKILL.md"
        template = self.skill_template
        if template is None:
            template = (
                files("hyprial.squire")
                .joinpath("SKILL-template.md")
                .read_text(encoding="utf-8")
            )
        replacements = (
            ("<用户显示名>", display_name),
            ("<用户名>", login_name),
            ("<用户>", display_name),
            ("<owner>", owner),
            ("<adapter>", adapter),
            ("<dm-route>", dm_route),
        )
        rendered = template
        for placeholder, value in replacements:
            rendered = rendered.replace(placeholder, value)

        changed: list[str] = []
        for path, content, field in (
            (skill_path, rendered.encode("utf-8"), "scaffold.skill"),
            (home / "backlog.md", b"", "scaffold.backlog"),
            (home / "profile.md", b"", "scaffold.profile"),
        ):
            if path.is_file():
                continue
            if path.exists():
                raise ValueError(f"Squire scaffold path is not a file: {path}")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            changed.append(field)
        return changed

    def _configured_adapters(self) -> tuple[str, ...]:
        from hyprial.persistent_config import ChannelConfiguration

        path = self.hyprial_home / "channels.json"
        if not path.exists():
            return ()
        raw = json.loads(path.read_text(encoding="utf-8"))
        configuration = ChannelConfiguration.from_json(raw)
        return tuple(sorted(gateway.name for gateway in configuration.gateways))

    @staticmethod
    def _runtime_args(
        harness: str, *, provider: str, model: str, skill_dir: Path
    ) -> tuple[str, ...]:
        if harness == "pi":
            return (
                "--provider",
                provider,
                "--model",
                model,
                "--skill",
                str(skill_dir),
                "--approve",
            )
        return ("--model", model)

    def _cross_checks(self, identity: SetupIdentity, channel: str | None) -> list[str]:
        """Fail or warn on silent-delivery traps before any state is written.

        Two traps turned a healthy-looking setup into permanently undeliverable
        ``user:`` traffic: a profile whose preferred receiver is not this
        daemon's node id, and a squire channel naming an adapter that
        channels.json never configures.
        """

        warnings: list[str] = []
        configured_node = os.environ.get("HYPRIAL_NODE_ID", "").strip()
        if configured_node and identity.machine != configured_node:
            raise ValueError(
                f"--machine {identity.machine!r} does not match the daemon node "
                f"id HYPRIAL_NODE_ID={configured_node!r}; user delivery is owned by "
                "the profile's preferredReceiver.machine and would never be "
                "claimed by this daemon"
            )
        hostname = socket.gethostname()
        if not configured_node and identity.machine != hostname:
            warnings.append(
                f"--machine {identity.machine!r} differs from this host's "
                f"default daemon node id {hostname!r}; set HYPRIAL_NODE_ID on both "
                "the daemon and this setup shell, or user delivery will not be "
                "claimed locally"
            )
        if channel is not None:
            from hyprial.uri import parse_channel_uri

            parsed_channel = parse_channel_uri(channel)
            adapter_name = parsed_channel[2] if parsed_channel is not None else channel
            channels_path = self.hyprial_home / "channels.json"
            if channels_path.exists():
                raw = json.loads(channels_path.read_text(encoding="utf-8"))
                gateways = raw.get("gateways") if isinstance(raw, dict) else None
                names = (
                    sorted(
                        item["name"]
                        for item in gateways
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    )
                    if isinstance(gateways, list)
                    else []
                )
                if adapter_name not in names:
                    raise ValueError(
                        f"--channel names adapter {adapter_name!r}, which is not "
                        f"configured in {channels_path} (available: "
                        f"{', '.join(names) or 'none'}); the daemon registers "
                        "outbound delivery only for configured adapters"
                    )
            else:
                warnings.append(
                    f"{channels_path} does not exist yet; --channel adapter "
                    f"{adapter_name!r} could not be validated, and outbound "
                    "delivery stays unavailable until an adapter with that "
                    "name is configured"
                )
        return warnings

    @staticmethod
    def _validate(
        identity: SetupIdentity,
        channel: str | None,
        owner_open_id: str | None,
        binding_code: str | None,
        display_name: str | None,
        adapter: str | None,
        dm_route: str,
        provider: str,
        model: str,
        preferred_harness: str,
        start: bool,
        step: str | None,
    ) -> None:
        for label, value in (
            ("--owner", identity.owner),
            ("--owner-key", identity.owner_key),
            ("--login-name", identity.login_name),
            ("--machine", identity.machine),
            ("--machine-key", identity.machine_key),
            ("--dm-route", dm_route),
            ("--provider", provider),
            ("--model", model),
            ("--preferred-harness", preferred_harness),
        ):
            if not value:
                raise ValueError(f"{label} must not be empty")
        if display_name is not None and not display_name:
            raise ValueError("--display-name must not be empty")
        if adapter is not None and not adapter:
            raise ValueError("--adapter must not be empty")
        if preferred_harness not in {"claude", "codex", "pi"}:
            raise ValueError("--preferred-harness must be claude, codex, or pi")
        if step is not None and step not in SETUP_STEPS:
            raise ValueError(f"--step must be one of {', '.join(SETUP_STEPS)}")
        if start and step not in {None, "create-squire"}:
            raise ValueError("--start requires the create-squire step")
        if owner_open_id is not None and (channel is None or binding_code is None):
            raise ValueError("--owner-open-id requires --channel and --binding-code")
        if binding_code is not None and owner_open_id is None:
            raise ValueError("--binding-code requires --owner-open-id")
