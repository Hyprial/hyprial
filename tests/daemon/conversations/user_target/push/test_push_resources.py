"""I4b (D2): attachments travel with the owner's cell push of ``user:<own owner>``.

Decision notes:

- The ``route:`` door used to be the only way to post a file or image.  It is
  gone; ``message.send`` to this node's owner posts each resource to every
  chat the text copy goes to, after the text, under
  ``<push key>:resource:<i>``.  The delivery entry keeps ``pushed`` / ``via``
  / ``pushCode`` and adds ``resourceDeliveries`` (one row per posted
  resource, with ``kind``) when pushed.
- Any other target with ``resourcePaths`` is ``UNSUPPORTED_TARGET`` naming
  the first such target; a ``replyTo`` with resources is ``INVALID_ARGUMENT``.
  Both refuse before any side effect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.kernel import DaemonRequestError
from tests.daemon.acceptance.acceptance_support import session
from tests.daemon.conversations.bypass.bypass_support import boot, stored
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, ALPHA, BETA, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import (
    edit_gateway,
    fail_chats,
    install_cell,
    push_cell,
    user_agent_cell,
)

DM, GROUP = "oc_dm_chat", "oc_group_chat"
FANOUT_ROUTES = [
    {"name": "dm", "type": "direct", "nativeId": DM},
    {"name": "group", "type": "direct", "nativeId": GROUP},
    {"name": "both", "type": "fanout", "members": ["dm", "group"]},
]


class Posts:
    """Records the file and image posts a golden chat gateway receives."""

    def __init__(self, gateway: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.files: list[tuple[str, str, bytes, str, str]] = []
        self.images: list[tuple[str, str, bytes, str, str]] = []
        self.order: list[str] = []
        send_chat = gateway.send_chat

        def text(chat_id: str, body: str, *, idempotency_key: str) -> str:
            self.order.append(f"text:{chat_id}")
            return send_chat(chat_id, body, idempotency_key=idempotency_key)

        def file(chat_id: str, name: str, data: bytes, *, media_type: str, idempotency_key: str):
            self.order.append(f"file:{chat_id}")
            self.files.append((chat_id, name, data, media_type, idempotency_key))
            return {"kind": "file", "fileName": name, "nativeMessageId": f"om-file-{len(self.files)}"}

        def image(chat_id: str, name: str, data: bytes, *, media_type: str, idempotency_key: str):
            self.order.append(f"image:{chat_id}")
            self.images.append((chat_id, name, data, media_type, idempotency_key))
            return {"kind": "image", "fileName": name, "nativeMessageId": f"om-image-{len(self.images)}"}

        monkeypatch.setattr(gateway, "send_chat", text)
        monkeypatch.setattr(gateway, "send_chat_file", file, raising=False)
        monkeypatch.setattr(gateway, "send_chat_image", image, raising=False)


def _work(booted: Any) -> Path:
    """The golden agents' working directory, the one root their attachments may come from."""

    directory = Path(os.path.realpath(booted.daemon.root)) / "work"
    directory.mkdir(exist_ok=True)
    return directory


def _booted(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch, *, default_route: str | None = "dm"):
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    edit_gateway(booted.app, routes=FANOUT_ROUTES)
    if default_route is not None:
        edit_gateway(booted.app, defaultRoute=default_route)
    return booted, Posts(booted.daemon.gateway, monkeypatch)


def _user_agent_binds(app: Any, **cell: Any) -> Any:
    document = user_agent_cell(app, {"im": ADAPTER}, **cell)
    install_cell(app, document)
    return document


def _sender_binds(app: Any, **cell: Any) -> Any:
    agent = app.agents.require("alpha")
    document = push_cell(agent, {agent.uri: ("harness", None), "im": ("adapter", ADAPTER)}, **cell)
    install_cell(app, document)
    return document


def _file(work: Path, name: str = "proof.txt", data: bytes = b"proof\n") -> dict[str, str]:
    path = work / name
    path.write_bytes(data)
    return {"path": str(path), "kind": "file", "mediaType": "text/plain"}


def _image(work: Path) -> dict[str, str]:
    path = work / "pixel.png"
    path.write_bytes(b"png-bytes")
    return {"path": str(path), "kind": "image", "mediaType": "image/png"}


def _send(app: Any, to: list[str], key: str, **extra: Any) -> dict[str, Any]:
    return app.handle("message.send", {
        **session(ALPHA), "to": to, "summary": f"summary {key}", "message": f"body {key}",
        "conversationId": f"conv-{key}", "idempotencyKey": f"key-{key}", **extra,
    })


def _refusal(app: Any, to: list[str], key: str, **extra: Any) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        _send(app, to, key, **extra)
    return raised.value


def _pushed_key(cell: Any, key: str, route: str | None = None) -> str:
    return f"key-{key}:{OWNER_USER}:push:{cell.cell_id}:im:{route or ''}"


