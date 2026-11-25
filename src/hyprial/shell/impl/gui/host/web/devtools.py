# Decision record (developer tools on the session host, PR 4b-H, 2026-11-16):
# - Everything here exists only behind the host's --dev-tools argument. Without
#   it the host builds no DevTools record: the dev-tools/ prefix of the asset
#   tree, /docs-assets/* and the two developer /daemon routes are 404 and /docs
#   is whatever the page source answers (the single-page fallback).
# - The prefix test is made on the decoded relative path and compares the first
#   segment case-folded: an explicit root on a case-insensitive file system
#   would otherwise serve DEV-TOOLS/x for a gate that only knows dev-tools/x.
# - The API docs page is a second document on the host origin. Cookies are
#   Path=/daemon, so the page itself cannot be cookie-gated; it is a static
#   shell with no schema, no session value and no credential. Its script reads
#   the per-view proof the opener tab left in sessionStorage, asks
#   POST /daemon/session/attach for the live generation and CSRF value, and
#   loads the schema from GET /daemon/openapi.json. A tab that has no proof, or
#   whose attach is refused, is sent to / and shows no explanation. The proof
#   is already in this tab's own sessionStorage when the script starts, so the
#   script drops window.opener first: Swagger UI is third-party code and gets
#   no handle on the main tab.
# - The script attaches again before every "Try it out" call instead of
#   retrying after a refusal: a refresh in the main tab rotates the generation,
#   and attach is a read of the live values that rotates nothing.
# - The upstream bearer is not known to this module. The script deletes any
#   Authorization header Swagger UI would add.
# - Swagger UI runs only in this document, under a policy that allows scripts
#   and connections from this origin alone and forbids framing. Its two files
#   are read once at start, from the verified tree when there is one, so a
#   change on disk after start cannot reach a browser.
# - The docs page exists only when its files were found and the driver is the
#   daemon (apiDocs): over the Contract Mock there is no schema to show, the
#   session says apiDocs false, and the docs paths behave as without the page.
"""Developer tools behind ``--dev-tools``: the gated prefix and the API docs page."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..envelope import Reply

TOOL_PREFIX = "dev-tools"
DOCS_TREE_DIRECTORY = f"{TOOL_PREFIX}/docs"
DOCS_PATH = "/docs"
DOCS_ASSETS_PREFIX = "/docs-assets/"
ATTACH_PATH = "/daemon/session/attach"
SCHEMA_PATH = "/daemon/openapi.json"
VIEW_STORAGE_KEY = "hyprial.gui.session-view"
SCRIPT_NAME = "hyprial-docs.js"
BUNDLE_NAME = "swagger-ui-bundle.js"
STYLE_NAME = "swagger-ui.css"
DOCS_FILES = (BUNDLE_NAME, STYLE_NAME)
DOCS_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_MEDIA = {
    SCRIPT_NAME: "text/javascript; charset=utf-8",
    BUNDLE_NAME: "text/javascript; charset=utf-8",
    STYLE_NAME: "text/css; charset=utf-8",
}

_PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hyprial API</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="{DOCS_ASSETS_PREFIX}{STYLE_NAME}">
</head>
<body>
<main id="swagger-ui"></main>
<script src="{DOCS_ASSETS_PREFIX}{BUNDLE_NAME}" defer></script>
<script src="{DOCS_ASSETS_PREFIX}{SCRIPT_NAME}" defer></script>
</body>
</html>
""".encode("utf-8")

