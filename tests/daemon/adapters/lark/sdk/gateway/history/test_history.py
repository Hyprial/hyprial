from __future__ import annotations

from types import SimpleNamespace
from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.contracts.messages import MAX_MERGE_FORWARD_DEPTH, MAX_MERGE_FORWARD_ITEMS, normalize_lark_message_content
from hyprial.daemon.impl.adapters.lark.outbound.sdk import SDK_HTTP_TIMEOUT_SECONDS

from ..support import (
    FakeClient,
    MERGE_FORWARD_SHELL_BODY,
    install_get_items,
    merge_forward_get_items,
    response,
    rest_message,
)


def test_rest_mentions_carry_open_ids_for_bot_identity_matching() -> None:
    """Recovery-path messages must expose the same identity keys as live
    events, or a bot mention lost on a dead websocket could never re-route."""

    client = FakeClient()
    gateway = LarkSdkGateway(client)
    message = rest_message("om_mentioned", mention_key="@_user_1")
    message.mentions = [
        SimpleNamespace(
            key="@_user_1", name="HQ Adjutant", id="ou_bot_self", id_type="open_id"
        ),
        SimpleNamespace(
            key="@_user_2", name="Somebody", id="u_123", id_type="user_id"
        ),
    ]
    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(items=[message])
    )

    inbound = gateway.get_inbound_message("om_mentioned", chat_type="group")

    assert inbound is not None
    # Only open_id-typed identities are comparable to the bot's open_id.
    assert inbound.mention_open_ids == ("ou_bot_self",)
    assert inbound.mentions == ("HQ Adjutant", "Somebody")


def test_merge_forward_shell_body_is_a_bare_string_and_stays_readable() -> None:
    """The shell's REST body is not JSON.  Treating that as an unreadable
    body dead-lettered the whole forward on the recovery path."""

    normalized = normalize_lark_message_content(
        "merge_forward", MERGE_FORWARD_SHELL_BODY
    )

    assert normalized.status == "supported"
    assert normalized.message_type == "merge_forward"
    assert normalized.text == "[Lark merged-forward message]"

    # The tolerance is scoped to this one type: everything else must still
    # fail loudly rather than quietly forwarding an empty body.
    for message_type in ("text", "post", "file", "image"):
        other = normalize_lark_message_content(message_type, "{not valid json")
        assert other.status == "invalid", message_type
        assert other.text == f"[Unreadable Lark {message_type} message]"


def test_get_message_expands_merge_forward_children_from_the_same_response() -> None:
    """The forwarded conversation is in items[1:] of the response the adapter
    already fetches; it used to be discarded whole."""

    client = FakeClient()
    install_get_items(client, merge_forward_get_items())
    gateway = LarkSdkGateway(client)

    quote = gateway.get_message("om_forward_shell")

    assert quote is not None
    assert quote.message_type == "merge_forward"
    assert quote.text.splitlines()[0] == "[Lark merged-forward message: 5 messages]"
    # What the agent must actually be able to read: the words inside the
    # children, not a stand-in for them.
    assert "Incident 42" in quote.text
    assert "gateway timed out at the dashboard" in quote.text
    assert "second paragraph" in quote.text
    assert "rolled back to the previous build" in quote.text
    # An ``app`` sender is still content.  Dropping it is the same class of
    # silent loss this change exists to remove.
    assert "deploy finished" in quote.text
    # Non-text children carry shell-aware retrieval handles. Lark's resource
    # endpoint needs the received top-level shell rather than the child id.
    assert (
        "[Lark image "
        "ref:om_forward_shell/om_child_image/img_v3_fwd]" in quote.text
    )
    assert (
        "[Lark file: postmortem.md "
        "ref:om_forward_shell/om_child_file/file_v3_forwarded]" in quote.text
    )
    # Every expanded child is attributed to its sender.  Without a resolver
    # the raw platform id is shown — honest, if not friendly.
    assert "ou_sender: rolled back to the previous build" in quote.text
    assert "cli_bot_sender: deploy finished" in quote.text
    assert quote.text != "[Lark merged-forward message]"


