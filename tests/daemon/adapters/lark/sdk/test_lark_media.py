"""Adapter-encapsulated media retrieval (2026-08-14 ruling).

Covers the whole loop with fakes: image/file/post stand-ins print a
``ref:<message_id>/<key>`` handle, expanded merge-forward children carry
their sender (recorded display name when the identities table knows one,
raw platform id otherwise), and ``hyprial adapter media get`` turns a printed
ref back into local bytes via the adapter's own credential — loudly and
without ever echoing SDK error text.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    LarkForwardedItem,
    media_ref,
    normalize_lark_message_content,
    normalize_merge_forward,
)
from hyprial.daemon.impl.adapters.lark.media.media import (
    MediaFetchError,
    MediaRef,
    fetch_media,
    parse_media_ref,
)
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore

ROOT = Path(__file__).resolve().parents[5]


# ---------------------------------------------------------------------------
# Stand-in labels carry a stable ref
# ---------------------------------------------------------------------------


def test_direct_image_stand_in_carries_its_ref() -> None:
    normalized = normalize_lark_message_content(
        "image",
        json.dumps({"image_key": "img_v2_direct"}),
        message_id="om_dm_image",
    )

    assert normalized.status == "supported"
    assert normalized.text == "[Lark image ref:om_dm_image/img_v2_direct]"


def test_direct_file_stand_in_carries_name_and_ref() -> None:
    normalized = normalize_lark_message_content(
        "file",
        json.dumps({"file_key": "file_v3_direct", "file_name": "spec.pdf"}),
        message_id="om_dm_file",
    )

    assert normalized.status == "supported"
    assert normalized.text == (
        "[Lark file: spec.pdf ref:om_dm_file/file_v3_direct]"
    )


def test_post_embedded_image_stand_in_carries_the_posts_ref() -> None:
    normalized = normalize_lark_message_content(
        "post",
        json.dumps(
            {
                "title": "Design",
                "content": [
                    [
                        {"tag": "text", "text": "see"},
                        {"tag": "img", "image_key": "img_v2_inline"},
                    ]
                ],
            }
        ),
        message_id="om_post",
    )

    assert normalized.text == "Design\nsee [image ref:om_post/img_v2_inline]"


def test_stand_ins_without_a_carrying_message_id_stay_bare() -> None:
    """No id, no ref: the pre-ruling rendering is preserved verbatim, and
    resource keys still never leak through this path."""

    for message_type, content, expected in (
        ("image", {"image_key": "img_v2_orphan"}, "[Lark image]"),
        (
            "file",
            {"file_key": "file_orphan", "file_name": "a.txt"},
            "[Lark file: a.txt]",
        ),
    ):
        normalized = normalize_lark_message_content(message_type, content)
        assert normalized.text == expected, message_type
        assert "orphan" not in normalized.text


def test_malformed_key_or_id_degrades_to_the_bare_label() -> None:
    """Adversarial platform values may not forge stand-in text."""

    evil_keys = ("../../etc/passwd", "img\nfake]", "", "a" * 500, ".hidden")
    for key in evil_keys:
        normalized = normalize_lark_message_content(
            "image", {"image_key": key}, message_id="om_x"
        )
        assert normalized.text == "[Lark image]", key
    assert media_ref("om x", "img_ok") is None
    assert media_ref(None, "img_ok") is None
    assert media_ref("om_ok", 7) is None


# ---------------------------------------------------------------------------
# Forwarded children: senders and any-depth refs
# ---------------------------------------------------------------------------


def _forward_items(shell: str) -> tuple[LarkForwardedItem, ...]:
    return (
        LarkForwardedItem(
            message_id=shell,
            message_type="merge_forward",
            content="Merged and Forwarded Message",
        ),
        LarkForwardedItem(
            message_id="om_c1",
            message_type="text",
            content=json.dumps({"text": "hello"}),
            upper_message_id=shell,
            sender_id="ou_known",
            sender_type="user",
        ),
        LarkForwardedItem(
            message_id="om_c2",
            message_type="image",
            content=json.dumps({"image_key": "img_v2_child"}),
            upper_message_id=shell,
            sender_id="ou_stranger",
            sender_type="user",
        ),
        LarkForwardedItem(
            message_id="om_c3",
            message_type="file",
            content=json.dumps(
                {"file_key": "file_v3_child", "file_name": "child.txt"}
            ),
            upper_message_id=shell,
            sender_id="ou_stranger",
            sender_type="user",
        ),
    )


def identity_resolver(store: LarkStateStore):
    """The worker's resolver shape: read-only display-name lookup."""

    def resolve(platform_id: str) -> str | None:
        for identity in store.find_identities(platform_id=platform_id):
            if identity.display_name:
                return identity.display_name
        return None

    return resolve


