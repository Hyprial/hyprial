#!/usr/bin/env python3
"""Attach the built wheels to the public GitHub release for a track tag.

Why this exists
---------------
`.github/workflows/wheel-release.yml` builds five platform wheels and publishes
them to the INTERNAL Forgejo PyPI registry. Nothing ever copies them to the
public GitHub release, so every public release shows `assets: 0`. A public
installer then has nothing to download and falls back to building from source
(`git+...@<commit>`), which needs a Go toolchain on the customer's machine.

This script closes that gap without passing workflow artifacts between jobs:
it re-reads the exact files that were published to the internal registry
(the registry is the single source of truth) and requires the exact five-target
set. It stages all wheels before replacing `SHA256SUMS`; that manifest is the
logical pointer, so the previous complete set stays selected on a stage failure.

Identity note (read before changing anything)
---------------------------------------------
The wheel's version carries the *internal* commit (`<base>+g<sha7>`). The public
tag points at a snapshot commit with a DIFFERENT sha. Both name the same tree
content, but they are different identifiers. Therefore:

  * the wheel embeds the internal sha7, and that is the precise provenance;
  * the release tag names the public snapshot sha;
  * an installer receipt must record BOTH if it wants to be checkable
    (`install.commit` = public sha, `install.wheel_version` = the stamped
    version). Recording only one of them makes `verified_commit` ambiguous.

Usage
-----
  attach_wheel_assets.py --tag stable --version 0.5.4+g1a2b3c4 \
      --registry-url https://code.hyprial.com/api/packages/HyprialOS/pypi \
      --user <u> --token <t> --github-repo Hyprial/hyprial --gh-token <t> \
      [--dry-run]

Exit codes (these are instructions, not categories -- see the shell vocabulary
used elsewhere in this repo: 0 pass, 1 fail, 2 not evaluable, 3 injection not
applied, 4 partial data):
  0  every wheel and SHA256SUMS are attached, and read back from the release
  1  a real failure (registry served something unexpected, or upload refused)
  2  not evaluable: a required input or credential is missing
  4  partial: the manifest-pointer swap failed and rollback also failed
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts.ci.github_publish_policy.netretry import (
        NETWORK_RETRY_ATTEMPTS,
        NETWORK_RETRY_BACKOFF,
        _backoff_for,
    )
except ModuleNotFoundError:  # direct ``python scripts/ci/...`` execution
    from github_publish_policy.netretry import (  # type: ignore[no-redef]
        NETWORK_RETRY_ATTEMPTS,
        NETWORK_RETRY_BACKOFF,
        _backoff_for,
    )

_SOURCE_REMOTE = runpy.run_path(str(Path(__file__).with_name("wheel_source_remote.py")))
DIST = "hyprial"
PLATFORM_TAGS = (
    "macosx_13_0_arm64",
    "macosx_13_0_x86_64",
    "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "manylinux_2_17_aarch64.manylinux2014_aarch64",
    "win_amd64",
)
SUPPORTED_ABIS = ("py3-none", "cp312-abi3")


class WheelSetError(ValueError):
    """The registry candidate is not the complete five-platform release set."""


def asset_plan(names: list[str], version: str) -> tuple[str, ...]:
    """Return the exact release assets, refusing incomplete or extra wheels."""
    actual = set(names)
    matching_abis = [
        abi for abi in SUPPORTED_ABIS
        if actual == {f"hyprial-{version}-{abi}-{platform}.whl" for platform in PLATFORM_TAGS}
    ]
    if len(names) != len(PLATFORM_TAGS) or len(matching_abis) != 1:
        missing_platforms = [
            platform for platform in PLATFORM_TAGS
            if not any(f"hyprial-{version}-{abi}-{platform}.whl" in actual for abi in SUPPORTED_ABIS)
        ]
        raise WheelSetError(
            "candidate must contain exactly five platform wheels with one supported ABI; "
            f"missing platforms={missing_platforms!r}, got={sorted(actual)!r}"
        )
    return (*sorted(actual), "SHA256SUMS")


def _http(url: str, *, auth: tuple[str, str] | None, method: str = "GET", data: bytes | None = None,
          headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, method=method, data=data)
    if auth:
        import base64

        raw = f"{auth[0]}:{auth[1]}".encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:  # the server answered, with a status
        return exc.code, exc.read(), dict(exc.headers or {})
    except (urllib.error.URLError, OSError) as exc:
        # The server did not answer at all. That is "I could not look", not
        # "it is gone": returning a status the caller would read as a failure
        # would report a network problem as a missing artefact, and the two have
        # different next actions. Status 0 means exactly "no answer".
        return 0, f"no answer: {exc}".encode(), {}


def _wait_for_registry(
    registry: str,
    auth: tuple[str, str],
    version: str,
    seconds: int,
    *,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> list[str]:
    """Poll until the registry serves the exact five-wheel set for ``version``.

    Why this exists: `wheel-release` and `github-publish` both fire on the same
    tag push, so they run in parallel. Attaching can therefore arrive first and
    find either no wheels or only a partial set. The bounded wait treats both as
    "not yet"; when the bound expires, a partial set is a failure that names
    every missing platform, while an unavailable set remains not evaluable.
    """

    deadline = monotonic() + max(0, seconds)
    attempt = 0
    last_incomplete: WheelSetError | None = None
    while True:
        attempt += 1
        unavailable = False
        try:
            names = _registry_files(registry, auth, version)
            asset_plan(names, version)
            return names
        except WheelSetError as exc:
            last_incomplete = exc
        except SystemExit as exc:
            if exc.code != 2:
                raise
            unavailable = True

        remaining = deadline - monotonic()
        if remaining <= 0:
            if last_incomplete is not None and not unavailable:
                print(
                    f"FAIL: after {attempt} attempt(s) over {seconds}s the registry "
                    f"still serves an incomplete set for {version}: {last_incomplete}",
                    file=sys.stderr,
                )
                raise last_incomplete
            print(
                f"NOT EVALUABLE: after {attempt} attempt(s) over {seconds}s the registry "
                f"still does not serve {version}; either wheel-release has not finished, "
                "or it failed. Re-dispatch this workflow once it has.",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        state = "an incomplete wheel set" if last_incomplete is not None else "no wheels"
        print(
            f"registry serves {state} for {version} (attempt {attempt}); "
            f"waiting up to {int(remaining)}s more -- wheel-release runs in parallel",
            file=sys.stderr,
        )
        sleep(min(30, max(1, int(remaining))))


def _registry_files(registry: str, auth: tuple[str, str], version: str) -> list[str]:
    """List the files the registry serves for `dist` and keep our version."""

    # Forgejo's PyPI registry has no JSON API and no `<registry>/<dist>/` page;
    # the only listing is the PEP 503 simple index (docs/guides/wheel-distribution.md).
    url = f"{registry.rstrip('/')}/simple/{DIST}/"
    status, body, _ = _http(url, auth=auth)
    if status == 0:
        print(f"NOT EVALUABLE: the registry did not answer at {url}: {body.decode('utf-8', 'replace')}",
              file=sys.stderr)
        raise SystemExit(2)
    if status != 200:
        print(f"NOT EVALUABLE: registry listing failed with {status} at {url}", file=sys.stderr)
        raise SystemExit(2)
    # Forgejo renders the index with Go html/template, which writes "+" as "&#43;";
    # decode entities first or no local version (`0.5.5+g<sha>`) ever matches.
    text = html.unescape(body.decode("utf-8", "replace"))
    names = re.findall(r"[A-Za-z0-9_.+!-]+\.whl", text)

    matched = sorted({name for name in names if name.endswith(".whl") and version in name})
    if not matched:
        print(
            f"NOT EVALUABLE: the registry serves no wheel for {DIST}=={version}; "
            "has wheel-release.yml finished for this tag?",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return matched


def _download(registry: str, auth: tuple[str, str], name: str, dest_dir: str) -> tuple[str, str]:
    # Forgejo serves files at <registry>/files/<dist>/<version>/<filename>. Build the
    # URL on the registry base we were given (CI reaches it by an internal host),
    # not from the index hrefs, which carry the public app URL.
    version = name.split("-")[1]
    url = (f"{registry.rstrip('/')}/files/{DIST}/{urllib.parse.quote(version, safe='')}/"
           f"{urllib.parse.quote(name)}")
    status, body, _ = _http(url, auth=auth)
    if status == 0:
        print(f"NOT EVALUABLE: the registry did not answer for {name}: "
              f"{body.decode('utf-8', 'replace')}", file=sys.stderr)
        raise SystemExit(2)
    if status != 200:
        print(f"FAIL: {name} returned {status} from the registry", file=sys.stderr)
        raise SystemExit(1)
    path = f"{dest_dir}/{name}"
    with open(path, "wb") as handle:
        handle.write(body)
    return path, hashlib.sha256(body).hexdigest()


def _release(repo: str, tag: str, token: str, *, dry_run: bool) -> dict[str, object]:
    api = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    status, body, _ = _http(api, auth=None, headers={"Authorization": f"Bearer {token}",
                                                    "Accept": "application/vnd.github+json"})
    if status == 0:
        print(f"NOT EVALUABLE: the GitHub API did not answer: {body.decode('utf-8', 'replace')}",
              file=sys.stderr)
        raise SystemExit(2)
    if status == 200:
        return json.loads(body)
    if status != 404:
        print(f"FAIL: reading the release for {tag} returned {status}", file=sys.stderr)
        raise SystemExit(1)
    if dry_run:
        print(f"DRY RUN: release for {tag} does not exist yet; it would be created")
        return {"id": 0, "upload_url": "dry-run", "tag_name": tag}
    create = "https://api.github.com/repos/" + repo + "/releases"
    payload = json.dumps({"tag_name": tag, "name": tag, "draft": False, "prerelease": tag == "nightly"}).encode()
    status, body, _ = _http(create, auth=None, method="POST", data=payload,
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json",
                                     "Content-Type": "application/json"})
    if status not in (200, 201):
        print(f"FAIL: creating the release for {tag} returned {status}: {body[:300]!r}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(body)


def _upload(upload_url: str, token: str, path: str, name: str, *, dry_run: bool) -> dict[str, object] | None:
    if dry_run:
        print(f"DRY RUN: would upload {name}")
        return {"id": -1, "name": name, "url": "dry-run"}
    base = upload_url.split("{", 1)[0]
    url = f"{base}?name={urllib.parse.quote(name)}&label={urllib.parse.quote(name)}"
    with open(path, "rb") as handle:
        data = handle.read()
    status, body, _ = _http(
        url, auth=None, method="POST", data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/octet-stream",
                 "Content-Length": str(len(data))},
    )
    if status in (200, 201):
        return json.loads(body)
    print(f"FAIL: uploading {name} returned {status}: {body[:300]!r}", file=sys.stderr)
    return None


def _asset_bytes(asset: dict[str, object], token: str) -> bytes | None:
    status, body, _ = _http(
        str(asset.get("url") or ""), auth=None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"},
    )
    if status == 200:
        return body
    print(f"FAIL: reading existing asset {asset.get('name')} returned {status}", file=sys.stderr)
    return None


def _rename_asset(repo: str, asset_id: int, name: str, token: str, *, dry_run: bool) -> bool:
    if dry_run:
        print(f"DRY RUN: would rename asset {asset_id} to {name}")
        return True
    url = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"
    payload = json.dumps({"name": name, "label": name}).encode()
    status, body, _ = _http(
        url, auth=None, method="PATCH", data=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    if status == 200:
        return True
    print(f"FAIL: renaming asset {asset_id} to {name} returned {status}: {body[:300]!r}", file=sys.stderr)
    return False


def _delete_asset(repo: str, asset_id: int, token: str, *, dry_run: bool) -> bool:
    if dry_run:
        print(f"DRY RUN: would delete asset {asset_id}")
        return True
    url = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"
    status, body, _ = _http(
        url, auth=None, method="DELETE",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if status == 204:
        return True
    print(f"FAIL: deleting asset {asset_id} returned {status}: {body[:300]!r}", file=sys.stderr)
    return False


def _rename_with_retry(
    repo: str,
    asset: dict[str, object],
    name: str,
    token: str,
    *,
    dry_run: bool,
    rename=_rename_asset,
    sleep=time.sleep,
) -> bool:
    """Rename one asset with the shared three-attempt, 10s/30s policy."""

    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        if rename(repo, int(asset["id"]), name, token, dry_run=dry_run):
            asset["name"] = name
            return True
        if attempt < NETWORK_RETRY_ATTEMPTS:
            wait = _backoff_for(attempt, NETWORK_RETRY_BACKOFF)
            print(
                f"rename asset {asset['id']} to {name}: attempt "
                f"{attempt}/{NETWORK_RETRY_ATTEMPTS} failed; retrying in {wait:g}s",
                file=sys.stderr,
            )
            sleep(wait)
    return False


def _manifest_is_complete(
    manifest: dict[str, object],
    assets: list[dict[str, object]],
    token: str,
    *,
    asset_bytes=_asset_bytes,
) -> bool:
    body = asset_bytes(manifest, token)
    if body is None:
        return False
    lines = body.decode("utf-8", "replace").splitlines()
    entries: list[tuple[str, str]] = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (hyprial-[A-Za-z0-9_.+!-]+\.whl)", line)
        if match is None:
            return False
        entries.append((match.group(1), match.group(2)))
    names = [name for _digest, name in entries]
    if len(entries) != 5 or len(set(names)) != 5:
        return False
    if any(sum(name.endswith(f"-{tag}.whl") for name in names) != 1 for tag in PLATFORM_TAGS):
        return False
    by_name = {str(asset.get("name")): asset for asset in assets}
    for digest, name in entries:
        wheel = by_name.get(name)
        if wheel is None:
            return False
        wheel_body = asset_bytes(wheel, token)
        if wheel_body is None or hashlib.sha256(wheel_body).hexdigest() != digest:
            return False
    return True


def _repair_missing_manifest(
    assets: list[dict[str, object]],
    repo: str,
    token: str,
    *,
    dry_run: bool,
    repair_command: str,
    asset_bytes=_asset_bytes,
    rename=_rename_asset,
    sleep=time.sleep,
) -> bool:
    """Restore the newest verified manifest when the canonical name is absent."""

    if any(asset.get("name") == "SHA256SUMS" for asset in assets):
        return True
    candidates = [
        asset for asset in assets
        if str(asset.get("name", "")).startswith("SHA256SUMS.previous-")
        or str(asset.get("name", "")).startswith("SHA256SUMS.")
    ]
    if not candidates:
        return True  # first publication: there is no pointer to repair
    complete = [
        asset for asset in candidates
        if _manifest_is_complete(asset, assets, token, asset_bytes=asset_bytes)
    ]
    if not complete:
        print(
            "CANONICAL SHA256SUMS IS MISSING and no staged/backup manifest is "
            f"complete; repair command: {repair_command}",
            file=sys.stderr,
        )
        return False
    newest = max(complete, key=lambda asset: int(asset.get("id", 0)))
    if not _rename_with_retry(
        repo, newest, "SHA256SUMS", token, dry_run=dry_run,
        rename=rename, sleep=sleep,
    ):
        print(
            "CANONICAL SHA256SUMS IS MISSING; automatic repair failed for "
            f"asset {newest['name']} ({newest['id']}); repair command: {repair_command}",
            file=sys.stderr,
        )
        return False
    print(f"self-repair: restored SHA256SUMS from verified asset {newest['id']}")
    return True


def _missing_manifest_error(backup: str, repair_command: str) -> None:
    print(
        "CANONICAL SHA256SUMS IS MISSING after pointer switch and rollback retries; "
        f"backup asset: {backup}; repair command: {repair_command}",
        file=sys.stderr,
    )


def _swap_manifest_pointer(
    repo: str,
    old: dict[str, object] | None,
    staged: dict[str, object],
    token: str,
    *,
    tag: str = "",
    dry_run: bool,
    repair_command: str,
    rename=_rename_asset,
    release=_release,
    sleep=time.sleep,
) -> int:
    """Perform the two renames, rolling the old pointer back on failure."""

    backup_name = ""
    if old is not None:
        backup_name = f"SHA256SUMS.previous-{old['id']}"
        if not _rename_with_retry(
            repo, old, backup_name, token, dry_run=dry_run,
            rename=rename, sleep=sleep,
        ):
            # A PATCH can apply even when every response is lost. Re-read the
            # release before deciding whether the canonical pointer is safe.
            refreshed = release(repo, tag, token, dry_run=dry_run)
            fresh_assets = [
                item for item in refreshed.get("assets", [])
                if isinstance(item, dict)
            ]
            if any(item.get("name") == "SHA256SUMS" for item in fresh_assets):
                print(
                    "FAIL: could not move the old SHA256SUMS to its backup name; "
                    "the canonical manifest remains selected",
                    file=sys.stderr,
                )
                return 1
            remote_old = next(
                (item for item in fresh_assets if item.get("id") == old.get("id")),
                None,
            )
            if remote_old is not None and _rename_with_retry(
                repo, remote_old, "SHA256SUMS", token, dry_run=dry_run,
                rename=rename, sleep=sleep,
            ):
                print(
                    "FAIL: moving the old manifest had an ambiguous response; "
                    "previous complete manifest was restored",
                    file=sys.stderr,
                )
                return 1
            _missing_manifest_error(backup_name, repair_command)
            return 4
    if _rename_with_retry(
        repo, staged, "SHA256SUMS", token, dry_run=dry_run,
        rename=rename, sleep=sleep,
    ):
        return 0
    if old is not None and _rename_with_retry(
        repo, old, "SHA256SUMS", token, dry_run=dry_run,
        rename=rename, sleep=sleep,
    ):
        print(
            "FAIL: manifest pointer switch failed; previous complete manifest was restored",
            file=sys.stderr,
        )
        return 1
    backup = backup_name or str(staged.get("name"))
    _missing_manifest_error(backup, repair_command)
    return 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True, help="stamped wheel version, e.g. 0.5.4+g1a2b3c4")
    parser.add_argument("--source-sha", required=False, default="", help="full source commit the wheel version must identify")
    parser.add_argument("--registry-url", required=True)
    # Credentials default to environment variables ON PURPOSE. This repository's
    # wheel-release.yml states the contract explicitly: "凭据全程只在进程 env;
    # 不进 argv、不落盘". An argv token is visible in `ps` to every process on
    # the runner, so the flags exist only for a human debugging by hand; the
    # workflow sets the environment instead.
    parser.add_argument("--user", default=os.environ.get("WHEEL_REGISTRY_USER", ""))
    parser.add_argument("--token", default=os.environ.get("WHEEL_REGISTRY_TOKEN", ""))
    parser.add_argument("--github-repo", required=True)
    parser.add_argument(
        "--gh-token",
        default=os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", ""),
        help="defaults to $GH_TOKEN, then $GITHUB_TOKEN, so the workflow never puts it in argv",
    )
    parser.add_argument("--workdir", default="/tmp/wheel-assets")
    parser.add_argument(
        "--wait-for-registry",
        type=int,
        default=0,
        help="seconds to keep polling for the built wheels (default 0 = do not wait). "
        "The workflow passes at most 2400, bounded by the job's remaining budget, because "
        "wheel-release runs in parallel with it; the default is 0 so the script stays "
        "fast when a human runs it.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Report EVERY missing input in one line. Two separate reports would make the
    # person fixing it discover the second gap only after fixing the first, which
    # is exactly the round trip this check exists to avoid.
    missing: list[str] = []
    if not args.gh_token and not args.dry_run:
        missing.append("GH_TOKEN (or --gh-token)")
    if not args.user or not args.token:
        missing.append("WHEEL_REGISTRY_USER + WHEEL_REGISTRY_TOKEN (or --user/--token)")
    if missing:
        print("NOT EVALUABLE: nothing can be attached; missing: " + ", ".join(missing),
              file=sys.stderr)
        return 2

    if args.source_sha:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", args.source_sha):
            print("FAIL: --source-sha must be a full 40-hex commit", file=sys.stderr)
            return 1
        if not args.version.lower().endswith(f"+g{args.source_sha[:7].lower()}"):
            print("FAIL: wheel version does not identify --source-sha", file=sys.stderr)
            return 1

    repair_parts = [
        "python3", "scripts/ci/attach_wheel_assets.py",
        "--tag", args.tag, "--version", args.version,
    ]
    if args.source_sha:
        repair_parts.extend(("--source-sha", args.source_sha))
    repair_parts.extend(
        ("--registry-url", args.registry_url, "--github-repo", args.github_repo)
    )
    repair_command = " ".join(shlex.quote(part) for part in repair_parts)

    # Repair the public pointer before consulting the wheel registry. A prior
    # interrupted run must be recoverable even while the new build is missing
    # or the registry is unavailable.
    release = _release(args.github_repo, args.tag, args.gh_token, dry_run=args.dry_run)
    assets = [item for item in release.get("assets", []) if isinstance(item, dict)]
    by_name = {str(item.get("name")): item for item in assets}
    if len(by_name) != len(assets):
        print("FAIL: release contains duplicate asset names; refusing mutation", file=sys.stderr)
        return 1
    if not _repair_missing_manifest(
        assets, args.github_repo, args.gh_token, dry_run=args.dry_run,
        repair_command=repair_command,
    ):
        return 4
    by_name = {str(item.get("name")): item for item in assets}
    upload_url = str(release.get("upload_url") or "")
    if not upload_url:
        print("FAIL: the release has no upload_url", file=sys.stderr)
        return 1

    os.makedirs(args.workdir, exist_ok=True)
    auth = (args.user, args.token)
    try:
        names = _wait_for_registry(
            args.registry_url, auth, args.version, args.wait_for_registry
        )
        asset_plan(names, args.version)
    except WheelSetError as exc:
        print(f"FAIL: {exc}; release assets were not changed", file=sys.stderr)
        return 1
    print(f"registry serves the complete five-wheel set for {args.version}:")
    for name in names:
        print(f"  {name}")

    sums: list[str] = []
    uploaded = 0
    for name in names:
        path, digest = _download(args.registry_url, auth, name, args.workdir)
        try:
            _SOURCE_REMOTE["check_wheel"](Path(path))
        except _SOURCE_REMOTE["SourceRemoteError"] as exc:
            print(f"FAIL: {exc}; release assets were not changed", file=sys.stderr)
            return 1
        sums.append(f"{digest}  {name}")
        print(f"downloaded {name} sha256={digest}")

    sums_path = f"{args.workdir}/SHA256SUMS"
    with open(sums_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted(sums)) + "\n")
    print(f"wrote {sums_path} ({len(sums)} entries)")

    # Stage all five content-addressed wheel names first. SHA256SUMS remains the
    # public selector for the previous complete set until every candidate byte
    # is present and verified. A failed stage therefore never selects a half set.
    created: list[dict[str, object]] = []
    for name, digest in ((line.split("  ", 1)[1], line.split("  ", 1)[0]) for line in sums):
        existing = by_name.get(name)
        if existing is not None:
            body = _asset_bytes(existing, args.gh_token)
            if body is None or hashlib.sha256(body).hexdigest() != digest:
                print(f"FAIL: existing candidate asset {name} has unexpected bytes; previous manifest unchanged", file=sys.stderr)
                return 1
            continue
        asset = _upload(upload_url, args.gh_token, f"{args.workdir}/{name}", name, dry_run=args.dry_run)
        if asset is None:
            for made in created:
                _delete_asset(args.github_repo, int(made["id"]), args.gh_token, dry_run=args.dry_run)
            print("FAIL: candidate wheel staging failed; previous complete manifest remains selected", file=sys.stderr)
            return 1
        uploaded_body = None if args.dry_run else _asset_bytes(asset, args.gh_token)
        if not args.dry_run and (
            uploaded_body is None or hashlib.sha256(uploaded_body).hexdigest() != digest
        ):
            print(
                f"FAIL: uploaded candidate asset {name} did not verify; "
                "previous manifest unchanged",
                file=sys.stderr,
            )
            return 1
        created.append(asset)
        uploaded += 1

    with open(sums_path, "rb") as sums_file:
        desired_sums = sums_file.read()
    old = by_name.get("SHA256SUMS")
    old_body = _asset_bytes(old, args.gh_token) if old is not None else None
    if old is not None and old_body is None:
        print("NOT EVALUABLE: existing SHA256SUMS could not be read; refusing mutation", file=sys.stderr)
        return 2
    if old_body == desired_sums:
        stale = [
            item for name, item in by_name.items()
            if (name.endswith(".whl") and name not in names)
            or name.startswith("SHA256SUMS.previous-")
        ]
        failed = [item for item in stale if not _delete_asset(
            args.github_repo, int(item["id"]), args.gh_token, dry_run=args.dry_run
        )]
        if failed:
            print("FAIL: complete set is selected, but obsolete assets could not all be removed", file=sys.stderr)
            return 1
        print(f"attached complete 6/6 asset set to {args.tag} (already selected)")
        return 0

    stage_name = f"SHA256SUMS.{args.version}"
    staged = by_name.get(stage_name)
    if staged is not None:
        if _asset_bytes(staged, args.gh_token) != desired_sums:
            print(f"FAIL: stale staging manifest {stage_name} has unexpected bytes", file=sys.stderr)
            return 1
    else:
        staged = _upload(upload_url, args.gh_token, sums_path, stage_name, dry_run=args.dry_run)
        if staged is None:
            print("FAIL: staging manifest upload failed; previous complete manifest remains selected", file=sys.stderr)
            return 1
        if not args.dry_run and _asset_bytes(staged, args.gh_token) != desired_sums:
            print(
                "FAIL: uploaded staging manifest did not verify; previous complete "
                "manifest remains selected",
                file=sys.stderr,
            )
            return 1

    # GitHub has no atomic multi-asset transaction. Move the old pointer aside,
    # then promote the verified staged manifest. Every rename, including the
    # rollback, uses the shared bounded network retry policy.
    swap_rc = _swap_manifest_pointer(
        args.github_repo, old, staged, args.gh_token, tag=args.tag,
        dry_run=args.dry_run, repair_command=repair_command,
    )
    if swap_rc != 0:
        return swap_rc

    cleanup = [
        item for name, item in by_name.items()
        if (name.endswith(".whl") and name not in names)
        or name.startswith("SHA256SUMS.previous-")
    ]
    if old is not None:
        cleanup.append(old)
    failed_cleanup = [item for item in cleanup if not _delete_asset(
        args.github_repo, int(item["id"]), args.gh_token, dry_run=args.dry_run
    )]
    if failed_cleanup:
        print("FAIL: new complete set is selected, but old complete assets could not all be removed", file=sys.stderr)
        return 1
    print(f"attached complete 6/6 asset set to {args.tag}; manifest swapped last")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
