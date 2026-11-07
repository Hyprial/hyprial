from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from hyprial.daemon.impl.adapters.lark import LarkSdkGateway, normalize_sdk_event
from hyprial.daemon.impl.adapters.lark.contracts.messages import VisibleAttachmentLabel, VisibleLinkLabel, VisibleMessage, VisibleParagraph, VisibleHeading, VisiblePlaceholder, VisibleText, _card_message, _post_message, _render_message, _visible_message_for_content, normalize_lark_message_content

from ..support import (
    FIXTURES,
    FakeClient,
    _RecordingLogger,
    response,
    rest_message,
)


def test_known_lark_message_types_have_safe_deterministic_text() -> None:
    opaque = {"access_token": "must-not-leak", "image_key": "img_secret"}
    cases = {
        "text": {"text": "plain"},
        "post": {"content": [[{"tag": "text", "text": "rich"}]]},
        "merge_forward": {"content": "Merged and Forwarded Message"},
        "file": {**opaque, "file_name": "report.txt"},
        "interactive": {
            **opaque,
            "elements": [{"tag": "markdown", "content": "card body"}],
        },
        "todo": {
            **opaque,
            "summary": {"content": [[{"tag": "text", "text": "task"}]]},
        },
        "vote": {**opaque, "topic": "choice", "options": ["one", "two"]},
        "image": opaque,
        "audio": opaque,
        "media": opaque,
        "sticker": opaque,
        "folder": opaque,
        "share_chat": opaque,
        "share_user": opaque,
        "location": opaque,
        "hongbao": opaque,
        "calendar": opaque,
        "general_calendar": opaque,
        "video_chat": opaque,
        "system": opaque,
    }

    first = {
        message_type: normalize_lark_message_content(message_type, content)
        for message_type, content in cases.items()
    }
    second = {
        message_type: normalize_lark_message_content(message_type, content)
        for message_type, content in cases.items()
    }

    assert first == second
    assert {item.status for item in first.values()} == {"supported"}
    assert all(item.text for item in first.values())
    assert "must-not-leak" not in repr(first)
    assert "img_secret" not in repr(first)


def test_every_known_type_builds_ast_from_visible_fields_only() -> None:
    opaque = "opaque-platform-value"
    cases = {
        "text": {"text": "Visible", "raw": opaque},
        "post": {
            "content": [[{"tag": "text", "text": "Visible", "href": opaque}]],
            "raw": opaque,
        },
        "merge_forward": {"content": opaque},
        "file": {"file_name": "Visible", "file_key": opaque},
        "interactive": {
            "elements": [{"tag": "plain_text", "content": "Visible", "value": opaque}],
            "raw": opaque,
        },
        "todo": {
            "summary": {"content": [[{"tag": "text", "text": "Visible"}]]},
            "task_id": opaque,
        },
        "vote": {"topic": "Visible", "options": ["One"], "vote_id": opaque},
        **{message_type: {"raw": opaque} for message_type in (
            "image", "audio", "media", "sticker", "folder", "share_chat",
            "share_user", "location", "hongbao", "calendar",
            "general_calendar", "video_chat", "system",
        )},
    }

    for message_type, content in cases.items():
        ast = _visible_message_for_content(message_type, content)
        assert opaque not in repr(ast), message_type
        assert ast.nodes, message_type


def test_interactive_markdown_extracts_only_visible_safe_text() -> None:
    content = {
        "header": {"title": {"content": "Deploy status\u0000", "tag": "plain_text"}},
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "See [nested [label]](https://secret.invalid/path?q=1&token=abc) "
                    "and [escaped\\] label](https://other.invalid/a_(b))\\n"
                    "plain https://plain.invalid/?access_token=raw "
                    "access_token=top-secret target=opaque-target "
                    '{"authorization":"Bearer hidden }",'
                    '"safe":"must-not-copy-json"}'
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Approve"},
                        "url": "https://button.invalid/?token=button-secret",
                        "value": {"target": "opaque-action-target"},
                    }
                ],
            },
        ],
    }

    normalized = normalize_lark_message_content("interactive", content)

    assert normalized.status == "supported"
    assert normalized.message_type == "interactive"
    assert "Deploy status" in normalized.text
    assert "nested [label]" in normalized.text
    assert "escaped] label" in normalized.text
    assert "Approve" in normalized.text
    # This text is authored in a platform-visible Markdown field. The adapter
    # is a provenance filter, not a content DLP engine, so it stays verbatim.
    assert "access_token=top-secret" in normalized.text
    assert "target=opaque-target" in normalized.text
    assert "Bearer hidden" in normalized.text
    assert "must-not-copy-json" in normalized.text
    for forbidden in (
        "secret.invalid",
        "other.invalid",
        "button-secret",
        "opaque-action-target",
        "\x00",
    ):
        assert forbidden not in normalized.text
    assert "https://plain.invalid/?access_token=raw" in normalized.text


