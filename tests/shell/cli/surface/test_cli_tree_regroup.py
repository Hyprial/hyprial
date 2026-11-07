"""The regrouped CLI tree and the fail-loud removal of old top-level paths.

Targets (0.5.5.2): ``service``→``org service``, ``fs``→``org fs``,
``user-proxy``→``user proxy``, ``link``→``agent link``,
``onboarding``→``init onboarding``.  Old paths keep no alias: they exit 2 and
print one greppable ``moved: hyprial <old>  →  hyprial <new>`` line.  A bare
``hyprial init`` must still initialize — ``init`` becomes a group, not help-only.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli

#: Old top-level name → its path under the owning group.
MOVED_FAMILIES: dict[str, tuple[str, ...]] = {
    "service": ("org", "service"),
    "fs": ("org", "fs"),
    "user-proxy": ("user", "proxy"),
    "link": ("agent", "link"),
    "onboarding": ("init", "onboarding"),
}

#: Old argv (without the ``hyprial`` program name) → new argv.
MOVED_ARGV: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("service",), ("org", "service")),
    (("fs",), ("org", "fs")),
    (("user-proxy",), ("user", "proxy")),
    (("link",), ("agent", "link")),
    (("onboarding",), ("init", "onboarding")),
    (("fs", "space", "list"), ("org", "fs", "space", "list")),
    (("link", "list", "--json"), ("agent", "link", "list", "--json")),
    (("service", "connect", "--json"), ("org", "service", "connect", "--json")),
]


def group_paths(group: Any, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Map every group path to its Typer object."""

    found: dict[tuple[str, ...], Any] = {}
    for info in group.registered_groups:
        sub = info.typer_instance
        group_path = path + (info.name or "",)
        found[group_path] = sub
        found.update(group_paths(sub, group_path))
    return found


def _render(old: tuple[str, ...], new: tuple[str, ...]) -> str:
    return f"moved: hyprial {' '.join(old)}  →  hyprial {' '.join(new)}"


def test_moved_families_left_the_top_level() -> None:
    top_level = {info.name for info in cli.app.registered_groups}
    remaining = sorted(top_level & set(MOVED_FAMILIES))
    assert remaining == [], f"still registered at the top level: {remaining}"


@pytest.mark.parametrize("old,new_path", sorted(MOVED_FAMILIES.items()))
def test_family_is_registered_under_its_new_parent(
    old: str, new_path: tuple[str, ...]
) -> None:
    groups = group_paths(cli.app)
    assert new_path in groups, f"hyprial {' '.join(new_path)} is not a group"
    siblings = {info.name for info in cli.app.registered_groups}
    assert new_path[0] in siblings, f"hyprial {new_path[0]} is missing"


@pytest.mark.parametrize("old,new", MOVED_ARGV)
def test_old_path_fails_loud(
    old: tuple[str, ...], new: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(cli.app, list(old))
    assert result.exit_code == 2, (result.stdout or "") + (result.stderr or "")
    # Usage errors never produce a JSON (or any) payload on stdout, even with
    # ``--json``: the command no longer exists, it did not reject the call.
    assert (result.stdout or "") == ""
    assert "moved:" in (result.stderr or "")
    assert " ".join(new) in (result.stderr or "")


@pytest.mark.parametrize(
    "old,new",
    [
        (("service",), ("org", "service")),
        (("fs",), ("org", "fs")),
        (("user-proxy",), ("user", "proxy")),
        (("link",), ("agent", "link")),
        (("onboarding",), ("init", "onboarding")),
        (("fs", "space", "list"), ("org", "fs", "space", "list")),
    ],
)
def test_old_path_line_is_exact(
    old: tuple[str, ...], new: tuple[str, ...]
) -> None:
    """Golden line: dropping ``moved: `` or widening the arrow spacing breaks it."""

    result = CliRunner().invoke(cli.app, list(old))
    printed = (result.stdout or "") + (result.stderr or "")
    assert _render(old, new) in printed, printed


def test_service_move_points_at_org_service_only() -> None:
    """`hyprial service` moves under org; `hyprial daemon service` stays separate."""

    result = CliRunner().invoke(cli.app, ["service"])
    printed = (result.stdout or "") + (result.stderr or "")
    assert "moved: hyprial service  \u2192  hyprial org service" in printed, printed
    assert "daemon service" not in printed, printed


def test_moved_line_format_is_frozen() -> None:
    """Negative controls: prefix removal and arrow-spacing drift must both red."""

    from hyprial.shell.impl.cli.runtime import legacy_paths

    assert legacy_paths.MOVED_PREFIX == "moved: "
    assert legacy_paths.MOVED_ARROW == "  \u2192  "
    assert (
        legacy_paths.render_moved(("service",), ("org", "service"))
        == "moved: hyprial service  \u2192  hyprial org service"
    )
    assert (
        legacy_paths.render_moved(
            ("fs", "space", "list"), ("org", "fs", "space", "list")
        )
        == "moved: hyprial fs space list  \u2192  hyprial org fs space list"
    )


def test_host_stays_at_the_top_level() -> None:
    """The host→org host example was a format sample, not this card's scope."""

    top_level = {info.name for info in cli.app.registered_groups}
    assert "host" in top_level


class _FakeRuntime:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    def execute(self, operation: Any, **_kwargs: Any) -> None:
        self.calls.append(operation())


class _FakeInitServices:
    """Just enough of the CLI service face for a bare ``hyprial init``."""

    # dev #2151 made autostart the default, so bare ``init`` now reaches the
    # supervisor install and catches its refusal on this services face.
    from hyprial.shell.impl.cli.commands.common.support import CliError

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.runtime = _FakeRuntime(self.calls)
        self.home_initialized = False
        self.completed = False

    def initialize_hyprial_home(self) -> None:
        self.home_initialized = True

    def _initialize_org_context(self) -> None:
        return None

    def _complete_initialization(self, **_kwargs: Any) -> dict[str, Any]:
        self.completed = True
        return {"ok": True}

    def _stdin_isatty(self) -> bool:
        return False


def test_bare_init_still_initializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``init`` becomes a group, so guard that no arguments still initialize."""

    from hyprial.shell.impl.cli.commands.init import init as init_module

    fake = _FakeInitServices()
    monkeypatch.setattr(init_module, "get_services", lambda: fake)

    result = CliRunner().invoke(cli.app, ["init", "--json"])

    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert fake.home_initialized is True
    assert fake.completed is True
    assert fake.calls and fake.calls[0].get("ok") is True