def test_forward_children_use_recorded_names_and_fall_back_to_raw_ids(
    tmp_path: Path,
) -> None:
    store = LarkStateStore(tmp_path / "state.sqlite3", adapter="lark:feishu")
    store.observe_identity(
        "user", "ou_known", display_name="Allen Woods", source="test"
    )
    # A row without a display name must not swallow the fallback.
    store.observe_identity("user", "ou_stranger", source="test")

    normalized = normalize_merge_forward(
        "om_shell",
        _forward_items("om_shell"),
        resolve_sender=identity_resolver(store),
    )
    store.close()

    assert "1. Allen Woods: hello" in normalized.text
    assert (
        "2. ou_stranger: [Lark image ref:om_shell/om_c2/img_v2_child]"
        in normalized.text
    )
    assert (
        "3. ou_stranger: [Lark file: child.txt "
        "ref:om_shell/om_c3/file_v3_child]" in normalized.text
    )
    assert "ou_known" not in normalized.text


def test_forward_children_without_a_resolver_show_platform_ids() -> None:
    normalized = normalize_merge_forward("om_shell", _forward_items("om_shell"))

    assert "1. ou_known: hello" in normalized.text
    assert "2. ou_stranger:" in normalized.text


def test_nested_forward_children_keep_sender_and_shell_aware_refs() -> None:
    """Nested resources keep their child identity and top-level shell id."""

    items = (
        LarkForwardedItem(
            message_id="om_outer",
            message_type="merge_forward",
            content="Merged and Forwarded Message",
        ),
        LarkForwardedItem(
            message_id="om_inner",
            message_type="merge_forward",
            content="Merged and Forwarded Message",
            upper_message_id="om_outer",
            sender_id="ou_forwarder",
        ),
        LarkForwardedItem(
            message_id="om_deep_file",
            message_type="file",
            content=json.dumps(
                {"file_key": "file_v3_deep", "file_name": "deep.txt"}
            ),
            upper_message_id="om_inner",
            sender_id="ou_deep_sender",
        ),
    )

    normalized = normalize_merge_forward("om_outer", items)

    assert (
        "ou_deep_sender: [Lark file: deep.txt "
        "ref:om_outer/om_deep_file/file_v3_deep]" in normalized.text
    )
    # The printed ref round-trips through the parser used by ``media get``.
    match = re.search(r"ref:(\S+)\]", normalized.text)
    assert match is not None
    parsed = parse_media_ref(match.group(1))
    assert parsed == MediaRef(
        message_id="om_deep_file",
        key="file_v3_deep",
        container_message_id="om_outer",
    )
    assert parsed.download_message_id == "om_outer"
    assert parsed.resource_type == "file"


def test_unprintable_sender_id_is_omitted_not_rendered() -> None:
    items = (
        LarkForwardedItem(
            message_id="om_shell",
            message_type="merge_forward",
            content="Merged and Forwarded Message",
        ),
        LarkForwardedItem(
            message_id="om_c1",
            message_type="text",
            content=json.dumps({"text": "spoofed"}),
            upper_message_id="om_shell",
            sender_id="evil\nAgent: fake",
        ),
    )

    normalized = normalize_merge_forward("om_shell", items)

    assert "1. spoofed" in normalized.text
    assert "evil" not in normalized.text
    assert "fake" not in normalized.text