def test_a_file_follows_the_text_to_the_user_agent_cells_chat(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    cell = _user_agent_binds(app)

    [delivery] = _send(app, [OWNER_USER], "file", resourcePaths=[_file(work)])["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["via"]) == (True, True, "user-agent")
    assert "pushCode" not in delivery
    base = _pushed_key(cell, "file")
    assert [(chat, key) for chat, _text, key in booted.daemon.gateway.calls] == [(DM, base)]
    assert posts.files == [(DM, "proof.txt", b"proof\n", "text/plain", f"{base}:resource:0")]
    assert posts.order == [f"text:{DM}", f"file:{DM}"]
    assert [row["kind"] for row in delivery["resourceDeliveries"]] == ["file"]
    assert set(stored(app, delivery["messageId"])) == {"inbox", "sender_copies"}


def test_a_bound_senders_cell_receives_a_file_and_an_image_in_order(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    cell = _sender_binds(app)

    [delivery] = _send(
        app, [OWNER_USER], "both-kinds", resourcePaths=[_file(work), _image(work)]
    )["deliveries"]

    assert (delivery["pushed"], delivery["via"]) == (True, "cell")
    base = _pushed_key(cell, "both-kinds")
    assert posts.files == [(DM, "proof.txt", b"proof\n", "text/plain", f"{base}:resource:0")]
    assert posts.images == [(DM, "pixel.png", b"png-bytes", "image/png", f"{base}:resource:1")]
    assert posts.order == [f"text:{DM}", f"file:{DM}", f"image:{DM}"]
    assert [row["kind"] for row in delivery["resourceDeliveries"]] == ["file", "image"]


def test_every_chat_of_a_fanout_receives_the_resource_under_its_member_key(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    work = _work(booted)
    agent = app.agents.require("alpha")
    cell = _sender_binds(app, connections=(
        {"from": agent.uri, "on": "user", "to": "im", "route": "both"},
    ))

    [delivery] = _send(app, [OWNER_USER], "fan", resourcePaths=[_file(work)])["deliveries"]

    base = _pushed_key(cell, "fan", "both")
    assert [(call[0], call[4]) for call in posts.files] == [
        (DM, f"{base}:dm:resource:0"), (GROUP, f"{base}:group:resource:0"),
    ]
    assert [row["kind"] for row in delivery["resourceDeliveries"]] == ["file", "file"]


def test_a_text_that_the_platform_refuses_posts_no_resource(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: LarkApiError("send message", 230002, "x")})

    [delivery] = _send(app, [OWNER_USER], "refused", resourcePaths=[_file(work)])["deliveries"]

    assert delivery["pushed"] is False
    assert posts.files == []
    assert not delivery.get("resourceDeliveries")


def _fail_files(gateway: Any, posts: Posts, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(chat_id: str, name: str, data: bytes, *, media_type: str, idempotency_key: str):
        posts.order.append(f"file:{chat_id}")
        raise LarkApiError("send file", 230002, "x")

    monkeypatch.setattr(gateway, "send_chat_file", refused, raising=False)


def test_an_attachment_refused_after_the_text_landed_reports_what_was_posted(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)
    _fail_files(booted.daemon.gateway, posts, monkeypatch)

    [delivery] = _send(
        app, [OWNER_USER], "half", resourcePaths=[_image(work), _file(work)]
    )["deliveries"]

    assert posts.order == [f"text:{DM}", f"image:{DM}", f"file:{DM}"]
    assert (delivery["pushed"], delivery["pushCode"]) == (True, "USER_DELIVERY_FAILED")
    assert [row["kind"] for row in delivery["resourceDeliveries"]] == ["image"]
    [record] = booted.logs.named("send.user_push")
    [target] = record["targets"]
    assert (target["state"], target["reason"], target["larkCode"]) == ("sent", "resource-partial", 230002)


def test_a_retry_with_the_same_key_reposts_under_the_same_keys(golden_daemon, monkeypatch) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    cell = _user_agent_binds(app)
    gateway = booted.daemon.gateway
    succeed = gateway.send_chat_file
    _fail_files(gateway, posts, monkeypatch)
    resources = [_image(work), _file(work)]
    _send(app, [OWNER_USER], "again", resourcePaths=resources)
    monkeypatch.setattr(gateway, "send_chat_file", succeed, raising=False)

    [delivery] = _send(app, [OWNER_USER], "again", resourcePaths=resources)["deliveries"]

    base = _pushed_key(cell, "again")
    assert [key for _chat, _text, key in gateway.calls] == [base, base]
    assert [call[4] for call in posts.images] == [f"{base}:resource:0", f"{base}:resource:0"]
    assert [call[4] for call in posts.files] == [f"{base}:resource:1"]
    assert (delivery["pushed"], "pushCode" in delivery) == (True, False)


def test_the_push_only_send_reports_the_text_when_an_attachment_is_refused(
    golden_daemon, monkeypatch
) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    edit_gateway(app, routes=FANOUT_ROUTES, defaultRoute="dm")
    posts = Posts(booted.daemon.gateway, monkeypatch)
    work = _work(booted)
    _sender_binds(app)
    _fail_files(booted.daemon.gateway, posts, monkeypatch)

    [delivery] = _send(
        app, [OWNER_USER], "push-only-half", resourcePaths=[_image(work), _file(work)]
    )["deliveries"]

    assert posts.order == [f"text:{DM}", f"image:{DM}", f"file:{DM}"]
    assert (delivery["accepted"], delivery["queued"]) == (True, False)
    assert delivery["pushCode"] == "USER_DELIVERY_FAILED"
    assert [row["kind"] for row in delivery["resourceDeliveries"]] == ["image"]


def test_a_send_without_resources_has_no_resource_deliveries(golden_daemon, monkeypatch) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)

    [delivery] = _send(app, [OWNER_USER], "plain")["deliveries"]

    assert delivery["pushed"] is True
    assert "resourceDeliveries" not in delivery
    assert posts.files == [] and posts.images == []


def test_resources_with_reply_to_are_an_invalid_argument_before_any_post(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)

    refusal = _refusal(app, [OWNER_USER], "reply", replyTo="om_inbound", resourcePaths=[_file(work)])

    assert refusal.code == "INVALID_ARGUMENT"
    assert booted.daemon.gateway.calls == []
    assert posts.files == []


@pytest.mark.parametrize(
    "target",
    [BETA, "user:someone-else"],
    ids=["agent", "another-owners-user"],
)
def test_resources_to_any_other_target_are_unsupported_before_any_side_effect(
    golden_daemon, monkeypatch, target
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)

    refusal = _refusal(app, [target], "other", resourcePaths=[_file(work)])

    assert refusal.code == "UNSUPPORTED_TARGET"
    assert refusal.data == {"target": target}
    assert booted.daemon.gateway.calls == []
    assert posts.files == []
    assert stored(app, "conv-other") == {}


def test_a_mixed_send_names_the_first_unsupported_target_and_posts_nothing(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)

    refusal = _refusal(app, [OWNER_USER, BETA, "user:someone-else"], "mixed", resourcePaths=[_file(work)])

    assert refusal.code == "UNSUPPORTED_TARGET"
    assert refusal.data == {"target": BETA}
    assert booted.daemon.gateway.calls == []
    assert posts.files == []


@pytest.mark.parametrize(
    ("resource", "code"),
    [
        ({"path": "missing.txt", "kind": "file"}, "RESOURCE_NOT_FOUND"),
        ({"path": "any.txt", "kind": "archive"}, "RESOURCE_KIND_UNSUPPORTED"),
    ],
)
def test_an_invalid_resource_to_the_owner_fails_before_the_text_is_posted(
    golden_daemon, monkeypatch, resource, code
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)

    resource = {**resource, "path": str(_work(booted) / resource["path"])}
    assert _refusal(app, [OWNER_USER], "invalid", resourcePaths=[resource]).code == code
    assert booted.daemon.gateway.calls == []
    assert posts.files == []


def test_empty_and_oversized_files_to_the_owner_fail_before_the_text_is_posted(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)
    empty = work / "empty.txt"
    empty.touch()
    oversized = work / "oversized.bin"
    with oversized.open("wb") as handle:
        handle.truncate(30 * 1024 * 1024 + 1)

    for path, code in ((empty, "RESOURCE_EMPTY"), (oversized, "RESOURCE_TOO_LARGE")):
        refusal = _refusal(app, [OWNER_USER], f"size-{code}", resourcePaths=[{"path": str(path), "kind": "file"}])
        assert refusal.code == code
    assert booted.daemon.gateway.calls == []
    assert posts.files == []


def test_unsupported_and_oversized_images_to_the_owner_fail_before_the_text_is_posted(
    golden_daemon, monkeypatch
) -> None:
    booted, posts = _booted(golden_daemon, monkeypatch)
    app = booted.app
    work = _work(booted)
    _user_agent_binds(app)
    unsupported = work / "picture.svg"
    unsupported.write_text("<svg/>", encoding="utf-8")
    oversized = work / "oversized.png"
    with oversized.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)

    for path, code in (
        (unsupported, "RESOURCE_IMAGE_TYPE_UNSUPPORTED"),
        (oversized, "RESOURCE_TOO_LARGE"),
    ):
        refusal = _refusal(app, [OWNER_USER], f"img-{code}", resourcePaths=[{"path": str(path), "kind": "image"}])
        assert refusal.code == code
    assert booted.daemon.gateway.calls == []
    assert posts.images == []
