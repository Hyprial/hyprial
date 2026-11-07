from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace



FIXTURES = Path(__file__).resolve().parents[6] / 'tests/fixtures/lark'






























def response(*, data=None, success: bool = True):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        success=lambda: success,
        code=0 if success else 999,
        msg="ok" if success else "failed",
        data=data,
    )














class _RecordingLogger:
    """Capture send-outcome lines the way the adapter logger would."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def log(self, level: str, event: str, **fields: object) -> None:
        self.records.append({"level": level, "event": event, **fields})


def _nonzero_response_with_message_id() -> SimpleNamespace:
    """The platform shape that used to look like success: a message id is
    present, but the business ``code`` says the send was rejected."""

    return SimpleNamespace(
        success=lambda: False,
        code=230012,
        msg="content rejected",
        data=SimpleNamespace(message_id="om_trap"),
    )










class FakeMessageApi:
    def __init__(self) -> None:
        self.creates = []
        self.replies = []
        self.gets = []
        self.lists = []
        self.list_pages: list[SimpleNamespace] = []

    def create(self, request):  # type: ignore[no-untyped-def]
        self.creates.append(request)
        request_content = json.loads(request.request_body.content)
        if "file_key" in request_content:
            response_content = {"file_key": "file_sent"}
        elif "image_key" in request_content:
            response_content = {"image_key": "img_sent"}
        else:
            response_content = request_content
        return response(
            data=SimpleNamespace(
                message_id="om_owner_dm",
                body=SimpleNamespace(content=json.dumps(response_content)),
            )
        )

    def reply(self, request):  # type: ignore[no-untyped-def]
        self.replies.append(request)
        return response(data=SimpleNamespace(message_id="om_reply"))

    def get(self, request):  # type: ignore[no-untyped-def]
        self.gets.append(request)
        sender = SimpleNamespace(id="ou_sender", sender_type="user")
        body = SimpleNamespace(content=json.dumps({"text": "quoted"}))
        # ``msg_type`` is part of the real REST model and now decides how the
        # body is read; the quoted-context path is no longer text-only.
        message = SimpleNamespace(
            message_id="om_quote", msg_type="text", sender=sender, body=body
        )
        return response(data=SimpleNamespace(items=[message]))

    def list(self, request):  # type: ignore[no-untyped-def]
        self.lists.append(request)
        page = self.list_pages[len(self.lists) - 1]
        return response(data=page)


class FakeReactionApi:
    def __init__(self) -> None:
        self.creates = []
        self.lists = []
        self.deletes = []

    def create(self, request):  # type: ignore[no-untyped-def]
        self.creates.append(request)
        return response(data=SimpleNamespace(reaction_id="reaction_app"))

    def list(self, request):  # type: ignore[no-untyped-def]
        self.lists.append(request)
        app_reaction = SimpleNamespace(
            reaction_id="reaction_app",
            operator=SimpleNamespace(operator_type="app"),
        )
        user_reaction = SimpleNamespace(
            reaction_id="reaction_user",
            operator=SimpleNamespace(operator_type="user"),
        )
        return response(data=SimpleNamespace(items=[user_reaction, app_reaction]))

    def delete(self, request):  # type: ignore[no-untyped-def]
        self.deletes.append(request)
        return response()


class FakeFileApi:
    def __init__(self) -> None:
        self.creates = []

    def create(self, request):  # type: ignore[no-untyped-def]
        self.creates.append(request)
        return response(data=SimpleNamespace(file_key="file_uploaded"))


class FakeImageApi:
    def __init__(self) -> None:
        self.creates = []

    def create(self, request):  # type: ignore[no-untyped-def]
        self.creates.append(request)
        return response(data=SimpleNamespace(image_key="img_uploaded"))


class FakeClient:
    def __init__(self) -> None:
        self.message = FakeMessageApi()
        self.reaction = FakeReactionApi()
        self.file = FakeFileApi()
        self.image = FakeImageApi()
        self.im = SimpleNamespace(
            v1=SimpleNamespace(
                message=self.message,
                message_reaction=self.reaction,
                file=self.file,
                image=self.image,
            )
        )












class _BotInfoClient(FakeClient):
    def __init__(self, payloads: list[object]) -> None:
        super().__init__()
        self.payloads = payloads
        self.requests: list[object] = []

    def request(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        payload = self.payloads[min(len(self.requests), len(self.payloads)) - 1]
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(
            raw=SimpleNamespace(content=json.dumps(payload).encode("utf-8"))
        )










class FakeWsClient:
    def __init__(self) -> None:
        self.starts = 0
        self.probes = 0

    def start(self) -> None:
        self.starts += 1

    def _get_conn_url(self) -> str:
        self.probes += 1
        return "wss://unit.invalid/connection"










def rest_message(
    message_id: str,
    *,
    msg_type: str = "text",
    sender_type: str = "user",
    text: str = "history body",
    create_time="1720000002000",
    chat_id: str = "oc_dm",
    mention_key: str | None = None,
    content: object | None = None,
    raw_content: str | None = None,
    upper_message_id: str | None = None,
    sender_id: str = "ou_sender",
) -> SimpleNamespace:
    """The REST ``Message`` model shape returned by get/list."""

    mentions = []
    if mention_key is not None:
        mentions.append(
            SimpleNamespace(key=mention_key, name="agent", id=SimpleNamespace())
        )
    body_content = (
        raw_content
        if raw_content is not None
        else json.dumps(
            {"text": text} if content is None else content,
            ensure_ascii=False,
        )
    )
    return SimpleNamespace(
        message_id=message_id,
        root_id=None,
        parent_id=None,
        thread_id=None,
        msg_type=msg_type,
        create_time=create_time,
        chat_id=chat_id,
        upper_message_id=upper_message_id,
        sender=SimpleNamespace(id=sender_id, sender_type=sender_type),
        body=SimpleNamespace(content=body_content),
        mentions=mentions,
    )


#: The platform's own shell body: a bare string, *not* a JSON object.  Verified
#: against ``GET /open-apis/im/v1/messages/<merge_forward id>``.
MERGE_FORWARD_SHELL_BODY = "Merged and Forwarded Message"


def merge_forward_get_items(
    shell_id: str = "om_forward_shell",
) -> list[SimpleNamespace]:
    """A ``im.v1.message.get`` response for a merge-forward, real shape.

    Modelled byte-for-byte on a live 23-item response:

    * index 0 is the shell.  Its ``msg_type`` is ``merge_forward`` and its
      ``body.content`` is the bare placeholder string, not JSON.
    * every following item is a child carrying ``upper_message_id`` == the
      shell's id, its own real ``msg_type`` and its own real body.
    * children come from the *source* chat, not the chat the forward landed
      in, and at least one child was sent by an ``app`` rather than a user.
    """

    return [
        rest_message(
            shell_id,
            msg_type="merge_forward",
            raw_content=MERGE_FORWARD_SHELL_BODY,
            chat_id="oc_dm",
        ),
        rest_message(
            "om_child_post",
            msg_type="post",
            chat_id="oc_source",
            upper_message_id=shell_id,
            content={
                "title": "Incident 42",
                "content": [
                    [
                        {"tag": "text", "text": "gateway timed out at "},
                        {
                            "tag": "a",
                            "text": "the dashboard",
                            "href": "https://secret.invalid/?token=must-not-leak",
                        },
                    ],
                    [{"tag": "text", "text": "second paragraph"}],
                ],
            },
        ),
        rest_message(
            "om_child_text",
            msg_type="text",
            chat_id="oc_source",
            upper_message_id=shell_id,
            text="rolled back to the previous build",
        ),
        rest_message(
            "om_child_image",
            msg_type="image",
            chat_id="oc_source",
            upper_message_id=shell_id,
            content={"image_key": "img_v3_fwd"},
        ),
        rest_message(
            "om_child_file",
            msg_type="file",
            chat_id="oc_source",
            upper_message_id=shell_id,
            content={
                "file_key": "file_v3_forwarded",
                "file_name": "postmortem.md",
            },
        ),
        rest_message(
            "om_child_bot",
            msg_type="text",
            chat_id="oc_source",
            upper_message_id=shell_id,
            sender_type="app",
            sender_id="cli_bot_sender",
            text="deploy finished",
        ),
    ]


def install_get_items(client: FakeClient, items: list[SimpleNamespace]) -> None:
    def get(request):  # type: ignore[no-untyped-def]
        client.message.gets.append(request)
        return response(data=SimpleNamespace(items=items))

    client.message.get = get  # type: ignore[method-assign]


































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FIXTURES',
    'FakeClient',
    'FakeFileApi',
    'FakeImageApi',
    'FakeMessageApi',
    'FakeReactionApi',
    'FakeWsClient',
    'MERGE_FORWARD_SHELL_BODY',
    '_BotInfoClient',
    '_RecordingLogger',
    '_nonzero_response_with_message_id',
    'install_get_items',
    'merge_forward_get_items',
    'response',
    'rest_message',
]
