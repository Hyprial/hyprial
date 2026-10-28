"""Directed media retrieval for Lark adapters (2026-08-14 ruling).

Inbound stand-ins print a ``ref:<message_id>/<key>`` handle for direct media
and a ``ref:<shell_id>/<child_id>/<key>`` handle for forwarded media.  This
module turns either handle back into local bytes *on demand*:
the agent (or an operator) asks, the adapter's own App credential fetches
through ``im/v1`` message resources, and the payload lands under
``<hyprial-home>/media/<adapter>/``.  Nothing is ever inlined automatically —
media stays a directed pull, so a chat full of screenshots cannot flood
Harness text with bytes nobody asked for.

Credential domain isolation matters here exactly as it does for identity
sync: a message resource is only downloadable by the App whose adapter saw
the message, so the fetch always runs on that adapter's own credential.

Error hygiene: SDK/platform error text can carry URLs and tokens.  Every
failure this module reports is a summary (operation, code, exception type
name) and never the platform's own message.
"""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from hyprial.daemon.impl.adapters.lark.contracts.messages import media_ref
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError

#: An adapter name doubles as a storage directory component here, so it is
#: held to the same conservative shape as the ref parts.
_SAFE_ADAPTER_NAME = re.compile(r"(?=[^.])[A-Za-z0-9._-]{1,64}")
#: A replayed ``Content-Disposition`` suffix must look like a plain extension
#: before it may name a local file.
_SAFE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}")
#: Content types too generic to imply an extension.
_GENERIC_CONTENT_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream"}
)


class MediaFetchError(RuntimeError):
    """A loud, stable, secret-free media retrieval failure."""

    def __init__(
        self, message: str, *, code: str = "MEDIA_FETCH_FAILED"
    ) -> None:
        super().__init__(message)
        self.code = code


_RESOURCE_ERROR_SUMMARIES: dict[int, tuple[str, str]] = {
    234003: (
        "MEDIA_NOT_ACCESSIBLE",
        "the resource is not attached to the referenced message; for a "
        "forwarded file, use its shell-aware ref or ask the sender to send "
        "the file directly to the bot",
    ),
    234004: (
        "MEDIA_APP_NOT_IN_CHAT",
        "this App is not in the chat that carries the resource; add the bot "
        "to that chat or ask the sender to send the file directly",
    ),
    234009: (
        "MEDIA_EXTERNAL_CHAT_UNSUPPORTED",
        "the platform does not allow this resource download in an external chat",
    ),
    234038: (
        "MEDIA_RESTRICTED",
        "restricted-message mode prevents this resource download",
    ),
    234040: (
        "MEDIA_MESSAGE_NOT_VISIBLE",
        "the carrying message is not visible to this App; ask a group "
        "administrator to check history visibility",
    ),
    234043: (
        "MEDIA_UNSUPPORTED_MESSAGE_TYPE",
        "the platform does not support resource downloads for this message type",
    ),
}


@runtime_checkable
class MediaDownloadPort(Protocol):
    """What retrieval needs from the platform gateway."""

    def download_message_resource(
        self, message_id: str, file_key: str, *, resource_type: str
    ) -> tuple[bytes, str | None, str | None]: ...


@dataclass(frozen=True, slots=True)
class MediaRef:
    """One parsed direct or shell-aware media reference.

    ``message_id`` identifies the message whose content exposed the key.
    Forwarded children additionally carry ``container_message_id`` because
    Lark's resource endpoint addresses their bytes through the top-level
    merge-forward shell received by the App.
    """

    message_id: str
    key: str
    container_message_id: str | None = None

    @property
    def download_message_id(self) -> str:
        """Message id required by the platform resource endpoint."""

        return self.container_message_id or self.message_id

    @property
    def resource_type(self) -> str:
        """The platform's ``image``/``file`` discriminator for this key.

        Image keys are ``img_*`` in every observed generation (``img_v2``,
        ``img_v3``); everything else — file keys and rich-media covers —
        downloads through the ``file`` arm.
        """

        return "image" if self.key.startswith("img") else "file"