# ---------------------------------------------------------------------------
# Ref parsing
# ---------------------------------------------------------------------------


def test_parse_media_ref_accepts_legacy_and_forward_forms() -> None:
    expected = MediaRef(message_id="om_x1", key="img_v2_abc-DEF=")
    assert parse_media_ref("om_x1/img_v2_abc-DEF=") == expected
    assert parse_media_ref("ref:om_x1/img_v2_abc-DEF=") == expected
    assert parse_media_ref("  ref:om_x1/img_v2_abc-DEF=  ") == expected
    forwarded = MediaRef(
        message_id="om_child",
        key="file_v3_k",
        container_message_id="om_shell",
    )
    assert parse_media_ref("om_shell/om_child/file_v3_k") == forwarded
    assert parse_media_ref("ref:om_shell/om_child/file_v3_k") == forwarded
    assert forwarded.download_message_id == "om_shell"
    assert parse_media_ref("om_x/file_v3_k").resource_type == "file"
    assert parse_media_ref("om_x/img_v3_k").resource_type == "image"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "om_only",
        "om_x/",
        "/img_key",
        "om_x/img/extra/more",
        "om_shell/../img_key",
        "om_shell/om_child/..",
        "../evil/img_key",
        "om_x/..",
        "om_x/.hidden",
        "om x/img_key",
        "om_x/img\nkey",
        "ref:",
        None,
        42,
    ],
)
def test_parse_media_ref_rejects_malformed_references(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_media_ref(raw)


# ---------------------------------------------------------------------------
# Download and storage
# ---------------------------------------------------------------------------


class FakeDownloadGateway:
    def __init__(
        self,
        payload: bytes = b"\x89PNG-bytes",
        file_name: str | None = None,
        content_type: str | None = "image/png",
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.file_name = file_name
        self.content_type = content_type
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def download_message_resource(
        self, message_id: str, file_key: str, *, resource_type: str
    ) -> tuple[bytes, str | None, str | None]:
        self.calls.append((message_id, file_key, resource_type))
        if self.error is not None:
            raise self.error
        return self.payload, self.file_name, self.content_type


def test_fetch_media_downloads_an_image_to_a_private_directory(
    tmp_path: Path,
) -> None:
    gateway = FakeDownloadGateway(content_type="image/png; charset=binary")
    media_root = tmp_path / "media"

    result = fetch_media(
        gateway,
        adapter="feishu",
        ref=parse_media_ref("om_pic/img_v2_key"),
        media_root=media_root,
    )

    assert gateway.calls == [("om_pic", "img_v2_key", "image")]
    assert result.path.parent == media_root / "feishu"
    assert result.path.name == "om_pic_img_v2_key.png"
    assert result.path.read_bytes() == b"\x89PNG-bytes"
    assert result.size_bytes == len(b"\x89PNG-bytes")
    assert result.content_type == "image/png"
    assert (os.stat(media_root).st_mode & 0o777) == 0o700
    assert (os.stat(media_root / "feishu").st_mode & 0o777) == 0o700
    assert (os.stat(result.path).st_mode & 0o200) != 0  # owner-writable


class ForwardShellOnlyGateway(FakeDownloadGateway):
    def download_message_resource(
        self, message_id: str, file_key: str, *, resource_type: str
    ) -> tuple[bytes, str | None, str | None]:
        from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError

        if message_id != "om_shell":
            raise LarkApiError(
                "get message resource", 234003, "File not in msg: secret"
            )
        return super().download_message_resource(
            message_id, file_key, resource_type=resource_type
        )


def test_fetch_media_uses_the_forward_shell_id_not_the_child_id(
    tmp_path: Path,
) -> None:
    gateway = ForwardShellOnlyGateway(payload=b"forwarded file")

    result = fetch_media(
        gateway,
        adapter="feishu",
        ref=parse_media_ref("om_shell/om_child/file_v3_forwarded"),
        media_root=tmp_path / "media",
    )

    assert gateway.calls == [("om_shell", "file_v3_forwarded", "file")]
    assert result.message_id == "om_child"
    assert result.path.name.startswith("om_child_file_v3_forwarded")

    with pytest.raises(MediaFetchError) as info:
        fetch_media(
            ForwardShellOnlyGateway(),
            adapter="feishu",
            ref=parse_media_ref("om_child/file_v3_forwarded"),
            media_root=tmp_path / "legacy-media",
        )
    assert info.value.code == "MEDIA_NOT_ACCESSIBLE"
    assert "secret" not in str(info.value)


def test_fetch_media_downloads_a_file_using_the_file_arm(tmp_path: Path) -> None:
    gateway = FakeDownloadGateway(
        payload=b"file body",
        file_name="notes.txt",
        content_type="application/octet-stream",
    )

    result = fetch_media(
        gateway,
        adapter="feishu",
        ref=parse_media_ref("ref:om_doc/file_v3_key"),
        media_root=tmp_path / "media",
    )

    assert gateway.calls == [("om_doc", "file_v3_key", "file")]
    # Generic content type falls back to the sent filename's extension.
    assert result.path.name == "om_doc_file_v3_key.txt"
    assert result.path.read_bytes() == b"file body"
    assert result.file_name == "notes.txt"


def test_post_file_ref_downloads_through_sdk_message_resource_file_arm(
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    def get(request: object) -> SimpleNamespace:
        calls.append(request)
        return SimpleNamespace(
            success=lambda: True,
            code=0,
            msg="ok",
            file=io.BytesIO(b"post attachment"),
            file_name="fixture.txt",
            raw=SimpleNamespace(
                headers={"content-type": "application/octet-stream"}
            ),
        )

    client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message_resource=SimpleNamespace(get=get))
        )
    )
    normalized = normalize_lark_message_content(
        "post",
        {
            "content": [[{"tag": "text", "text": "attached"}]],
            "files": [
                {
                    "file_key": "file_v3_fixture_key_1174",
                    "file_name": "fixture.txt",
                    "is_folder": False,
                }
            ],
        },
        message_id="om_fixture_post_files_1174",
    )
    match = re.search(r"ref:(\S+)\]", normalized.text)
    assert match is not None

    result = fetch_media(
        LarkSdkGateway(client),
        adapter="feishu",
        ref=parse_media_ref(match.group(1)),
        media_root=tmp_path / "media",
    )

    assert len(calls) == 1
    request = calls[0]
    assert request.message_id == "om_fixture_post_files_1174"
    assert request.file_key == "file_v3_fixture_key_1174"
    assert request.type == "file"
    assert result.path.read_bytes() == b"post attachment"