def test_visible_markdown_is_preserved_by_provenance_not_keyword_guessing() -> None:
    visible_values = (
        "token=user-authored-value",
        "authorization: Bearer user-authored-bearer",
        "123456789 true null",
    )

    for index, markdown in enumerate(visible_values):
        normalized = normalize_lark_message_content(
            "interactive",
            {"elements": [{"tag": "markdown", "content": markdown}]},
        )
        assert normalized.status == "supported", index
        assert normalized.message_type == "interactive", index
        assert markdown in normalized.text, (index, normalized.text)

    for raw_json in (
        '["user-authored", {"token": "not-copied-from-raw-json"}]',
        '"standalone raw JSON string"',
        "123456789",
        "true",
        "null",
    ):
        normalized = normalize_lark_message_content(
            "interactive",
            {"elements": [{"tag": "markdown", "content": raw_json}]},
        )
        assert raw_json in normalized.text

    bare_url = "https://user-visible.invalid/?token=user-authored"
    normalized = normalize_lark_message_content(
        "interactive",
        {"elements": [{"tag": "markdown", "content": bare_url}]},
    )
    assert bare_url in normalized.text


def test_rest_get_and_history_list_share_visible_field_provenance() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    content = {
        "elements": [
            {
                "tag": "markdown",
                "content": "authorization: Bearer user-visible",
                "href": "https://opaque.invalid/hidden-href",
                "value": {"target": "hidden-value"},
            }
        ],
        "unknown": {"raw": "hidden-unknown"},
    }
    item = rest_message(
        "om_card_guard",
        msg_type="interactive",
        content=content,
    )
    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(items=[item])
    )
    client.message.list_pages = [
        SimpleNamespace(has_more=False, page_token=None, items=[item])
    ]

    recovered = gateway.get_inbound_message("om_card_guard", chat_type="p2p")
    history = gateway.list_chat_messages("oc_dm", chat_type="p2p")

    assert recovered is not None
    assert len(history) == 1
    for message in (recovered, history[0]):
        assert message.message_type == "interactive"
        assert message.content_status == "supported"
        assert "authorization: Bearer user-visible" in message.text
        assert "hidden-href" not in message.text
        assert "hidden-value" not in message.text
        assert "hidden-unknown" not in message.text


def test_card_converter_ast_has_no_field_for_opaque_platform_values() -> None:
    opaque_keys = (
        "href",
        "url",
        "value",
        "image_key",
        "file_key",
        "open_id",
        "raw",
        "unknown",
    )
    for index, key in enumerate(opaque_keys):
        opaque = f"opaque-{index}-{key}"
        ast = _card_message(
            {
                "elements": [
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "Visible",
                                },
                                key: opaque,
                            }
                        ],
                    },
                    {"tag": "future_node", key: opaque},
                ],
                key: opaque,
            }
        )
        assert repr(ast).find(opaque) == -1
        assert ast.nodes == (
            VisibleText("Visible"),
            VisiblePlaceholder("unknown card element"),
        )


def test_card_schema_context_rejects_untagged_and_scalar_children() -> None:
    ast = _card_message(
        {
            "header": {
                "title": {"tag": "plain_text", "content": "Visible heading"}
            },
            "elements": [
                {"text": {"tag": "plain_text", "content": "hidden-text"}},
                {
                    "actions": [
                        {"tag": "plain_text", "content": "hidden-actions"}
                    ]
                },
                "hidden-scalar",
                42,
                {"tag": "future_node", "text": "hidden-unknown"},
            ],
        }
    )

    assert ast.nodes == (
        VisibleHeading(VisibleText("Visible heading")),
        VisiblePlaceholder("unknown card element"),
        VisiblePlaceholder("unknown card element"),
        VisiblePlaceholder("unknown card element"),
        VisiblePlaceholder("unknown card element"),
        VisiblePlaceholder("unknown card element"),
    )
    for hidden in (
        "hidden-text",
        "hidden-actions",
        "hidden-scalar",
        "hidden-unknown",
    ):
        assert hidden not in repr(ast)