def test_merge_forward_expansion_carries_refs_and_senders_but_never_urls() -> None:
    """The 2026-08-14 ruling narrowed the no-leak rule, it did not drop it:
    media refs and sender ids are now deliberate content; URLs, tokens and
    action payloads still never reach Harness text."""

    client = FakeClient()
    install_get_items(client, merge_forward_get_items())
    gateway = LarkSdkGateway(client)

    quote = gateway.get_message("om_forward_shell")

    assert quote is not None
    # Still withheld: link destinations and anything token-shaped.
    assert "must-not-leak" not in quote.text
    assert "https://" not in quote.text
    # Now deliberate: shell + child + key retrieval refs and senders.
    assert (
        "ref:om_forward_shell/om_child_image/img_v3_fwd" in quote.text
    )
    assert (
        "ref:om_forward_shell/om_child_file/file_v3_forwarded" in quote.text
    )
    assert "ou_sender" in quote.text
    assert "cli_bot_sender" in quote.text


def test_get_message_returns_readable_context_for_non_text_types() -> None:
    """Quoting anything but ``text`` used to raise, which the adapter turned
    into "Quoted context could not be resolved"."""

    for msg_type, content, expected in (
        (
            "image",
            {"image_key": "img_quoted"},
            "[Lark image ref:om_quote/img_quoted]",
        ),
        (
            "file",
            {"file_key": "file_quoted", "file_name": "notes.txt"},
            "[Lark file: notes.txt ref:om_quote/file_quoted]",
        ),
        (
            "post",
            {"content": [[{"tag": "text", "text": "quoted rich text"}]]},
            "quoted rich text",
        ),
    ):
        client = FakeClient()
        install_get_items(
            client,
            [rest_message("om_quote", msg_type=msg_type, content=content)],
        )
        gateway = LarkSdkGateway(client)

        quote = gateway.get_message("om_quote")

        assert quote is not None, msg_type
        assert quote.message_type == msg_type
        assert quote.text == expected
        assert "must-not-leak" not in quote.text


def test_merge_forward_expansion_reports_how_many_messages_it_withheld() -> None:
    shell = "om_wide_shell"
    items = [
        rest_message(
            shell, msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY
        )
    ]
    items.extend(
        rest_message(
            f"om_child_{index}",
            upper_message_id=shell,
            text=f"forwarded line {index}",
        )
        for index in range(MAX_MERGE_FORWARD_ITEMS + 7)
    )
    client = FakeClient()
    install_get_items(client, items)
    gateway = LarkSdkGateway(client)

    quote = gateway.get_message(shell)

    assert quote is not None
    total = MAX_MERGE_FORWARD_ITEMS + 7
    assert quote.text.splitlines()[0] == (
        f"[Lark merged-forward message: {total} messages]"
    )
    assert f"forwarded line {MAX_MERGE_FORWARD_ITEMS - 1}" in quote.text
    assert f"forwarded line {MAX_MERGE_FORWARD_ITEMS}" not in quote.text
    # Bounded, but never silently: the agent is told the exact shortfall.
    assert quote.text.splitlines()[-1] == (
        f"[7 of {total} forwarded messages not shown "
        f"(limit: {MAX_MERGE_FORWARD_ITEMS} messages)]"
    )


