"""Shared fixtures for cell runtime tests over an isolated LocalOrgFs.

``RETIRED_GRAPH_DOCUMENT`` is the exact text ``codec.encode`` produced for a
``graph`` cell while that kind existed (captured at 787037c19, before the kind
and the PAC mirror that wrote it were removed).  It stands for the files an
earlier build may have left under ``cells/graph/`` and in ``cells.sqlite3``.

``RETIRED_ROUTINE_DOCUMENT`` is the same for the ``routine`` kind (captured at
c34c8b0f8): the owner's ``agent-default`` document, which lived in the org
directory space under ``cells/routine/`` and was listed on every scan.

``RETIRED_CONVERSATION_DOCUMENT`` is the same for the ``conversation`` kind
(captured just before the kind was demoted to a storage category): the
document the recorder's ``ensure`` used to write beside the record segments
in the owner-home space under ``cells/conversation/``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.ports import CellIdentity
from hyprial.daemon.impl.cells.runtime import CellCache, CellRuntime
from hyprial.daemon.impl.composition.cells.spaces import CellSpaces
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity import org_space_name

ORG = "group-acme"
RETIRED_GRAPH_ID = "c.zr6keg3asjepe2gyxaxj7cebt4"
RETIRED_GRAPH_PATH = f"cells/graph/{RETIRED_GRAPH_ID}.json"
RETIRED_GRAPH_DOCUMENT = (
    '{"authority":"user:alice","binding":{"type":"keyed","value":"sha256:'
    + "ab" * 32
    + '"},"cellId":"c.zr6keg3asjepe2gyxaxj7cebt4","kind":"graph","members":[],'
    '"name":"nightly","params":{"pac.definition":{"digest":"sha256:'
    + "ab" * 32
    + '","name":"nightly","onFailure":"stop",'
    '"schema":"hyprial-pac-graph-definition/v1","specification":"{}"}},'
    '"permissions":{},"retired":false,"revision":1,"schema":"hyprial-cell/v1",'
    '"scope":{"kind":"owner","org":null},"updatedAtMs":1001}'
)

RETIRED_ROUTINE_ID = "c.b3oddul6ywwufr6qiwpmtc4o6a"
RETIRED_ROUTINE_PATH = f"cells/routine/{RETIRED_ROUTINE_ID}.json"
RETIRED_ROUTINE_DOCUMENT = (
    '{"authority":"user:alice","binding":{"type":"keyed","value":"agent-default"},'
    '"cellId":"c.b3oddul6ywwufr6qiwpmtc4o6a","kind":"routine","members":[],'
    '"name":"","params":{"routines.agentDefault":{"enabled":true,"interval":"30m"}},'
    '"permissions":{},"retired":false,"revision":2,"schema":"hyprial-cell/v1",'
    '"scope":{"kind":"org","org":null},"updatedAtMs":1002}'
)

RETIRED_CONVERSATION_ID = "c.olj5bkoknqoarljer7i6t24jzq"
RETIRED_CONVERSATION_PATH = f"cells/conversation/{RETIRED_CONVERSATION_ID}.json"
RETIRED_CONVERSATION_DOCUMENT = (
    '{"authority":"user:alice","binding":{"type":"keyed","value":"t.x\\u0000conv-1"},'
    '"cellId":"c.olj5bkoknqoarljer7i6t24jzq","kind":"conversation","members":'
    '[{"ref":"agent:alice:mac:a","role":"member"},{"ref":"agent:alice:mac:b",'
    '"role":"member"}],"name":"conv-1","params":{},"permissions":{},'
    '"retired":false,"revision":1,"schema":"hyprial-cell/v1",'
    '"scope":{"kind":"owner","org":null},"updatedAtMs":0}'
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        self.now += 1
        return self.now


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, _domain: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))

    def names(self) -> list[str]:
        return [event for _, event, _ in self.events]


def uid_for(writer: str) -> str:
    """A stable Casdoor-shaped ``sub`` for the OrgFS principal *writer*."""

    return "ou_" + hashlib.sha256(writer.encode("utf-8")).hexdigest()[:32]


def authority_for(writer: str) -> str:
    """The cell authority ``user:u.<sub>`` paired with the OrgFS principal *writer*."""

    return f"user:u.{uid_for(writer)}"


def identity_for(writer: str, issuer: str = "") -> CellIdentity:
    return CellIdentity(authority=authority_for(writer), issuer=issuer, writer=writer)


ALICE = authority_for("user:alice")
MALLORY = authority_for("user:mallory")


def org_fs(author: str = "user:alice") -> tuple[LocalOrgFs, str]:
    fs = LocalOrgFs(author=author)
    space = fs.create_space(org_space_name(ORG))
    return fs, space.space_id


def runtime(
    tmp_path: Path, fs: Any, *, name: str = "cells.sqlite3", spaces: Any = None, **kwargs: Any
) -> tuple[CellRuntime, Log]:
    log = Log()
    clock = kwargs.pop("clock_ms", None) or Clock()
    built = CellRuntime(
        fs=fs,
        spaces=spaces or CellSpaces(fs),
        cache=CellCache(tmp_path / name),
        identity=kwargs.pop("identity", None) or identity_for(fs.author),
        clock_ms=clock,
        logger=log,
        **kwargs,
    )
    return built, log
