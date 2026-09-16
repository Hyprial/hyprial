"""Where the Lark adapter's traffic goes.

Three separate constructors each decide, independently, which host this
process talks to:

* :class:`~hyprial.adapters.lark.sdk.LarkSdkGateway` -- every REST call, via the
  official SDK's ``domain``.
* ``LarkEventStream``'s websocket client -- both the HTTP endpoint-discovery
  request that precedes each handshake *and* the long connection itself.
* :class:`~hyprial.adapters.lark.scopes.LarkScopeClient` -- the permission/
  authorization v6 calls, via its own ``origin``.

They are listed here rather than left to each call site because a partial
override is worse than none: a run that redirected two of the three would
report itself as pointed somewhere local while one code path still reached a
production App.  One function, one environment key, three uses.

The default is the vendor host this adapter has always used, so an
unconfigured process is byte-identical to the one before this seam existed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Lark's own default.  ``lark_oapi`` spells it ``lark.FEISHU_DOMAIN``; it is
#: repeated as a literal so this module states its default without importing
#: the SDK, which callers outside the adapter (tests, scenario runners) should
#: not have to install to ask where traffic would go.
FEISHU_BASE_URL = "https://open.feishu.cn"

LARK_BASE_URL_ENV = "HYPRIAL_LARK_BASE_URL"


def lark_base_url(environment: Mapping[str, str] | None = None) -> str:
    """Base URL for Lark's OpenAPI in ``environment`` (vendor host by default).

    Trailing slashes are stripped because both consumers concatenate a path
    onto this value directly -- the SDK's ``_get_conn_url`` builds
    ``domain + "/callback/ws/endpoint"`` -- and ``//callback`` is a different
    route on some servers.
    """

    source = os.environ if environment is None else environment
    configured = source.get(LARK_BASE_URL_ENV, "").strip()
    return (configured or FEISHU_BASE_URL).rstrip("/")


def is_production_lark(base_url: str) -> bool:
    """Whether ``base_url`` names Lark's own service rather than a stand-in.

    Exists so a caller that must not reach production can assert on the
    resolved value.  Asking the question this way -- "is this the real one?"
    -- rather than "does this look like my fake?" keeps a typo'd override
    (which resolves to neither) from reading as safe.
    """

    return base_url.rstrip("/") in {FEISHU_BASE_URL, "https://open.larksuite.com"}