def parse_media_ref(raw: object) -> MediaRef:
    """Parse a reference exactly as printed inside a stand-in label.

    Accepts legacy ``om_xxx/<key>`` and forwarded
    ``om_shell/om_child/<key>`` forms. A leading ``ref:`` is tolerated so the
    label can be copied verbatim. Empty/non-identifier parts and path tricks
    fail loudly.
    """

    if not isinstance(raw, str):
        raise ValueError("media ref must be a string")
    value = raw.strip().removeprefix("ref:")
    parts = value.split("/")
    if len(parts) == 2:
        message_id, key = parts
        container_message_id = None
    elif len(parts) == 3:
        container_message_id, message_id, key = parts
    else:
        container_message_id = message_id = key = ""
    if media_ref(
        message_id,
        key,
        container_message_id=container_message_id,
    ) != value:
        raise ValueError(
            "media ref must be <message_id>/<key> or "
            "<shell_id>/<child_id>/<key> as printed in the message label "
            "(optionally prefixed with 'ref:')"
        )
    return MediaRef(
        message_id=message_id,
        key=key,
        container_message_id=container_message_id,
    )


@dataclass(frozen=True, slots=True)
class MediaFetchResult:
    adapter: str
    message_id: str
    key: str
    resource_type: str
    path: Path
    size_bytes: int
    content_type: str | None = None
    file_name: str | None = None


def _base_content_type(content_type: str | None) -> str | None:
    if not isinstance(content_type, str):
        return None
    base = content_type.split(";", 1)[0].strip().lower()
    return base or None


def _extension_for(content_type: str | None, file_name: str | None) -> str:
    """Choose a filename extension: content type first, then the sent name."""

    base = _base_content_type(content_type)
    if base and base not in _GENERIC_CONTENT_TYPES:
        guessed = mimetypes.guess_extension(base)
        if guessed:
            return guessed
    if isinstance(file_name, str):
        suffix = Path(file_name).suffix
        if _SAFE_EXTENSION.fullmatch(suffix):
            return suffix
    return ""


def fetch_media(
    gateway: MediaDownloadPort,
    *,
    adapter: str,
    ref: MediaRef,
    media_root: Path,
) -> MediaFetchResult:
    """Download one referenced media resource into the local media store.

    The file is written to ``<media_root>/<adapter>/`` (both directories
    held at mode 0700 — downloaded media is private material) under a name
    that carries the message id and key, so a directory listing alone maps
    every artifact back to its source message.
    """

    if not _SAFE_ADAPTER_NAME.fullmatch(adapter):
        raise MediaFetchError(
            "adapter name is not usable as a media directory component"
        )
    resource_type = ref.resource_type
    try:
        payload, file_name, content_type = gateway.download_message_resource(
            ref.download_message_id, ref.key, resource_type=resource_type
        )
    except LarkApiError as error:
        # ``from None`` on purpose: the platform's own message may carry
        # URLs/tokens and must not survive into any later rendering of the
        # exception chain.  The summary keeps what an operator can act on.
        mapped = _RESOURCE_ERROR_SUMMARIES.get(error.code)
        if mapped is not None:
            code, summary = mapped
            raise MediaFetchError(summary, code=code) from None
        scopes = (
            f"; missing scopes: {', '.join(error.missing_scopes)}"
            if error.missing_scopes
            else ""
        )
        raise MediaFetchError(
            f"platform refused the download "
            f"(operation={error.operation}, code={error.code}{scopes})"
        ) from None
    except Exception as error:  # noqa: BLE001 - sanitizing report boundary
        # Deliberately wide, never silent: every failure re-raises loudly
        # with the exception's type name (NameError/ImportError included),
        # only the SDK's message text is withheld.
        raise MediaFetchError(
            f"media download failed ({type(error).__name__})"
        ) from None
    directory = media_root / adapter
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(media_root, 0o700)
    os.chmod(directory, 0o700)
    extension = _extension_for(content_type, file_name)
    target = directory / f"{ref.message_id}_{ref.key}{extension}"
    descriptor = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    return MediaFetchResult(
        adapter=adapter,
        message_id=ref.message_id,
        key=ref.key,
        resource_type=resource_type,
        path=target,
        size_bytes=len(payload),
        content_type=_base_content_type(content_type),
        file_name=file_name,
    )
