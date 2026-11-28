"""The two hooks of ``collection.PosixOnlyPlugin``, called directly.

Decision (2026-11-26): the plugin is exercised by calling its two hooks with
``tests.support.windows_suite.host.is_windows`` replaced, never by starting a
pytest run and never by changing ``os.name``: ``pathlib`` dispatches on
``os.name`` and would refuse to build a path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.windows_suite import collection, host


from tests.tooling.ci.windows.registries.registry_gate import (
    APPROVED_ENTRIES,
    ROOT,
)

class Item:
    """Smallest stand-in for a collected test: its own markers, as pytest's ``Node`` keeps them."""

    def __init__(self, *marks: pytest.MarkDecorator) -> None:
        self.own_markers: list[pytest.Mark] = [mark.mark for mark in marks]

    def iter_markers(self, name: str) -> list[pytest.Mark]:
        return [mark for mark in self.own_markers if mark.name == name]

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.own_markers.append(marker.mark)


class Manager:
    """``pluginmanager`` with the two calls ``configure_posix_only`` makes."""

    def __init__(self) -> None:
        self.plugins: dict[str, object] = {}

    def get_plugin(self, name: str) -> object:
        return self.plugins.get(name)

    def register(self, plugin: object, name: str) -> None:
        self.plugins[name] = plugin


def skip_reasons(items: list[Item]) -> list[object]:
    return [next(iter(item.iter_markers("skip")), None) for item in items]


def test_off_windows_the_plugin_ignores_nothing_and_skips_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: False)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests/a",), "file": ("tests/b/test_c.py",)})
    ignored = [
        plugin.pytest_ignore_collect(path, None)  # type: ignore[arg-type]
        for path in (ROOT / "tests/a", ROOT / "tests/a/deep/test_x.py", ROOT / "tests/b/test_c.py", ROOT / "tests/d")
    ]
    assert ignored == [None, None, None, None]
    items = [Item(pytest.mark.posix_only("pty")), Item()]
    plugin.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    assert [item.own_markers for item in items] == [[pytest.mark.posix_only("pty").mark], []]
    assert skip_reasons(items) == [None, None]


def test_on_windows_the_plugin_ignores_a_registered_directory_and_everything_below_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests/a",), "file": ("tests/b/test_c.py",)})
    answers = {
        ROOT / "tests/a": True,
        ROOT / "tests/a/deep/test_x.py": True,
        ROOT / "tests/a/deep": True,
        ROOT / "tests/ab/test_x.py": None,
        ROOT / "tests": None,
        ROOT / "other/a/test_x.py": None,
    }
    assert {path: plugin.pytest_ignore_collect(path, None) for path in answers} == answers  # type: ignore[arg-type]


def test_on_windows_the_plugin_ignores_a_registered_file_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin({"dir": (), "file": ("tests/b/test_c.py",)})
    answers = {
        ROOT / "tests/b/test_c.py": True,
        ROOT / "tests/b/test_cd.py": None,
        ROOT / "tests/b/test_c.py/inner.py": None,
        ROOT / "tests/b": None,
    }
    assert {path: plugin.pytest_ignore_collect(path, None) for path in answers} == answers  # type: ignore[arg-type]


def test_on_windows_a_relative_collection_path_and_a_trailing_slash_are_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    monkeypatch.chdir(ROOT)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests/a/",), "file": ("tests/b/test_c.py",)})
    assert plugin.pytest_ignore_collect(Path("tests/a/deep/test_x.py"), None) is True  # type: ignore[arg-type]
    assert plugin.pytest_ignore_collect(Path("tests/b/test_c.py"), None) is True  # type: ignore[arg-type]
    assert plugin.pytest_ignore_collect(Path("tests/b/test_d.py"), None) is None  # type: ignore[arg-type]


def test_on_windows_a_path_outside_the_repository_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin({"dir": ("tests",), "file": ()})
    assert plugin.pytest_ignore_collect(tmp_path / "tests" / "test_x.py", None) is None  # type: ignore[arg-type]
    assert plugin.pytest_ignore_collect(ROOT / "tests" / "test_x.py", None) is True  # type: ignore[arg-type]


def test_the_plugin_reads_the_real_registry_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin()
    assert plugin.directories == ("tests/tooling/release/shell/signing",)
    assert plugin.files == tuple(path for path, kind, _ in APPROVED_ENTRIES if kind == "file")
    answers = {ROOT / path: plugin.pytest_ignore_collect(ROOT / path, None) for path, _, _ in APPROVED_ENTRIES}  # type: ignore[arg-type]
    assert set(answers.values()) == {True}
    assert plugin.pytest_ignore_collect(ROOT / "tests/tooling/release/shell/daemon/test_verify_daemon.py", None) is None  # type: ignore[arg-type]


def test_on_windows_the_plugin_skips_every_marked_item_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin()
    items = [
        Item(pytest.mark.posix_only("pty")),
        Item(pytest.mark.posix_only("SIGUSR1"), pytest.mark.posix_only("SIGHUP")),
        Item(pytest.mark.posix_only("fcntl"), pytest.mark.skip(reason="already skipped")),
        Item(pytest.mark.skip(reason="plain skip")),
        Item(),
    ]
    plugin.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    added = [item.own_markers[len(item.own_markers) - 1] for item in items[:3]]
    assert [(mark.name, mark.args, dict(mark.kwargs)) for mark in added] == [
        ("skip", (), {"reason": "posix_only: pty"}),
        ("skip", (), {"reason": "posix_only: SIGUSR1, SIGHUP"}),
        ("skip", (), {"reason": "posix_only: fcntl"}),
    ]
    assert [len(item.own_markers) for item in items] == [2, 3, 3, 1, 0]
    assert dict(items[2].own_markers[1].kwargs) == {"reason": "already skipped"}
    assert skip_reasons(items) == [pytest.mark.skip(reason="posix_only: pty").mark, pytest.mark.skip(reason="posix_only: SIGUSR1, SIGHUP").mark, pytest.mark.skip(reason="already skipped").mark, pytest.mark.skip(reason="plain skip").mark, None]


def test_a_posix_only_marker_without_a_reason_skips_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "is_windows", lambda: True)
    plugin = collection.PosixOnlyPlugin()
    items = [Item(pytest.mark.posix_only), Item(pytest.mark.posix_only(reason="pty"))]
    plugin.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    assert [len(item.own_markers) for item in items] == [1, 1]


def test_the_plugin_is_registered_once_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    for windows in (False, True):
        monkeypatch.setattr(host, "is_windows", lambda: windows)
        manager = Manager()
        for _ in range(2):
            collection.configure_posix_only(SimpleNamespace(pluginmanager=manager))  # type: ignore[arg-type]
        assert list(manager.plugins) == [collection.PLUGIN_NAME]
        assert manager.plugins[collection.PLUGIN_NAME].__class__ is collection.PosixOnlyPlugin
    assert collection.PLUGIN_NAME == "hyprial-windows-posix-only"
