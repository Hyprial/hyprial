"""Private bootstrap lifecycle record, separate from normal daemon readiness.

Callers hold the home identity transaction and daemon state ownership locks
through read/update/removal. This file is diagnostic metadata, not a control
capability or proof of authorization. It contains no credential material.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from hyprial.kernel import atomic_json_write, process_birth_identity

_FIELDS = frozenset({"pid", "birthIdentity", "epoch", "socket", "phase"})
_PHASES = frozenset({"awaiting-auth", "authenticating", "authenticated", "promoting"})


@dataclass(frozen=True, slots=True)
class BootstrapRecord:
    pid: int
    birth_identity: str
    epoch: str
    socket: str
    phase: str = "awaiting-auth"

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("bootstrap record PID must be a positive integer")
        for value in (self.birth_identity, self.epoch, self.socket):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("bootstrap record identity fields must be nonempty")
        if not isinstance(self.phase, str) or self.phase not in _PHASES:
            raise ValueError("bootstrap record has an invalid phase")

    @classmethod
    def current(cls, socket: str) -> BootstrapRecord:
        pid = os.getpid()
        birth = process_birth_identity(pid)
        if not birth:
            raise RuntimeError("current process birth identity is unavailable")
        return cls(pid, birth, uuid4().hex, socket)

    @classmethod
    def read(cls, state_dir: Path) -> BootstrapRecord | None:
        path = state_dir / "bootstrap.json"
        try:
            observed = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(observed.st_mode):
            raise ValueError("bootstrap record must not be a symlink")
        if not stat.S_ISREG(observed.st_mode):
            raise ValueError("bootstrap record must be a regular file")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)):
                    raise ValueError("bootstrap record changed during read")
                text = stream.read(65537)
            if len(text) > 65536:
                raise ValueError("bootstrap record exceeds size bound")
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("bootstrap record is malformed") from error
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError("bootstrap record fields do not match the local format")
        return cls(value["pid"], value["birthIdentity"], value["epoch"],
                   value["socket"], value["phase"])

    def same_instance(self, other: BootstrapRecord) -> bool:
        return (
            self.pid, self.birth_identity, self.epoch, self.socket
        ) == (other.pid, other.birth_identity, other.epoch, other.socket)

    def publish(self, state_dir: Path) -> None:
        existing = self.read(state_dir)
        if existing is not None and not self.same_instance(existing):
            raise RuntimeError("record belongs to another bootstrap instance")
        atomic_json_write(state_dir / "bootstrap.json", {
            "pid": self.pid,
            "birthIdentity": self.birth_identity,
            "epoch": self.epoch,
            "socket": self.socket,
            "phase": self.phase,
        })

    def remove_if_owned(self, state_dir: Path) -> bool:
        existing = self.read(state_dir)
        if existing is None or not self.same_instance(existing):
            return False
        (state_dir / "bootstrap.json").unlink()
        return True