def test_nested_merge_forward_is_expanded_to_the_depth_limit_then_declared() -> None:
    """A forward inside a forward terminates: children are only followed while
    the flat response keeps supplying them, and never past the depth cap."""

    shell = "om_outer"
    items = [
        rest_message(
            shell, msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY
        )
    ]
    parent = shell
    for level in range(1, MAX_MERGE_FORWARD_DEPTH + 2):
        child = f"om_level_{level}"
        items.append(
            rest_message(
                child,
                msg_type="merge_forward",
                raw_content=MERGE_FORWARD_SHELL_BODY,
                upper_message_id=parent,
            )
        )
        items.append(
            rest_message(
                f"om_leaf_{level}",
                upper_message_id=child,
                text=f"leaf at level {level}",
            )
        )
        parent = child

    client = FakeClient()
    install_get_items(client, items)
    gateway = LarkSdkGateway(client)

    quote = gateway.get_message(shell)

    assert quote is not None
    assert "leaf at level 1" in quote.text
    assert f"leaf at level {MAX_MERGE_FORWARD_DEPTH + 1}" not in quote.text
    assert (
        f"[nested forwarded messages not expanded "
        f"(depth limit {MAX_MERGE_FORWARD_DEPTH})]" in quote.text
    )
    # A nested shell still announces itself even when it is not followed.
    assert "[Lark merged-forward message]" in quote.text
    assert "not shown" in quote.text.splitlines()[-1]
    # One response, one call: expansion never fans out into extra fetches.
    assert len(client.message.gets) == 1


def test_merge_forward_expansion_survives_a_cyclic_upper_message_id() -> None:
    shell = "om_cycle_shell"
    items = [
        rest_message(
            shell, msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY
        ),
        rest_message(
            "om_a", msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY,
            upper_message_id=shell,
        ),
        rest_message(
            "om_b", msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY,
            upper_message_id="om_a",
        ),
        # om_a claims om_b as its parent as well: a cycle.
        rest_message(
            "om_a", msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY,
            upper_message_id="om_b",
        ),
    ]
    client = FakeClient()
    install_get_items(client, items)
    gateway = LarkSdkGateway(client)

    quote = gateway.get_message(shell)

    assert quote is not None
    # Three children, two rendered: the cycle is broken, not followed, and the
    # unrendered one is declared rather than dropped.
    assert quote.text.splitlines()[0] == "[Lark merged-forward message: 3 messages]"
    assert quote.text.splitlines()[-1] == (
        "[1 of 3 forwarded messages not shown (limit: 50 messages)]"
    )


def test_get_inbound_message_normalizes_rest_model() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)

    def get(request):  # type: ignore[no-untyped-def]
        return response(
            data=SimpleNamespace(
                items=[
                    rest_message(
                        "om_lost",
                        text="@_user_1 lost in the outage",
                        mention_key="@_user_1",
                    )
                ]
            )
        )

    client.message.get = get  # type: ignore[method-assign]

    message = gateway.get_inbound_message("om_lost", chat_type="p2p")

    assert message is not None
    assert message.event_id == "recover:om_lost"
    assert message.message_id == "om_lost"
    assert message.chat_id == "oc_dm"
    assert message.chat_type == "p2p"
    assert message.text == "lost in the outage"  # mention key stripped
    assert message.mentions == ("agent",)
    assert message.sender_id == "ou_sender"
    assert message.sender_type == "user"
    assert message.create_time == "1720000002000"
    assert message.message_type == "text"


def test_get_inbound_message_filters_undeliverable_messages() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)

    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(
            items=[rest_message("om_bot", sender_type="app")]
        )
    )
    assert gateway.get_inbound_message("om_bot") is None

    client.message.get = lambda request: response(data=SimpleNamespace(items=[]))  # type: ignore[method-assign]
    assert gateway.get_inbound_message("om_gone") is None


def test_get_inbound_message_normalizes_nontext_without_exposing_urls() -> None:
    """An embedded post image keeps its retrieval ref (the 2026-08-14
    ruling), while link destinations stay out of Harness text."""

    client = FakeClient()
    gateway = LarkSdkGateway(client)
    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(
            items=[
                rest_message(
                    "om_post",
                    msg_type="post",
                    content={
                        "title": "Release note",
                        "content": [[
                            {"tag": "text", "text": "ready "},
                            {
                                "tag": "a",
                                "text": "details",
                                "href": "https://secret.invalid/?token=must-not-leak",
                            },
                            {
                                "tag": "img",
                                "image_key": "img_v2_embedded",
                            },
                        ]],
                    },
                )
            ]
        )
    )

    message = gateway.get_inbound_message("om_post", chat_type="p2p")

    assert message is not None
    assert message.message_type == "post"
    assert message.content_status == "supported"
    assert message.text == (
        "Release note\nready details [image ref:om_post/img_v2_embedded]"
    )
    assert "must-not-leak" not in message.text
    assert "https://" not in message.text