_SCRIPT = (
    """(function () {
  "use strict";
  try {
    window.opener = null;
  } catch (_error) {
    void _error;
  }
  var view = null;
  try {
    view = window.sessionStorage.getItem("__VIEW_KEY__");
  } catch (_error) {
    view = null;
  }
  function leave() {
    window.location.replace("/");
  }
  if (!view) {
    leave();
    return;
  }
  function attach() {
    return fetch("__ATTACH__", {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-hyprial-view": view
      },
      body: "{}",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error"
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("attach refused");
      }
      return response.json();
    });
  }
  function withSession(headers, live) {
    headers["x-hyprial-view"] = view;
    headers["x-hyprial-generation"] = live.generation;
    headers["x-hyprial-csrf"] = live.csrf;
    delete headers.Authorization;
    delete headers.authorization;
    return headers;
  }
  attach().then(function (live) {
    return fetch("__SCHEMA__", {
      method: "GET",
      headers: withSession({ "Accept": "application/json" }, live),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error"
    });
  }).then(function (response) {
    if (!response.ok) {
      throw new Error("schema refused");
    }
    return response.json();
  }).then(function (schema) {
    window.SwaggerUIBundle({
      spec: schema,
      dom_id: "#swagger-ui",
      deepLinking: false,
      displayRequestDuration: true,
      persistAuthorization: false,
      filter: true,
      requestInterceptor: function (request) {
        return attach().then(function (live) {
          request.headers = withSession(request.headers || {}, live);
          request.credentials = "same-origin";
          return request;
        });
      }
    });
  }).catch(leave);
})();
""".replace("__VIEW_KEY__", VIEW_STORAGE_KEY)
    .replace("__ATTACH__", ATTACH_PATH)
    .replace("__SCHEMA__", SCHEMA_PATH)
    .encode("utf-8")
)


class DocsAssetsError(RuntimeError):
    """An explicit docs directory cannot be served; the host must not start."""


def is_tool_path(relative: str) -> bool:
    """Whether a decoded path relative to the root lies under the gated tool prefix."""
    return relative.lstrip("/").split("/", 1)[0].casefold() == TOOL_PREFIX


def collect_docs(read: Callable[[str], bytes | None]) -> dict[str, bytes] | None:
    """The Swagger UI files by name, or ``None`` unless every one of them was found."""
    found: dict[str, bytes] = {}
    for name in DOCS_FILES:
        body = read(name)
        if body is None:
            return None
        found[name] = body
    return found


def read_docs_directory(directory: Path) -> dict[str, bytes]:
    """Read an explicit docs directory once; raise instead of starting without its files."""
    resolved = Path(directory).expanduser().resolve()
    if not resolved.is_dir():
        raise DocsAssetsError(f"the API docs directory is missing: {resolved}")

    def read(name: str) -> bytes | None:
        try:
            return (resolved / name).read_bytes()
        except OSError:
            return None

    found = collect_docs(read)
    if found is None:
        raise DocsAssetsError(f"the API docs directory does not hold {' and '.join(DOCS_FILES)}: {resolved}")
    return found


def _reply(media: str, body: bytes, extra: tuple[tuple[str, str], ...] = ()) -> Reply:
    return Reply(
        200,
        (
            ("content-type", media),
            ("x-content-type-options", "nosniff"),
            ("cache-control", "no-store"),
            *extra,
            ("content-length", str(len(body))),
        ),
        body,
    )


@dataclass(frozen=True)
class DevTools:
    """What ``--dev-tools`` turned on for this host: the prefix, and the docs files if any."""

    docs: Mapping[str, bytes] | None = None

    def api_docs(self, driver: str) -> bool:
        return self.docs is not None and driver == "daemon"

    def capability(self, driver: str) -> dict[str, bool]:
        """The ``devTools`` value of a session response."""
        return {"apiDocs": self.api_docs(driver)}

    def page(self) -> Reply:
        return _reply(
            "text/html; charset=utf-8",
            _PAGE,
            (
                ("content-security-policy", DOCS_CSP),
                ("x-frame-options", "DENY"),
                ("referrer-policy", "no-referrer"),
            ),
        )

    def asset(self, name: str) -> Reply | None:
        """The docs script or one Swagger UI file; ``None`` for any other name."""
        if name == SCRIPT_NAME:
            return _reply(_MEDIA[name], _SCRIPT)
        body = self.docs.get(name) if self.docs is not None else None
        if body is None or name not in _MEDIA:
            return None
        return _reply(_MEDIA[name], body)
