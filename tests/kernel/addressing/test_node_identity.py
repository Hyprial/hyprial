"""One node-id reader for every product surface."""

from __future__ import annotations

import logging

import pytest

from hyprial.kernel import resolve_node_id


@pytest.mark.parametrize(
    "site",
    [
        "daemon-application",
        "adapter-registration",
        "pac-context",
        "squire-derived-machine",
        "squire-cross-check",
        "login-device",
        "interactive-agent",
        "daemon-process",
        "desktop-device",
        "transfer-bundle",
    ],
)
@pytest.mark.parametrize(
    ("environ", "expected"),
    [({"HYPRIAL_NODE_ID": " node-a "}, "node-a"), ({}, "host-a")],
)
def test_helper_matches_each_replaced_expression_for_normal_inputs(
    site: str, environ: dict[str, str], expected: str
) -> None:
    del site
    assert resolve_node_id(environ, hostname=lambda: "host-a") == expected


def test_explicit_empty_warns_once_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        resolved = resolve_node_id(
            {"HYPRIAL_NODE_ID": " \t "}, hostname=lambda: " host-a "
        )

    assert resolved == "host-a"
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "HYPRIAL_NODE_ID is empty after trimming; ignoring it and falling "
        "back to the host name"
    ]


def test_empty_environment_and_host_name_fail_loudly() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_node_id({}, hostname=lambda: " \t ")
