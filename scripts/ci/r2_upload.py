"""Small stdlib S3 SigV4 transport shared by the two R2 publication lanes.

Only the CLI wrappers read environment credentials. Requests never redirect;
logs never contain authorization headers, endpoint secrets or response bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.client import HTTPException
from pathlib import PurePosixPath
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

CACHE_CONTROL = "max-age=60, must-revalidate"
PUBLIC_BASE = "https://dl.hyprial.com"
# Cloudflare answers urllib's default "Python-urllib/x.y" UA with 403 (error 1010).
READ_BACK_USER_AGENT = "hyprial-release-verify/1"
SECRET_NAMES = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


@dataclass(frozen=True)
class Object:
    key: str
    body: bytes
    content_type: str


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signed_request(
    method: str, url: str, body: bytes, content_type: str,
    access_key: str, secret_key: str, now: datetime | None = None,
) -> Request:
    """S3 SigV4: encoded path, sorted encoded query, region auto, service s3."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]
    parsed = urlsplit(url)
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": parsed.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp}
    if method == "PUT":
        headers.update({"cache-control": CACHE_CONTROL, "content-type": content_type})
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    query = "&".join(f"{key}={value}" for key, value in sorted(
        (quote(key, safe="~"), quote(value, safe="~"))
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)))
    canonical = "\n".join((method, parsed.path or "/", query, canonical_headers,
                            signed_headers, payload_hash))
    scope = f"{date}/auto/s3/aws4_request"
    to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, scope,
                          hashlib.sha256(canonical.encode()).hexdigest()))
    key = ("AWS4" + secret_key).encode()
    for part in (date, "auto", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return Request(url, data=body if method == "PUT" else None, headers=headers, method=method)


def signed_put(url, body, content_type, access_key, secret_key, now=None) -> Request:
    return signed_request("PUT", url, body, content_type, access_key, secret_key, now)


def transfer_timeout(method: str, body_size: int) -> float:
    """Socket timeout: 15s base + PUT bytes at 128 KiB/s, capped at 300s.

    urllib uses one timeout for connect and blocking socket operations, not a
    wall-clock deadline. The CI job timeout remains the overall time bound.
    GETs (including small listings) retain the short 15s socket timeout.
    """
    return min(300, 15 + body_size / (128 * 1024)) if method == "PUT" else 15


def network_failure(error: Exception) -> str:
    """Only type names and numeric errno are safe; never format reason text."""
    detail = type(error).__name__
    cause = error
    if isinstance(error, URLError):
        cause = error.reason
        detail += f" reason {type(cause).__name__}"
    errno = getattr(cause, "errno", None)
    if type(errno) is int:
        detail += f" errno {errno}"
    return f"network error ({detail})"


def transfer(request: Request, *, expected: bytes | None = None, limit: int = 4 * 1024 * 1024,
             before_attempt: Callable[[], None] | None = None) -> bytes:
    """Three bounded attempts; anonymous verification lets a 60s cache expire."""
    opener = build_opener(NoRedirect())
    failure = "request failed"
    timeout = transfer_timeout(request.method, len(request.data or b""))
    for attempt in range(3):
        if attempt:
            time.sleep((10, 60)[attempt - 1])
        if before_attempt is not None:
            before_attempt()
        try:
            with opener.open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    failure = f"HTTP {response.status}"
                    continue
                data = response.read((len(expected) if expected is not None else limit) + 1)
                if expected is not None:
                    if data != expected:
                        failure = "Anonymous read-back mismatch"
                        continue
                elif len(data) > limit:
                    failure = "Response exceeds size limit"
                    continue
                return data
        except HTTPError as error:
            failure = f"HTTP {error.code}"
            error.close()
        except (URLError, OSError, HTTPException) as error:
            failure = network_failure(error)
    # Never interpolate urllib exceptions: they can include sensitive URLs.
    name = PurePosixPath(urlsplit(request.full_url).path).name
    raise RuntimeError(f"{failure}: {request.method} {name}")


class R2:
    def __init__(self, env: dict[str, str], *, endpoint: str | None = None):
        if any(not env.get(name, "").strip() for name in SECRET_NAMES):
            raise RuntimeError("R2 credentials not configured")
        configured, self.access, self.secret, bucket = (env[name] for name in SECRET_NAMES)
        if not re.fullmatch(r"https://[0-9a-fA-F]{32}\.r2\.cloudflarestorage\.com/?", configured):
            raise RuntimeError("R2_ENDPOINT must be the HTTPS account R2 endpoint")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise RuntimeError("R2_BUCKET is not a valid bucket name")
        # Python-only injection for loopback fixtures; no CLI/environment override.
        self.base = f"{(endpoint or configured).rstrip('/')}/{quote(bucket, safe='')}"
        #: 每次 `snapshot()` 调用一行，记录**该次**是否真的把 prefix 写进 LIST 请求。
        #: 刻意用 list 而不是 set: set 会把"两次快照都带 prefix"与"只带了一次"合并成同一个显示，
        #: 于是"prefix 生效"这句话就分不出 1/2 —— 而这正是本字段要证明的事。
        self.snapshot_prefix_calls: list[str | None] = []

    def put(self, obj: Object, *, before_attempt: Callable[[], None] | None = None) -> None:
        url = f"{self.base}/{quote(obj.key, safe='/~')}"
        transfer(signed_put(url, obj.body, obj.content_type, self.access, self.secret),
                 before_attempt=before_attempt)
        print(f"Uploaded {obj.key}")

    def snapshot(self, prefix: str | None = None) -> dict[str, tuple[str, int]]:
        """Paginated key -> (ETag, size); malformed pages fail closed.

        `prefix` 用于把**快速变化的多写者命名空间**收敛到本 run 的作用域:
        两套 track 并发写同一个桶时，整桶快照会把对方的正常写入读成"我方越界"。
        传 `prefix` 时用 S3 的 `prefix=` 服务端过滤（不是本地过滤——本地过滤仍会把
        对方的 key 拉回来，只是丢弃，代价一样且无法证明服务端真的隔离）。
        """
        result: dict[str, tuple[str, int]] = {}
        sent_prefix: str | None = None
        token = ""
        seen = set()
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        for _page in range(10000):
            params = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                params["prefix"] = prefix
                # 记录点就是**发送点**: 只有真的把 prefix 写进 LIST 请求才会置旗。
                sent_prefix = prefix
            if token:
                params["continuation-token"] = token
            url = self.base + "?" + urlencode(params, quote_via=quote)
            data = transfer(signed_request("GET", url, b"", "", self.access, self.secret))
            try:
                page = ElementTree.fromstring(data)
                if page.tag != ns + "ListBucketResult":
                    raise ValueError("unexpected list root")
                for item in page.findall(ns + "Contents"):
                    key = item.findtext(ns + "Key")
                    etag = item.findtext(ns + "ETag")
                    size = int(item.findtext(ns + "Size", "-1"))
                    if not key or not etag or size < 0 or key in result:
                        raise ValueError("invalid or duplicate list entry")
                    result[key] = (etag, size)
                truncated = page.findtext(ns + "IsTruncated")
                if truncated == "false":
                    self.snapshot_prefix_calls.append(sent_prefix)
                    return result
                if truncated != "true":
                    raise ValueError("missing truncation flag")
                token = page.findtext(ns + "NextContinuationToken", "")
                if not token or token in seen:
                    raise ValueError("missing or repeated pagination token")
                seen.add(token)
            except (ElementTree.ParseError, ValueError) as error:
                self.snapshot_prefix_calls.append(sent_prefix)
                raise RuntimeError("Invalid ListObjectsV2 response") from error
        self.snapshot_prefix_calls.append(sent_prefix)
        raise RuntimeError("ListObjectsV2 page limit exceeded")


def verify_public(obj: Object, public_base: str = PUBLIC_BASE) -> None:
    url = f"{public_base.rstrip('/')}/{quote(obj.key, safe='/~')}"
    request = Request(
        url, headers={"Cache-Control": "no-cache", "User-Agent": READ_BACK_USER_AGENT}, method="GET"
    )
    transfer(request, expected=obj.body)
    print(f"Verified anonymous bytes and SHA-256: {obj.key} {hashlib.sha256(obj.body).hexdigest()}")


def guard_changes(before, after, allowed: Callable[[str], bool],
                  intended: set[str] | None = None, scope: str | None = None,
                  scope_calls: tuple[str | None, ...] | None = None) -> None:
    """快照差分校验。两个独立条件，缺一都是**空洞检查**:

    ① 作用域: changed ⊆ allowed（越界写 ⇒ 红）
    ② 归属:   changed ⊆ intended（**作用域内**的非预期变更也红）

    只有 ① 时，若快照本身已按前缀收敛（多写者命名空间），① 就退化成恒真 —— 等于没检查。
    `intended` 来自本次要发布的 objects（不是从快照里读的），因此不与被测数据同源。
    """
    # 先验仪器，再用它的读数: 若某次快照没按 prefix 收敛，后面的 diff 根本不代表本作用域，
    # 报"越界"会把人引向错误的处置（看起来像别人写进我的命名空间）。
    if scope is not None and scope_calls is not None:
        for index, got in enumerate(scope_calls, start=1):
            if got != scope:
                raise RuntimeError(
                    f"bucket guard: snapshot #{index} did not use the server-side prefix "
                    f"filter (got {got!r}, want {scope!r})"
                )
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    # 两类违规**必须分开发声**: 处置方式完全不同（越界 = 有人在写不属于我的命名空间;
    # 非预期 = 我的命名空间里有变更但不在本次清单里, 可能是并发同 track 或 prune 删除）。
    out_of_scope = sorted(key for key in changed if not allowed(key))
    not_intended = sorted(key for key in changed
                          if allowed(key) and intended is not None and key not in intended)
    if out_of_scope:
        raise RuntimeError("Bucket changed outside allowed scope: " + ", ".join(out_of_scope))
    if not_intended:
        raise RuntimeError("Bucket changed inside scope but not intended by this run: "
                           + ", ".join(not_intended))
    listed = "" if scope_calls is None else " snapshots=" + ",".join(
        "prefix" if got else "none" for got in scope_calls)
    print(f"Bucket guard verified: prefix={scope or 'none'}{listed} "
          f"{len(changed)} changed keys, all within allowed scope and intended")


def publish_objects(
    client: R2, objects: list[Object], allowed: Callable[[str], bool], *,
    public_base: str = PUBLIC_BASE, verify_each: bool = False,
    before_put: Callable[[Object], None] | None = None,
    snapshot_prefix: str | None = None,
) -> None:
    if not objects or any(not allowed(obj.key) for obj in objects):
        raise RuntimeError("Publication key outside allowed scope")
    if len({obj.key for obj in objects}) != len(objects):
        raise RuntimeError("Duplicate publication key")
    calls = getattr(client, "snapshot_prefix_calls", [])
    seen_before = len(calls)
    before = client.snapshot(snapshot_prefix)
    before_calls = tuple(calls[seen_before:])
    try:
        for obj in objects:
            # Recheck on retries too: a mutable publication can become stale
            # during HTTP backoff, even after a successful initial preflight.
            client.put(obj, before_attempt=(lambda: before_put(obj)) if before_put else None)
            if verify_each:
                # For wheels the final object is SHA256SUMS. All referenced
                # wheels have been uploaded AND anonymously verified first.
                verify_public(obj, public_base)
        if not verify_each:
            for obj in objects:
                verify_public(obj, public_base)
    finally:
        # Include partial failures. A list failure is a failed guard, never PASS.
        seen_after = len(calls)
        after = client.snapshot(snapshot_prefix)
        after_calls = tuple(calls[seen_after:])
        guard_changes(before, after, allowed,
                      intended={obj.key for obj in objects},
                      scope=snapshot_prefix,
                      scope_calls=before_calls + after_calls)
