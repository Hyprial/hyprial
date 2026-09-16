"""HYPRIAL_HOME-provided plugin loading for harness launches.

``hyprial start`` sessions must not depend on launch-directory-local harness
registration (for Claude: the per-project ``mcpServers`` section of
``~/.claude.json``).  Everything a session needs beyond the built-in
harness-bridge channel is declared once in HYPRIAL_HOME and injected at launch
time through each harness's own session-scoped mechanism:

==========  =====================  ==========================================
harness     MCP servers            skills / extensions
==========  =====================  ==========================================
claude      ``--mcp-config`` file  ``--plugin-dir`` (session-scoped plugin)
codex       ``-c mcp_servers.*``   unsupported (no session-scoped mechanism)
pi          unsupported            ``--skill`` / ``--extension``
==========  =====================  ==========================================

The manifest lives at ``$HYPRIAL_HOME/plugins/plugins.json``; relative payload
paths resolve inside ``$HYPRIAL_HOME/plugins/``.  A missing manifest is an empty
manifest: every launch still carries the built-in hyprial usage skill.

Injection planning is pure (no subprocess, no daemon); the CLI launchers ask
for a per-harness plan and splice it into their argv. Harness support derives
from ``hyprial.harnesses.capabilities``. A plugin the declared mechanism cannot
carry enters ``skipped`` and the launcher emits it immediately plus returns a
structured warning, so absence can never look like successful loading.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from hyprial.harnesses.capabilities import (
    Capability,
    DECLARED_HARNESSES,
    PLUGIN_KINDS,
    SupportLevel,
    support,
)

#: The channel server every launch injects; user plugins must not shadow it.
RESERVED_MCP_SERVER = "harness-bridge"

_MANIFEST_RELPATH = Path("plugins") / "plugins.json"
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_KINDS = PLUGIN_KINDS

# Derivation direction is deliberate and review-visible:
# capabilities.py owns harness -> session injection mechanism AND the manifest
# payload kinds that mechanism can carry. This module consumes that row; it
# does not restate either fact.


def _declared_plugin_kinds(harness: str) -> tuple[str | None, frozenset[str], str | None]:
    declared = support(
        harness, headless=False, capability=Capability.PLUGIN_INJECTION
    )
    if (
        declared is None
        or declared.level is SupportLevel.UNSUPPORTED
        or declared.mechanism is None
    ):
        return None, frozenset(), declared.note if declared is not None else None
    return (
        declared.mechanism,
        declared.plugin_kinds,
        declared.note,
    )


_HARNESSES = frozenset(
    harness
    for harness in DECLARED_HARNESSES
    if _declared_plugin_kinds(harness)[1]
)


class PluginManifestError(ValueError):
    """The plugin manifest is present but invalid; refuse the launch loudly."""


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    def as_config(self) -> dict[str, object]:
        config: dict[str, object] = {
            "type": "stdio",
            "command": self.command,
            "args": list(self.args),
        }
        if self.env:
            config["env"] = dict(self.env)
        return config


@dataclass(frozen=True, slots=True)
class PluginSpec:
    name: str
    kind: str
    harnesses: tuple[str, ...]  # empty tuple = every harness that can carry it
    server: McpServerSpec | None = None
    path: Path | None = None

    def targets(self, harness: str) -> bool:
        return not self.harnesses or harness in self.harnesses


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugins: tuple[PluginSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class SkippedPlugin:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ClaudePlan:
    """Claude injection: extra MCP servers plus session-scoped plugin dirs."""

    mcp_servers: dict[str, dict[str, object]]
    plugin_dirs: tuple[Path, ...]
    skill_dirs: tuple[Path, ...]
    skipped: tuple[SkippedPlugin, ...]


@dataclass(frozen=True, slots=True)
class CodexPlan:
    """Codex injection: ``-c`` config overrides on the app-server invocation."""

    config_args: tuple[str, ...]
    skipped: tuple[SkippedPlugin, ...]


@dataclass(frozen=True, slots=True)
class PiPlan:
    """pi injection: repeatable ``--skill`` and ``--extension`` flags."""

    skill_dirs: tuple[Path, ...]
    extensions: tuple[Path, ...]
    skipped: tuple[SkippedPlugin, ...]


def builtin_skill_dir() -> Path:
    """The packaged hyprial usage skill, shipped with every install."""

    return Path(__file__).resolve().parent / "skills" / "hyprial-ops"


def manifest_path(home: Path) -> Path:
    return home / _MANIFEST_RELPATH


def load_manifest(home: Path) -> PluginManifest:
    """Read ``$HYPRIAL_HOME/plugins/plugins.json``; absent means empty."""

    path = manifest_path(home)
    if not path.exists():
        return PluginManifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PluginManifestError(f"unreadable plugin manifest {path}: {error}") from error
    if not isinstance(raw, dict):
        raise PluginManifestError(f"{path}: manifest must be a JSON object")
    version = raw.get("version")
    if version != 1:
        raise PluginManifestError(f"{path}: unsupported manifest version {version!r}")
    entries = raw.get("plugins", [])
    if not isinstance(entries, list):
        raise PluginManifestError(f"{path}: 'plugins' must be an array")
    base = path.parent
    plugins: list[PluginSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        plugins.append(_parse_entry(entry, index=index, base=base, seen=seen))
    return PluginManifest(tuple(plugins))


def _parse_entry(
    entry: object, *, index: int, base: Path, seen: set[str]
) -> PluginSpec:
    where = f"plugins[{index}]"
    if not isinstance(entry, dict):
        raise PluginManifestError(f"{where}: must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise PluginManifestError(
            f"{where}: 'name' must match [A-Za-z0-9_-]+, got {name!r}"
        )
    if name in seen:
        raise PluginManifestError(f"{where}: duplicate plugin name {name!r}")
    seen.add(name)
    if name == RESERVED_MCP_SERVER:
        raise PluginManifestError(
            f"{where}: {RESERVED_MCP_SERVER!r} is reserved for the hyprial channel"
        )
    kind = entry.get("kind")
    if kind not in _KINDS:
        raise PluginManifestError(
            f"{where}: 'kind' must be one of {sorted(_KINDS)}, got {kind!r}"
        )
    harnesses_raw = entry.get("harnesses", [])
    if not isinstance(harnesses_raw, list) or any(
        item not in _HARNESSES for item in harnesses_raw
    ):
        raise PluginManifestError(
            f"{where}: 'harnesses' must be a subset of {sorted(_HARNESSES)}"
        )
    harnesses = tuple(dict.fromkeys(harnesses_raw))
    server: McpServerSpec | None = None
    payload: Path | None = None
    if kind == "mcp":
        server = _parse_server(entry.get("server"), where=where)
    else:
        payload = _parse_payload(entry.get("path"), where=where, base=base, kind=kind)
    return PluginSpec(name=name, kind=kind, harnesses=harnesses, server=server, path=payload)


def _parse_server(raw: object, *, where: str) -> McpServerSpec:
    if not isinstance(raw, dict):
        raise PluginManifestError(f"{where}: mcp plugin requires a 'server' object")
    command = raw.get("command")
    if not isinstance(command, str) or not command:
        raise PluginManifestError(f"{where}: server.command must be a non-empty string")
    args = raw.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise PluginManifestError(f"{where}: server.args must be an array of strings")
    env = raw.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise PluginManifestError(f"{where}: server.env must map strings to strings")
    return McpServerSpec(command=command, args=tuple(args), env=dict(env))


def _parse_payload(raw: object, *, where: str, base: Path, kind: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise PluginManifestError(f"{where}: {kind} plugin requires a 'path' string")
    candidate = Path(raw)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    )
    if not candidate.is_absolute():
        # A relative payload must stay inside the plugin store: a manifest is
        # user-owned configuration, not a license to address arbitrary files.
        base_resolved = base.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise PluginManifestError(f"{where}: path escapes the plugin store: {raw!r}")
    if not resolved.exists():
        raise PluginManifestError(f"{where}: payload does not exist: {resolved}")
    return resolved


# Known residual: capability filtering and the concrete branches below are
# separate layers. If a capability row gains a new kind without a matching
# dispatch branch, that kind can pass filtering and then vanish without a
# skip. Any future kind addition must therefore add a dispatch branch and an
# exhaustiveness test in the same change; MCPREV2 records but does not redesign
# that boundary.
def claude_plan(manifest: PluginManifest) -> ClaudePlan:
    servers: dict[str, dict[str, object]] = {}
    plugin_dirs: list[Path] = []
    skill_dirs: list[Path] = [builtin_skill_dir()]
    plugins, skipped = _plugins_for_harness(manifest, "claude")
    for plugin in plugins:
        if plugin.kind == "mcp":
            assert plugin.server is not None
            servers[plugin.name] = plugin.server.as_config()
        elif plugin.kind == "claude-plugin":
            assert plugin.path is not None
            plugin_dirs.append(plugin.path)
        elif plugin.kind == "skill":
            assert plugin.path is not None
            skill_dirs.append(plugin.path)
    return ClaudePlan(
        mcp_servers=servers,
        plugin_dirs=tuple(plugin_dirs),
        skill_dirs=tuple(skill_dirs),
        skipped=skipped,
    )


def codex_plan(manifest: PluginManifest) -> CodexPlan:
    config_args: list[str] = []
    plugins, skipped = _plugins_for_harness(manifest, "codex")
    for plugin in plugins:
        if plugin.kind == "mcp":
            assert plugin.server is not None
            config_args.extend(_codex_overrides(plugin.name, plugin.server))
    return CodexPlan(config_args=tuple(config_args), skipped=skipped)


def pi_plan(manifest: PluginManifest) -> PiPlan:
    skill_dirs: list[Path] = [builtin_skill_dir()]
    extensions: list[Path] = []
    plugins, skipped = _plugins_for_harness(manifest, "pi")
    for plugin in plugins:
        if plugin.kind == "skill":
            assert plugin.path is not None
            skill_dirs.append(plugin.path)
        elif plugin.kind == "pi-extension":
            assert plugin.path is not None
            extensions.append(plugin.path)
    return PiPlan(
        skill_dirs=tuple(skill_dirs),
        extensions=tuple(extensions),
        skipped=skipped,
    )


def _plugins_for_harness(
    manifest: PluginManifest, harness: str
) -> tuple[tuple[PluginSpec, ...], tuple[SkippedPlugin, ...]]:
    mechanism, supported_kinds, capability_note = _declared_plugin_kinds(harness)
    accepted: list[PluginSpec] = []
    skipped: list[SkippedPlugin] = []
    for plugin in manifest.plugins:
        if not plugin.targets(harness):
            continue
        if plugin.kind in supported_kinds:
            accepted.append(plugin)
            continue
        mechanism_label = mechanism or "no session-scoped mechanism"
        reason = (
            f"{harness} {mechanism_label} plugin injection cannot load "
            f"kind {plugin.kind!r}"
        )
        if capability_note:
            reason += f" ({capability_note})"
        skipped.append(SkippedPlugin(plugin.name, reason))
    return tuple(accepted), tuple(skipped)


def _codex_overrides(name: str, server: McpServerSpec) -> list[str]:
    """``-c`` dotted-path overrides; values are TOML, JSON-escaped strings fit."""

    prefix = f"mcp_servers.{name}"
    overrides = [
        "-c",
        f"{prefix}.command={json.dumps(server.command)}",
        "-c",
        f"{prefix}.args={json.dumps(list(server.args))}",
    ]
    if server.env:
        pairs = ",".join(
            f"{json.dumps(key)}={json.dumps(value)}"
            for key, value in sorted(server.env.items())
        )
        overrides.extend(["-c", f"{prefix}.env={{{pairs}}}"])
    return overrides


def materialize_claude_skill_plugin(
    skill_dirs: tuple[Path, ...], target_dir: Path
) -> Path:
    """Wrap skill directories as one session-scoped Claude plugin.

    Claude Code has no ``--skill`` flag; skills ride inside a plugin
    (``.claude-plugin/plugin.json`` + ``skills/<name>/``).  The wrapper is
    generated per launch under the launch's state directory, like the
    channel's MCP config file, so a crashed launch never leaks a stale
    payload into the next one.
    """

    plugin_root = target_dir
    meta_dir = plugin_root / ".claude-plugin"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hyprial",
                "description": "hyprial Harness Bridge session skills",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    skills_root = plugin_root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for source in skill_dirs:
        destination = skills_root / source.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
    return plugin_root


__all__ = [
    "RESERVED_MCP_SERVER",
    "ClaudePlan",
    "CodexPlan",
    "McpServerSpec",
    "PiPlan",
    "PluginManifest",
    "PluginManifestError",
    "PluginSpec",
    "SkippedPlugin",
    "builtin_skill_dir",
    "claude_plan",
    "codex_plan",
    "load_manifest",
    "manifest_path",
    "materialize_claude_skill_plugin",
    "pi_plan",
]