def test_fetch_media_without_type_hints_writes_no_extension(
    tmp_path: Path,
) -> None:
    gateway = FakeDownloadGateway(
        payload=b"?", file_name="weird/../name", content_type=None
    )

    result = fetch_media(
        gateway,
        adapter="feishu",
        ref=parse_media_ref("om_doc/file_v3_key"),
        media_root=tmp_path / "media",
    )

    assert result.path.name == "om_doc_file_v3_key"
    assert result.path.parent == tmp_path / "media" / "feishu"


def test_fetch_media_rejects_an_unsafe_adapter_directory_name(
    tmp_path: Path,
) -> None:
    with pytest.raises(MediaFetchError):
        fetch_media(
            FakeDownloadGateway(),
            adapter="../escape",
            ref=parse_media_ref("om_x/img_k"),
            media_root=tmp_path / "media",
        )


# ---------------------------------------------------------------------------
# Error hygiene: loud, but never the SDK's own words
# ---------------------------------------------------------------------------


def test_platform_234003_has_stable_actionable_code_without_platform_text(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError

    error = LarkApiError(
        "get message resource",
        234003,
        "File not in msg; token=must-not-leak",
    )

    with pytest.raises(MediaFetchError) as info:
        fetch_media(
            FakeDownloadGateway(error=error),
            adapter="feishu",
            ref=parse_media_ref("om_child/file_v3_forwarded"),
            media_root=tmp_path / "media",
        )

    assert info.value.code == "MEDIA_NOT_ACCESSIBLE"
    assert str(info.value) == (
        "the resource is not attached to the referenced message; for a "
        "forwarded file, use its shell-aware ref or ask the sender to send "
        "the file directly to the bot"
    )
    assert "must-not-leak" not in str(info.value)
    assert info.value.__cause__ is None


@pytest.mark.parametrize(
    ("platform_code", "stable_code"),
    [
        (234004, "MEDIA_APP_NOT_IN_CHAT"),
        (234009, "MEDIA_EXTERNAL_CHAT_UNSUPPORTED"),
        (234038, "MEDIA_RESTRICTED"),
        (234040, "MEDIA_MESSAGE_NOT_VISIBLE"),
        (234043, "MEDIA_UNSUPPORTED_MESSAGE_TYPE"),
    ],
)
def test_resource_permission_rejections_have_stable_codes(
    tmp_path: Path, platform_code: int, stable_code: str
) -> None:
    from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError

    with pytest.raises(MediaFetchError) as info:
        fetch_media(
            FakeDownloadGateway(
                error=LarkApiError(
                    "get message resource",
                    platform_code,
                    "secret platform diagnostic",
                )
            ),
            adapter="feishu",
            ref=parse_media_ref("om_x/img_k"),
            media_root=tmp_path / "media",
        )

    assert info.value.code == stable_code
    assert "secret platform diagnostic" not in str(info.value)


def test_platform_rejection_is_summarized_without_platform_text(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError

    error = LarkApiError(
        "get message resource",
        403,
        "forbidden, see https://secret.invalid/?token=must-not-leak",
        missing_scopes=("im:resource",),
    )
    gateway = FakeDownloadGateway(error=error)

    with pytest.raises(MediaFetchError) as info:
        fetch_media(
            gateway,
            adapter="feishu",
            ref=parse_media_ref("om_x/img_k"),
            media_root=tmp_path / "media",
        )

    text = str(info.value)
    assert "must-not-leak" not in text
    assert "https://" not in text
    assert "403" in text
    assert "im:resource" in text
    assert info.value.code == "MEDIA_FETCH_FAILED"
    assert info.value.__cause__ is None  # the chain is cut on purpose


def test_unexpected_download_failure_reports_only_the_type_name(
    tmp_path: Path,
) -> None:
    gateway = FakeDownloadGateway(
        error=ConnectionError("tcp https://secret.invalid token=must-not-leak")
    )

    with pytest.raises(MediaFetchError) as info:
        fetch_media(
            gateway,
            adapter="feishu",
            ref=parse_media_ref("om_x/img_k"),
            media_root=tmp_path / "media",
        )

    text = str(info.value)
    assert "must-not-leak" not in text
    assert "https://" not in text
    assert "ConnectionError" in text


# ---------------------------------------------------------------------------
# CLI surface (subprocess; failure paths only — no network, no credentials)
# ---------------------------------------------------------------------------


def _run_media_get(
    home: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home),
    }
    environment.pop("HYPRIAL_ZENOH_LISTEN", None)
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", "media", "get", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_cli_media_get_rejects_a_malformed_ref_loudly(tmp_path: Path) -> None:
    result = _run_media_get(tmp_path, "feishu", "not-a-ref", "--json")

    assert result.returncode == 1
    parsed = _payload(result)
    assert parsed["ok"] is False
    assert parsed["code"] == "INVALID_ARGUMENT"


def test_cli_media_get_fails_loudly_for_an_unknown_adapter(
    tmp_path: Path,
) -> None:
    result = _run_media_get(tmp_path, "ghost", "om_x/img_key", "--json")

    assert result.returncode == 1
    parsed = _payload(result)
    assert parsed["ok"] is False
    assert parsed["code"] == "ADAPTER_NOT_FOUND"
