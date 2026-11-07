"""Opt-in test ownership for successfully constructed daemon applications.

A test module imports ``owned_applications`` to enable this autouse fixture.
Constructor failures remain the constructor's own rollback responsibility.
"""
from __future__ import annotations

import functools
import weakref
from collections.abc import Iterator

import pytest

from hyprial.daemon.impl.application import DaemonApplication


_closed: weakref.WeakSet[DaemonApplication] = weakref.WeakSet()


def close_application(app: DaemonApplication) -> None:
    """Run the same application and home-lease close steps as daemon shutdown."""

    if app in _closed:
        return
    errors: list[BaseException] = []
    try:
        app._close()
    except BaseException as error:
        errors.append(error)
    try:
        app._close_home_lease()
    except BaseException as error:
        errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("application test cleanup failed", errors)
    _closed.add(app)


@pytest.fixture(autouse=True)
def owned_applications(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close only applications whose real constructor returned successfully."""

    original_init = DaemonApplication.__init__
    constructed: list[DaemonApplication] = []

    @functools.wraps(original_init)
    def tracked_init(self: DaemonApplication, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        constructed.append(self)

    monkeypatch.setattr(DaemonApplication, "__init__", tracked_init)
    yield
    errors: list[BaseException] = []
    for app in reversed(constructed):
        try:
            close_application(app)
        except BaseException as error:
            errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("owned applications did not close", errors)
