"""Seeded random honest write sequences are always admitted (design §3.3, §3.6).

``hypothesis`` is not a dependency of this repository, so the property is a
seeded random loop: each seed generates local writes that pass the local
write check, produces their updates through the C2 commit helper, and
delivers every update to a receiver through the admission.
"""

from __future__ import annotations

import random

import pytest

from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    LocalWrite,
    ProtectionPolicy,
    ResultStateAdmission,
    decode_vector,
    local_write_error,
    vector_advanced,
)
from tests.daemon.orgfs.membership.support import (
    ORG_NAME,
    OWNER,
    SPACE_ID,
    Frame,
    Replica,
    deliver,
)

USERS = {"user:alice": "alice", "user:bob": "bob"}
POLICY = ProtectionPolicy(space_id=SPACE_ID, space_name=ORG_NAME, owner=OWNER)


def _documents(replica: Replica, *, protected: bool) -> list[str]:
    tree = replica.tree_value()
    return sorted(
        path for node_id, path in tree.paths.items()
        if tree.placed[node_id].get("kind") == "doc"
        and (POLICY.rule_for_path(path) is not None) == protected
    )


def _step(rng: random.Random, replica: Replica, counter: int) -> tuple[str, LocalWrite, list[Frame]]:
    author = rng.choice(sorted(USERS))
    choice = rng.choice(["create", "edit", "shrink", "device", "remove", "move", "mkdir", "invite"])
    ordinary = _documents(replica, protected=False)
    if choice == "invite":
        user = f"user:guest{counter}"
        return OWNER, LocalWrite("meta"), [replica.invite(user, rng.choice(["ro", "rw"]), by=OWNER)]
    if choice == "device":
        path = f"directory/devices/{USERS[author]}/d{rng.randrange(3)}.json"
        return author, LocalWrite("content", paths=(path,)), replica.write_text(
            path, f'{{"n":{counter}}}', author=author
        )
    if choice == "mkdir":
        path = f"work/dir{counter}"
        return author, LocalWrite("tree", paths=(path,)), [replica.mkdir(path, author=author)]
    if choice in ("edit", "shrink", "remove", "move") and ordinary:
        path = rng.choice(ordinary)
        if choice == "remove":
            return author, LocalWrite("tree", paths=(path,)), [
                replica.remove(path, author=author, batch=f"b{counter}")
            ]
        if choice == "move":
            dirs = sorted(
                node_path for node_id, node_path in replica.tree_value().paths.items()
                if node_path.startswith("work/") and replica.tree_value().placed[node_id]["kind"] == "dir"
            )
            target = rng.choice(dirs) if dirs else "work"
            name = path.rsplit("/", 1)[-1]
            destination = f"{target}/{name}"
            if replica.resolve(destination) is not None:
                return author, LocalWrite("tree", paths=(path,)), replica.write_text(path, "m", author=author)
            return author, LocalWrite("tree", paths=(path, destination)), [
                replica.move(path, target, author=author)
            ]
        current = replica.staged(
            str(replica.tree_value().rows[replica.resolve(path)]["doc_id"])
        ).materialize().text
        text = current[: max(0, len(current) - 1)] if choice == "shrink" else current + f"+{counter}"
        return author, LocalWrite("content", paths=(path,)), replica.write_text(path, text, author=author)
    path = f"work/f{counter}.md"
    return author, LocalWrite("content", paths=(path,)), replica.write_text(
        path, "x" * rng.randrange(1, 20), author=author
    )


@pytest.mark.parametrize("seed", range(30))
def test_random_honest_writes_are_accepted_and_always_advance_the_vector(seed: int) -> None:
    rng = random.Random(seed)
    admission = ResultStateAdmission()
    sender = Replica.create(members={user: "rw" for user in USERS}, client_id=1 + seed)
    sender.makedirs("directory/devices", author=OWNER)
    sender.makedirs("work", author=OWNER)
    for user, name in USERS.items():
        sender.makedirs(f"directory/devices/{name}", author=user)
    receiver = sender.clone(500 + seed)
    for counter in range(40):
        caller, write, frames = _step(rng, sender, counter)
        assert local_write_error(caller, write, receiver.view(), POLICY) is None, (caller, write)
        for frame in frames:
            before = receiver.vector(frame.doc_id) if frame.doc_id in receiver.docs else b""
            assert vector_advanced(before, frame.vector)
            result = deliver(admission, receiver, frame)
            assert result == Accepted(), (seed, counter, frame.doc_id, result)
            assert vector_advanced(before, receiver.vector(frame.doc_id))
    for doc_id in sender.docs:
        assert decode_vector(receiver.vector(doc_id)) == decode_vector(sender.vector(doc_id))
        assert receiver.staged(doc_id).materialize() == sender.staged(doc_id).materialize()