def test_rest_normalization_covers_merge_forward_attachment_and_unknown() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    client.message.list_pages = [
        SimpleNamespace(
            has_more=False,
            page_token=None,
            items=[
                rest_message(
                    "om_merge",
                    msg_type="merge_forward",
                    content={"content": "Merged and Forwarded Message"},
                ),
                rest_message(
                    "om_image",
                    msg_type="image",
                    content={"image_key": "img_v2_history"},
                ),
                rest_message(
                    "om_future",
                    msg_type="future_secret_type",
                    content={"access_token": "must-not-leak"},
                ),
            ],
        )
    ]

    messages = gateway.list_chat_messages("oc_dm", chat_type="p2p")

    assert [(item.message_type, item.text, item.content_status) for item in messages] == [
        ("merge_forward", "[Lark merged-forward message]", "supported"),
        ("image", "[Lark image ref:om_image/img_v2_history]", "supported"),
        (
            "unknown",
            "[Unsupported Lark message type]",
            "unsupported",
        ),
    ]
    assert "must-not-leak" not in repr(messages)


def test_list_chat_messages_paginates_filters_and_bounds() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    client.message.list_pages = [
        SimpleNamespace(
            has_more=True,
            page_token="page-2",
            items=[
                rest_message("om_1", text="first"),
                rest_message("om_img", msg_type="image"),
                rest_message("om_bot", sender_type="app"),
            ],
        ),
        SimpleNamespace(
            has_more=False,
            page_token=None,
            items=[rest_message("om_2", text="second")],
        ),
    ]

    messages = gateway.list_chat_messages(
        "oc_dm", start_time="1719999105", chat_type="p2p"
    )

    assert [item.message_id for item in messages] == ["om_1", "om_img", "om_2"]
    assert {item.chat_type for item in messages} == {"p2p"}
    assert {item.event_id for item in messages} == {
        "reconcile:om_1",
        "reconcile:om_img",
        "reconcile:om_2",
    }
    assert len(client.message.lists) == 2
    first, second = client.message.lists
    assert first.container_id_type == "chat"
    assert first.container_id == "oc_dm"
    assert first.sort_type == "ByCreateTimeDesc"
    assert first.start_time == "1719999105"
    assert first.page_token is None
    assert second.page_token == "page-2"

    # has_more that never clears must not crawl forever.
    client.message.lists.clear()
    client.message.list_pages = [
        SimpleNamespace(
            has_more=True,
            page_token="next",
            items=[rest_message("om_loop")],
        )
    ] * 10
    bounded = gateway.list_chat_messages("oc_dm", max_pages=3)
    assert len(client.message.lists) == 3
    assert bounded.complete is False  # type: ignore[attr-defined]


def test_list_chat_messages_reaches_newest_message_beyond_four_pages() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    client.message.list_pages = [
        SimpleNamespace(
            has_more=index < 4,
            page_token=f"page-{index + 2}" if index < 4 else None,
            items=[
                rest_message(f"om_{offset}")
                for offset in range(index * 50 + 1, index * 50 + 51)
            ],
        )
        for index in range(5)
    ]

    messages = gateway.list_chat_messages("oc_dm")

    assert len(messages) == 250
    assert any(item.message_id == "om_201" for item in messages)
    assert len(client.message.lists) == 5
    assert {request.sort_type for request in client.message.lists} == {
        "ByCreateTimeDesc"
    }


def test_sdk_client_pins_explicit_per_request_timeout() -> None:
    """History paging must never depend on the vendored default timeout."""

    gateway = LarkSdkGateway.from_credentials("cli_fake", "secret")

    assert gateway._client._config.timeout == SDK_HTTP_TIMEOUT_SECONDS
    assert gateway._client._config.timeout is not None
