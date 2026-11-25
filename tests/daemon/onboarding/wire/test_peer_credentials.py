"""The seam's peer-uid reader: a real connection, and its two non-answers.

Decision: daemon-first-z1-peer-tests-20261115.  A connected socketpair reports
this process's effective uid where the platform exposes a credential; a
platform with none answers ``None`` (the caller relies on file modes), and a
credential with an unexpected layout is an ``OSError``, never a guess.

Decision: ipc-bootstrap 2026-11-19.  These cases used to pin the bootstrap
``peer.py``'s own reader; that reader moved unchanged into the kernel local
IPC seam (``local_ipc/posix.py``), whose accepted :class:`LocalIpcPeer` the
bootstrap wire host now enforces, so the same cases pin the seam copy here.
``own_uid`` stays in ``peer.py`` and supplies the expected side.
"""

from __future__ import annotations

import os
import socket
import struct

import pytest

from hyprial.daemon.impl.application.onboarding.bootstrap import peer
from hyprial.kernel.impl.manysys.ipc import posix as local_ipc_posix


def test_a_connected_peer_reports_its_effective_uid():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        uid = local_ipc_posix.read_peer_uid(left)
    assert uid == os.geteuid() == peer.own_uid()


def test_own_uid_is_none_where_the_platform_has_no_uids(monkeypatch):
    monkeypatch.delattr(os, "geteuid")
    assert peer.own_uid() is None


def test_no_credential_facility_answers_none(monkeypatch):
    class NoCredential:
        def getsockopt(self, *_arguments):
            raise OSError("no such option")

    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    assert local_ipc_posix.read_peer_uid(NoCredential()) is None


def test_the_bsd_family_probe_decodes_the_uid(monkeypatch):
    class Xucred:
        def getsockopt(self, level, option, size):
            assert (level, option, size) == (0, 0x001, 76)
            return struct.pack("II", 0, 4242) + bytes(68)

    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    assert local_ipc_posix.read_peer_uid(Xucred()) == 4242


def test_a_malformed_credential_is_an_oserror(monkeypatch):
    class Truncated:
        def getsockopt(self, *_arguments):
            return b"\x00"

    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    with pytest.raises(OSError):
        local_ipc_posix.read_peer_uid(Truncated())
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    with pytest.raises(OSError):
        local_ipc_posix.read_peer_uid(Truncated())


def test_an_unreadable_linux_credential_is_an_oserror_not_none(monkeypatch):
    class Refusing:
        def getsockopt(self, *_arguments):
            raise OSError("denied")

    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    with pytest.raises(OSError):
        local_ipc_posix.read_peer_uid(Refusing())


def test_a_linux_uid_above_the_signed_range_is_decoded_unsigned(monkeypatch):
    class Wide:
        def getsockopt(self, *_arguments):
            return struct.pack("3I", 7, 4_000_000_000, 5)

    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    assert local_ipc_posix.read_peer_uid(Wide()) == 4_000_000_000


def test_a_bsd_credential_with_another_version_is_an_oserror(monkeypatch):
    class Versioned:
        def getsockopt(self, *_arguments):
            return struct.pack("II", 1, 4242) + bytes(68)

    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    with pytest.raises(OSError):
        local_ipc_posix.read_peer_uid(Versioned())
