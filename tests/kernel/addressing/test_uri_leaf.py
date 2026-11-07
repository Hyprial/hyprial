"""Fresh-process coverage for schema imports through the URI leaf module."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hyprial.kernel.impl.primitives.uri import (
    is_agent_id_segment,
    is_device_id_segment,
    is_user_id_segment,
)


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("u.ou_" + "b" * 32, True),
        ("u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9", True),
        ("u.ou_" + "b" * 16, True),
        ("u.ou_" + "b" * 64, True),
        ("u.ou_" + "b" * 15, False),
        ("u.ou_" + "b" * 65, False),
        ("u.00000000-0000-1000-8000-000000000000", True),
        ("u.ou_" + "z" * 32, False),
        ("u.ou_" + "B" * 32, False),
        ("U.ou_" + "b" * 32, False),
        ("u.6F8B1D21-33A8-4688-A32E-B1B23E87D5D9", False),
    ],
)
def test_user_id_segment_exact_boundaries(segment: str, expected: bool) -> None:
    assert is_user_id_segment(segment) is expected


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("d." + "b" * 32, True),
        ("d." + "b" * 31, False),
        ("d." + "b" * 33, False),
        ("d." + "g" * 32, False),
        ("d." + "B" * 32, False),
        ("D." + "b" * 32, False),
    ],
)
def test_device_id_segment_exact_boundaries(segment: str, expected: bool) -> None:
    assert is_device_id_segment(segment) is expected


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("a." + "a" * 26, True),
        ("a." + "a" * 25, False),
        ("a." + "a" * 27, False),
        ("a." + "0" * 26, False),
        ("a." + "A" * 26, False),
        ("A." + "a" * 26, False),
    ],
)
def test_agent_id_segment_exact_boundaries(segment: str, expected: bool) -> None:
    assert is_agent_id_segment(segment) is expected


def test_routine_and_workflow_schemas_are_safe_as_first_hyprial_import() -> None:
    """Each schema must import cleanly before any other hyprial module is loaded."""

    for module in ('hyprial.biz.impl.routine.contracts.schema', 'hyprial.daemon.impl.pac.contracts.workflow'):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import {module}, sys; assert 'hyprial.daemon.impl.application' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        assert probe.returncode == 0, f"{module}: {probe.stderr}"


def test_daemon_identity_does_not_reexport_the_uri_surface() -> None:
    identity = importlib.import_module("hyprial.daemon.impl.configuration.identity")
    moved_names = {
        "ADAPTER_URI_PREFIX",
        "AGENT_URI_PREFIX",
        "CHANNEL_URI_PREFIX",
        "TARGET_KINDS",
        "TARGET_KIND_AGENT",
        "TARGET_KIND_CHANNEL_ROUTE",
        "TARGET_KIND_HOST",
        "TARGET_KIND_UNKNOWN",
        "TARGET_KIND_USER",
        "agent_uri_actor",
        "canonical_agent_uri",
        "parse_agent_uri",
        "parse_channel_uri",
        "short_actor_name",
    }
    assert moved_names.isdisjoint(vars(identity))


def test_schema_first_import_guard_rejects_the_real_application_owner() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hyprial.daemon.impl.application, sys; "
            "assert 'hyprial.daemon.impl.application' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert probe.returncode != 0
    assert "AssertionError" in probe.stderr
    assert "ModuleNotFoundError" not in probe.stderr