@pytest.mark.parametrize(
    ("payload", "hidden"),
    (
        (
            {"tag": "plain_text", "content": "hidden-root"},
            "hidden-root",
        ),
        (
            {
                "header": {
                    "title": {"tag": "markdown", "content": "hidden-title"}
                }
            },
            "hidden-title",
        ),
        (
            {
                "elements": [
                    {
                        "tag": "action",
                        "actions": [{"tag": "text", "content": "hidden-action"}],
                    }
                ]
            },
            "hidden-action",
        ),
        (
            {
                "elements": [
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "markdown",
                                    "content": "hidden-button",
                                },
                            }
                        ],
                    }
                ]
            },
            "hidden-button",
        ),
    ),
)
def test_card_schema_rejects_known_tags_in_illegal_contexts(
    payload: object,
    hidden: str,
) -> None:
    ast = _card_message(payload)
    assert ast.nodes == (VisiblePlaceholder("unknown card element"),)
    assert hidden not in repr(ast)


def test_card_schema_accepts_only_documented_context_tag_pairs() -> None:
    ast = _card_message(
        {
            "header": {
                "title": {"tag": "plain_text", "content": "Visible title"}
            },
            "elements": [
                {"tag": "markdown", "content": "Visible body"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "Visible div"},
                    "fields": [
                        {"tag": "plain_text", "content": "Visible field"}
                    ],
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "Visible button",
                            },
                        }
                    ],
                },
            ],
        }
    )

    assert _render_message(ast) == (
        "Visible title\nVisible body\nVisible div\nVisible field\nVisible button"
    )


def test_renderer_preserves_repeated_visible_segments_in_order() -> None:
    message = VisibleMessage(
        (
            VisibleText("repeat"),
            VisibleParagraph((VisibleText("repeat"),)),
            VisibleText("repeat"),
        )
    )

    assert _render_message(message) == "repeat\nrepeat\nrepeat"


def test_header_is_one_mapping_and_plural_slots_alone_expand_lists() -> None:
    ast = _card_message(
        {
            "header": [
                {"title": {"tag": "plain_text", "content": "hidden-one"}},
                {"title": {"tag": "plain_text", "content": "hidden-two"}},
            ],
            "elements": [
                {"tag": "markdown", "content": "Visible one"},
                {"tag": "markdown", "content": "Visible two"},
            ],
        }
    )

    assert ast.nodes == (
        VisiblePlaceholder("unknown card element"),
        VisibleParagraph((VisibleText("Visible one"),)),
        VisibleParagraph((VisibleText("Visible two"),)),
    )
    assert "hidden-one" not in repr(ast)
    assert "hidden-two" not in repr(ast)


def test_link_ast_contains_label_and_never_destination() -> None:
    destination = "https://opaque.invalid/?token=not-an-ast-field"
    ast = _card_message(
        {
            "elements": [
                {"tag": "markdown", "content": f"[Visible label]({destination})"},
                {"tag": "markdown", "content": f"![alt]({destination})"},
            ]
        }
    )

    assert ast.nodes == (
        VisibleParagraph((VisibleLinkLabel((VisibleText("Visible label"),)),)),
        VisibleParagraph((VisibleAttachmentLabel("image"),)),
    )
    assert destination not in repr(ast)


