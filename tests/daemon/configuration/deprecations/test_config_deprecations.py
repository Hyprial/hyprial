"""A field we removed must speak; a field we never knew must not.

Collapsing those two is what makes silence look like tolerance. Unknown fields
come from newer versions and must be ignored -- with three release tracks
running side by side, old code that refused a newer config would make every
downgrade fatal. But a field *we* deleted is different: somebody may still be
editing it, and getting nothing back is the same "changed it, nothing
happened" failure this codebase keeps producing in other shapes.

The registry is also migration's input (Allen 2026-08-28), so these tests pin
the structure migration will rely on, not only the message a human reads.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.correlation.deprecations import (
    DEPRECATED_FIELDS,
    DeprecatedField,
    deprecation_notices,
)


def test_a_removed_field_that_is_still_set_is_reported() -> None:
    notices = deprecation_notices({"zenoh": {"listen": ["tcp/1.2.3.4:7447"]}})

    assert [field.path for field, _ in notices] == [("zenoh", "listen")]


def test_a_field_we_never_knew_is_ignored() -> None:
    """Tolerance is for the future, not for our own removals.

    An unknown field comes from a newer version. Refusing it would mean a
    machine moving from dev back to stable cannot start -- and the three
    tracks make that a routine event, not an edge case.
    """

    assert deprecation_notices({"somethingFromTheFuture": {"x": 1}}) == ()


def test_an_absent_field_is_not_reported() -> None:
    assert deprecation_notices({"zenoh": {}}) == ()
    assert deprecation_notices({}) == ()


def test_an_emptied_field_is_not_reported() -> None:
    """Otherwise the notice could not be cleared by following it.

    A field left as [] after a cleanup is nobody trying to configure
    anything, and a warning that persists after the reader did the right
    thing is the kind people learn to ignore.
    """

    assert deprecation_notices({"zenoh": {"listen": [], "connect": []}}) == ()


def test_every_notice_says_what_to_do_instead() -> None:
    """Not merely that the field is dead.

    A notice that only reports the loss leaves the reader exactly where they
    started: still wanting the behaviour, now with no route to it. This is the
    property that makes the warning actionable rather than noise.
    """

    for field in DEPRECATED_FIELDS:
        assert field.guidance.strip(), field.path
        assert any(
            marker in field.guidance
            for marker in ("set ", "use ", "To pin", "instead")
        ), f"{field.path} does not tell the reader what to do"


def test_every_entry_states_what_migration_should_do() -> None:
    """The registry feeds migration as well as the log.

    An entry recording that a field is gone without saying what becomes of it
    would let the two consumers of this table disagree -- the warning claiming
    one thing while a future migration does another.
    """

    for field in DEPRECATED_FIELDS:
        assert field.action in {"drop", "rename"}, field.path
        if field.action == "rename":
            assert field.rename_to, field.path


def test_a_rename_without_a_destination_is_rejected() -> None:
    with pytest.raises(ValueError):
        DeprecatedField(
            path=("a", "b"),
            since_schema_version=1,
            guidance="use c.d instead",
            action="rename",
        )


def test_a_drop_with_a_destination_is_rejected() -> None:
    """Contradictory entries fail at import, not at migration time."""

    with pytest.raises(ValueError):
        DeprecatedField(
            path=("a", "b"),
            since_schema_version=1,
            guidance="set X instead",
            action="drop",
            rename_to=("c", "d"),
        )
