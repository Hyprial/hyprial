# Decision record (GUI session host, PR 2b):
# - One assets serves one directory: the wheel's verified hyprial/_gui, or an
#   explicit gui/dist that carries no manifest. Verified means the manifest and
#   every file hash were checked at start and only files the manifest names are
#   served, from the bytes read at that moment, so a change after start cannot
#   reach a browser.
# - The path is taken raw and decoded once; control characters, backslashes and
#   dot segments (also after decoding) are a 400, anything that resolves outside
#   the root is a 404. A missing path with a file extension is a 404, one
#   without is the SPA fallback to index.html.
# - A percent escape that is not valid UTF-8 is a 400 like any other malformed
#   target. A name or directory that starts with a dot is never served (404, no
#   SPA fallback), so a stray .env or .git in an explicit root cannot leak.
# - Cache policy: index.html and the fallback are never stored; hashed
#   assets/* are immutable for a year; everything else is not stored.
# - Misses are host envelopes, not bare statuses, so no consumer has to parse
#   two error shapes; the retired /api/gui/v1 501 placeholder is not reproduced.
# - PR 4b-H: the dev-tools/ prefix is served only when the host was started with
#   --dev-tools. Without it every path under the prefix is a 404, in a verified
#   and in an explicit tree alike, and the prefix never falls back to index.html
#   in either state. The test is made on the decoded path and again on the file
#   an explicit root resolved to, so neither another spelling nor a symbolic
#   link inside the root reaches a tool file. A hard link is not covered: a
#   second name outside the prefix for the same inode has no path that says so,
#   and an explicit root is the operator's own directory. The verified tree
#   serves manifest names only, so it has no such second name. read() is the
#   host's own access to the tree (the API docs files) and is not a browser
#   path.
"""Static GUI resources behind the session host."""

from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from hyprial.kernel import ipc_errors

from ...install import resources
from ..envelope import HostFailure, Reply, failure_reply, new_request_id
from .devtools import is_tool_path

IMMUTABLE = "public, max-age=31536000, immutable"
TEXT_TYPES = {"application/javascript", "application/json", "image/svg+xml", "application/manifest+json"}
VERIFIED = "installed"
EXPLICIT = "explicit-unverified"


class AssetRootError(RuntimeError):
    """The asset directory cannot be served safely; the host must not start."""


class AssetTree:
    """Serves index, hashed assets and the SPA fallback from one directory."""

    def __init__(
        self, root: Path, status: str, preloaded: dict[str, bytes] | None, dev_tools: bool = False
    ) -> None:
        self._root = root
        self._status = status
        self._preloaded = preloaded
        self._dev_tools = dev_tools

    @classmethod
    def open(cls, root: Path, *, dev_tools: bool = False) -> "AssetTree":
        """Validate ``root`` once; raise ``AssetRootError`` instead of serving a bad tree."""
        resolved = Path(root).expanduser().resolve()
        if not (resolved / "index.html").is_file():
            raise AssetRootError(f"GUI index.html is missing under {resolved}")
        if not (resolved / resources.MANIFEST_NAME).exists():
            return cls(resolved, EXPLICIT, None, dev_tools)
        try:
            assets = resources.load_assets(resolved, verify=True)
            preloaded = {
                str(entry["path"]): (resolved / str(entry["path"])).read_bytes() for entry in assets.manifest["files"]
            }
        except resources.GuiAssetError as error:
            raise AssetRootError(f"{error.code}: {error}") from error
        except OSError as error:
            raise AssetRootError(f"GUI_ASSETS_MISSING: {error}") from error
        return cls(resolved, VERIFIED, preloaded, dev_tools)

    @property
    def status(self) -> str:
        return self._status

    def health(self) -> dict[str, Any]:
        return {"assets": self._status, "assetsVerified": self._status == VERIFIED}

    def read(self, relative: str) -> bytes | None:
        """The bytes of one file of the tree for the host itself, ``None`` when it has none."""
        found = self._locate(relative)
        return found[1] if found is not None else None

    def serve(self, path: str, *, head: bool) -> Reply:
        request_id = new_request_id()
        try:
            relative = self._relative(path)
        except HostFailure as failure:
            return failure_reply(request_id, failure)
        if any(segment.startswith(".") for segment in relative.split("/")):
            return failure_reply(request_id, HostFailure(404, "NOT_FOUND", "not found"))
        tool = is_tool_path(relative)
        found = None if tool and not self._dev_tools else self._find(relative)
        if found is None:
            if tool or "." in PurePosixPath(relative).name:
                return failure_reply(request_id, HostFailure(404, "NOT_FOUND", "not found"))
            relative, found = "index.html", self._find("index.html")
            if found is None:
                return failure_reply(request_id, HostFailure(404, "NOT_FOUND", "not found"))
        return self._reply(relative, found)

    @staticmethod
    def _relative(path: str) -> str:
        if any(ord(c) < 0x21 or ord(c) == 0x7F for c in path):
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "request target is not a valid origin-form URL")
        try:
            decoded = unquote(path, errors="strict") if "%" in path else path
        except UnicodeDecodeError:
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "request target is not a valid origin-form URL") from None
        segments = decoded.split("/")
        if (
            "\\" in decoded
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in decoded)
            or any(segment in ("..", ".") for segment in segments)
        ):
            raise HostFailure(400, ipc_errors.INVALID_REQUEST, "request target is not a valid origin-form URL")
        return decoded.lstrip("/") or "index.html"

    def _find(self, relative: str) -> bytes | None:
        found = self._locate(relative)
        if found is None or (not self._dev_tools and is_tool_path(found[0])):
            return None
        return found[1]

    def _locate(self, relative: str) -> tuple[str, bytes] | None:
        """The path a name resolved to inside the root, and its bytes."""
        if self._preloaded is not None:
            body = self._preloaded.get(relative)
            return (relative, body) if body is not None else None
        candidate = (self._root / relative).resolve()
        if self._root not in candidate.parents or not candidate.is_file():
            return None
        try:
            return candidate.relative_to(self._root).as_posix(), candidate.read_bytes()
        except OSError:
            return None

    @staticmethod
    def _reply(relative: str, body: bytes) -> Reply:
        name = PurePosixPath(relative).name
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if media.startswith("text/") or media in TEXT_TYPES:
            media += "; charset=utf-8"
        immutable = relative.startswith("assets/") and name != "index.html"
        headers = (
            ("content-type", media),
            ("x-content-type-options", "nosniff"),
            ("cache-control", IMMUTABLE if immutable else "no-store"),
            ("content-length", str(len(body))),
        )
        return Reply(200, headers, body)