def test_post_converter_property_only_known_visible_fields_enter_ast() -> None:
    opaque_keys = ("href", "image_key", "file_key", "open_id", "raw", "unknown")
    for index, key in enumerate(opaque_keys):
        opaque = f"opaque-post-{index}-{key}"
        ast = _post_message(
            {
                "title": "Visible title",
                "content": [[
                    {"tag": "text", "text": "Visible text", key: opaque},
                    {"tag": "a", "text": "Visible link", key: opaque},
                    {"tag": "at", "user_name": "Visible name", key: opaque},
                    {"tag": "future_node", key: opaque},
                ]],
                key: opaque,
            }
        )
        assert opaque not in repr(ast)
        rendered = normalize_lark_message_content(
            "post",
            {
                "title": "Visible title",
                "content": [[
                    {"tag": "text", "text": "Visible text", key: opaque},
                    {"tag": "a", "text": "Visible link", key: opaque},
                    {"tag": "at", "user_name": "Visible name", key: opaque},
                    {"tag": "future_node", key: opaque},
                ]],
                key: opaque,
            },
        )
        assert opaque not in rendered.text
        assert "Visible title" in rendered.text
        assert "Visible text" in rendered.text
        assert "Visible link" in rendered.text
        assert "@Visible name" in rendered.text
        assert "[attachment not parsed]" in rendered.text


def test_real_client_post_parallel_file_is_visible_on_event_and_rest_paths() -> None:
    payload = json.loads((FIXTURES / "post_with_files_1174.json").read_text())
    raw = payload["data"]["items"][0]
    message_id = raw["message_id"]
    content = raw["body"]["content"]
    expected = (
        "回复文件能收到吗?\n"
        "[Lark file: path-authz-check-return.txt "
        "ref:om_fixture_post_files_1174/file_v3_fixture_key_1174]"
    )

    event = SimpleNamespace(
        header=SimpleNamespace(event_id="evt_fixture_1174"),
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                message_type="post",
                content=content,
                chat_id=raw["chat_id"],
                chat_type="p2p",
                mentions=[],
                root_id=None,
                thread_id=None,
                parent_id=None,
                create_time=raw["create_time"],
            ),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(
                    open_id=raw["sender"]["id"], union_id=None
                ),
                sender_type=raw["sender"]["sender_type"],
            ),
        ),
    )
    assert normalize_sdk_event(event).text == expected

    client = FakeClient()
    item = rest_message(
        message_id,
        msg_type="post",
        raw_content=content,
        chat_id=raw["chat_id"],
    )
    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(items=[item])
    )
    client.message.list_pages = [
        SimpleNamespace(has_more=False, page_token=None, items=[item])
    ]
    gateway = LarkSdkGateway(client)

    recovered = gateway.get_inbound_message(message_id, chat_type="p2p")
    history = gateway.list_chat_messages(raw["chat_id"], chat_type="p2p")

    assert recovered is not None
    assert recovered.text == expected
    assert [message.text for message in history] == [expected]


def test_post_file_media_and_image_elements_keep_retrievable_refs() -> None:
    normalized = normalize_lark_message_content(
        "post",
        {
            "content": [[
                {
                    "tag": "file",
                    "file_key": "file_v3_inline",
                    "file_name": "inline.txt",
                },
                {
                    "tag": "media",
                    "file_key": "file_v3_video",
                    "image_key": "img_v3_cover",
                },
                {"tag": "img", "image_key": "img_v3_inline"},
            ]]
        },
        message_id="om_post_resources",
    )

    assert normalized.text == (
        "[file ref:om_post_resources/file_v3_inline] "
        "[media ref:om_post_resources/file_v3_video] "
        "[image ref:om_post_resources/img_v3_inline]"
    )
    assert "img_v3_cover" not in normalized.text


def test_locale_wrapped_post_prefers_content_v2_without_duplicate_rows() -> None:
    normalized = normalize_lark_message_content(
        "post",
        {
            "en_us": {
                "title": "Fallback",
                "content": [[{"tag": "text", "text": "old"}]],
            },
            "zh_cn": {
                "title": "标题",
                "content": [[{"tag": "text", "text": "legacy row"}]],
                "content_v2": [[{"tag": "md", "text": "new row"}]],
            },
        },
        message_id="om_post_locale",
    )

    assert normalized.text == "标题\nnew row"
    assert "legacy row" not in normalized.text
    assert "Fallback" not in normalized.text


