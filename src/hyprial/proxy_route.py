"""One rule for how the CLI's own urllib traffic reaches the internet.

urllib reads ``ALL_PROXY`` into ``getproxies()`` as an ``all`` entry that
``ProxyHandler`` never applies to http or https, so a shell exporting only
``ALL_PROXY`` sent the CLI's requests DIRECT -- while curl, which honours
``ALL_PROXY``, went through the proxy.  Observed on a member's macOS host
during the self-hosted headscale cutover: the sidecar download failed with
Python "SSL: UNEXPECTED_EOF" and the login poll to the issuer kept being
reset, both on the direct route curl never took.

The rule is the workers' one (:func:`hyprial.agents.environment.
derived_proxy_environment`): an explicit per-scheme proxy wins; otherwise an
http(s) ``ALL_PROXY`` covers both schemes.  Only that derived case needs its
own opener (it also proxies redirects).  Every other case keeps urllib's
default opener, unchanged.  urllib cannot speak SOCKS, so a SOCKS
``ALL_PROXY`` is named in the route note instead of being silently ignored.
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from hyprial.agents.environment import derived_proxy_environment

__all__ = ["proxy_opener", "url_opener"]


def proxy_opener(
    environ: Mapping[str, str] | None = None,
) -> tuple[urllib.request.OpenerDirector | None, str]:
    """A custom opener only where urllib gets the route wrong, plus a note.

    Returns ``(None, note)`` when urllib's default opener already routes
    correctly; the note says which route a request takes, for error text.
    """

    env = os.environ if environ is None else environ
    derived = derived_proxy_environment(env)
    if derived:
        proxy = derived["HTTPS_PROXY"]
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler), f"via ALL_PROXY {proxy}"
    explicit = (
        env.get("HTTPS_PROXY") or env.get("https_proxy")
        or env.get("HTTP_PROXY") or env.get("http_proxy")
    )
    if explicit:
        return None, f"via proxy {explicit}"
    catch_all = env.get("ALL_PROXY") or env.get("all_proxy")
    if catch_all:
        return None, (
            f"direct: ALL_PROXY={catch_all} is not an http(s) proxy and cannot "
            "be used here; set HTTPS_PROXY to an HTTP proxy"
        )
    return None, "platform default proxy settings"


def url_opener(
    environ: Mapping[str, str] | None = None,
) -> tuple[Callable[..., Any], str]:
    """``(open, note)``: ``urllib.request.urlopen`` unless a route is needed."""

    opener, note = proxy_opener(environ)
    return (opener.open if opener is not None else urllib.request.urlopen), note
