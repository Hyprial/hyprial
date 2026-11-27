"""Shared arrangement for the S5 fix tests: one org directory as bob's device sees it.

Decision (service grant S5 fix, 2026-11-27): the fix tests reuse the S5
consumer support (real OrgFS store, real sealed envelopes, a real key store)
and add only the few pieces the critic's findings need: a service manager
over the recipient home and helpers for writing a grantor document by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.daemon.service_connect.support import FakeController, FakeStatePort
from tests.daemon.service_grants.consumer.support import (
    ORG,
    PORT,
    Clock,
    Recorder,
    build_directory,
    service_manager,
)


def world_at(tmp_path: Path, actor_host: Any) -> dict[str, Any]:
    """The S5 directory plus a manager, a controller and a state port for the recipient."""

    directory = build_directory(tmp_path)
    return {
        **directory,
        "tmp": tmp_path,
        "clock": Clock(),
        "events": Recorder(),
        "state": FakeStatePort(),
        "controller": FakeController(),
        "access": tmp_path / "recipient-state" / "tailcat" / "service-access.json",
        "actor_host": actor_host,
        "managers": [],
    }


def close(world: dict[str, Any]) -> None:
    for manager in world["managers"]:
        manager.close(2.0)
    world["managers"].clear()
    world["runtime"].close()


def consumer_arguments(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "orgfs": world["runtime"].facade,
        "home": world["bob_home"],
        "state_dir": world["bob_state"],
        "owner": "bob",
        "device_id": "device-b",
        "now_ms": world["clock"],
        "logger": world["events"],
    }


def manager_for(world: dict[str, Any], source: Any) -> Any:
    created = service_manager(world, source)
    world["managers"].append(created)
    return created


def names(service: Any) -> list[str]:
    return [row["name"] for row in service.handle("service.list", {})["services"]]


def reasons(world: dict[str, Any]) -> list[tuple[str, str]]:
    """``(grantId, reason)`` of every rejection, in the order they were reported."""

    return [(row["grantId"], row["reason"]) for row in world["events"].of("service.grant.rejected")]


__all__ = ["ORG", "PORT", "close", "consumer_arguments", "manager_for", "names", "reasons", "world_at"]