def test_locale_wrapped_post_can_carry_parallel_files() -> None:
    normalized = normalize_lark_message_content(
        "post",
        {
            "zh_cn": {
                "title": "附件",
                "content": [[{"tag": "text", "text": "见文件"}]],
            },
            "files": [
                {
                    "file_key": "file_v3_locale",
                    "file_name": "locale.txt",
                    "is_folder": False,
                }
            ],
        },
        message_id="om_post_locale_file",
    )

    assert normalized.text == (
        "附件\n见文件\n"
        "[Lark file: locale.txt ref:om_post_locale_file/file_v3_locale]"
    )


def test_post_folder_is_visible_but_not_misrepresented_as_retrievable() -> None:
    normalized = normalize_lark_message_content(
        "post",
        {
            "content": [[{"tag": "text", "text": "folder follows"}]],
            "files": [
                {
                    "file_key": "file_v3_folder",
                    "file_name": "Design assets",
                    "is_folder": True,
                }
            ],
        },
        message_id="om_post_folder",
    )

    assert normalized.text == "folder follows\n[folder: Design assets]"
    assert "ref:" not in normalized.text


def test_unknown_post_tag_is_visible_and_logs_only_its_shape() -> None:
    logger = _RecordingLogger()
    secret = "https://secret.invalid/?token=must-not-leak"
    event = SimpleNamespace(
        header=SimpleNamespace(event_id="evt_unknown_post"),
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="om_unknown_post",
                message_type="post",
                content=json.dumps(
                    {
                        "content": [[
                            {
                                "tag": "future_attachment",
                                "file_key": "file_secret_value",
                                "url": secret,
                            }
                        ]]
                    }
                ),
                chat_id="oc_dm",
                chat_type="p2p",
                mentions=[],
                root_id=None,
                thread_id=None,
                parent_id=None,
                create_time="1791273496980",
            ),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_sender", union_id=None),
                sender_type="user",
            ),
        ),
    )

    normalized = normalize_sdk_event(event, logger=logger)

    assert normalized.text == "[attachment not parsed]"
    assert logger.records == [
        {
            "level": "warn",
            "event": "lark.post.attachment_unparsed",
            "nativeMessageId": "om_unknown_post",
            "shape": {
                "tags": ["future_attachment"],
                "keys": ["file_key", "tag", "url"],
            },
        }
    ]
    assert "file_secret_value" not in repr(logger.records)
    assert secret not in repr(logger.records)


def test_attachment_key_outside_post_rows_is_visible_and_warned_on_rest() -> None:
    client = FakeClient()
    logger = _RecordingLogger()
    item = rest_message(
        "om_outside_rows",
        msg_type="post",
        content={
            "content": [[{"tag": "text", "text": "visible body"}]],
            "future_attachment": {
                "media_key": "opaque-media-value",
                "download_url": "https://secret.invalid/?token=hidden",
            },
        },
    )
    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(items=[item])
    )
    gateway = LarkSdkGateway(client, logger=logger)

    recovered = gateway.get_inbound_message("om_outside_rows", chat_type="p2p")

    assert recovered is not None
    assert recovered.text == "visible body\n[attachment not parsed]"
    assert logger.records == [
        {
            "level": "warn",
            "event": "lark.post.attachment_unparsed",
            "nativeMessageId": "om_outside_rows",
            "shape": {
                "tags": [],
                "keys": ["download_url", "media_key"],
            },
        }
    ]
    assert "opaque-media-value" not in repr(logger.records)
    assert "secret.invalid" not in repr(logger.records)


def test_empty_content_v2_falls_back_to_populated_content() -> None:
    # Review 901: an empty content_v2 must not hide the text in content.
    content = {
        "title": "",
        "content": [[{"tag": "text", "text": "still readable"}]],
        "content_v2": [],
    }

    normalized = normalize_lark_message_content(
        "post", json.dumps(content), message_id="om_empty_v2"
    )

    assert "still readable" in normalized.text
    assert "Unreadable" not in normalized.text


def test_horizontal_rule_is_not_reported_as_an_attachment() -> None:
    content = {
        "content": [
            [{"tag": "text", "text": "above"}],
            [{"tag": "hr"}],
            [{"tag": "text", "text": "below"}],
        ]
    }

    normalized = normalize_lark_message_content(
        "post", json.dumps(content), message_id="om_hr"
    )

    assert "above" in normalized.text and "below" in normalized.text
    assert "attachment not parsed" not in normalized.text
    assert normalized.unparsed_post_shapes == ()
