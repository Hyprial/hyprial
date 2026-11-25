#!/usr/bin/env python3
"""Download one workflow artifact by name through the forge API.

This instance mirrors ``actions/upload-artifact`` but not
``actions/download-artifact``, so a job cannot use the download action.  The
workflow token can read the artifacts of its own run through the REST API, and
this script turns that into the same ``name``/``path`` handoff: it selects the
single artifact with the requested name, downloads its ZIP and extracts the
flat file set into the output directory.

The token is only sent to the origin of ``GITHUB_SERVER_URL``.  Cross-origin
redirects are rejected rather than followed with credentials attached.  The
download URL is always derived from the origin that served the artifact
listing: a stored ``archive_download_url`` may name a public host while the job
talks to the forge over its own base URL, and following it blindly would
either fail or move the token to a different origin.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

TIMEOUT_SECONDS = 300
USER_AGENT = "hyprial-ci-download-artifact/1"


class ArtifactError(ValueError):
    """The artifact is missing, ambiguous or unsafe to extract."""


def api_root(server_url: str) -> str:
    return f"{server_url.rstrip('/')}/api/v1"


def origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while credentials would stay on the same origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if origin(target) != origin(req.full_url):
            raise ArtifactError(
                f"refusing redirect from {origin(req.full_url)} to {origin(target)}"
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


def read_json(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(SameOriginRedirects())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ArtifactError(f"GET {url} failed: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ArtifactError(f"GET {url} failed: {error.reason}") from error


def artifact_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("artifacts", [])
    else:
        raise ArtifactError("artifact listing has an unexpected shape")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) for entry in entries
    ):
        raise ArtifactError("artifact listing has an unexpected shape")
    return entries


def select_artifact(entries: list[dict], name: str) -> dict:
    matches = [entry for entry in entries if entry.get("name") == name]
    if len(matches) != 1:
        available = sorted(str(entry.get("name")) for entry in entries)
        raise ArtifactError(
            f"expected exactly one artifact named {name!r}; "
            f"matches={len(matches)}, available={available!r}"
        )
    artifact = matches[0]
    if artifact.get("expired"):
        raise ArtifactError(f"artifact {name!r} has expired")
    if not isinstance(artifact.get("id"), int):
        raise ArtifactError(f"artifact {name!r} has no numeric id")
    return artifact


def download_url(artifact: dict, api: str, repo: str) -> str:
    return f"{api}/repos/{repo}/actions/artifacts/{artifact['id']}/zip"


def safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and name != ""


def extract(archive: Path, out_dir: Path) -> list[Path]:
    written: list[Path] = []
    root = out_dir.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if not safe_member(info.filename):
                raise ArtifactError(f"unsafe artifact member: {info.filename!r}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ArtifactError(f"artifact member is a symlink: {info.filename}")
            target = (out_dir / info.filename).resolve()
            if root != target and root not in target.parents:
                raise ArtifactError(f"artifact member escapes {out_dir}: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    sink.write(chunk)
            written.append(target)
    if not written:
        raise ArtifactError("artifact archive contains no files")
    return written


def download(server: str, repo: str, run_id: str, name: str, out_dir: Path) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ArtifactError("GITHUB_TOKEN is not set")
    api = api_root(server)
    listing_url = f"{api}/repos/{repo}/actions/runs/{run_id}/artifacts"
    entries = artifact_list(read_json(listing_url, token))
    artifact = select_artifact(entries, name)
    url = download_url(artifact, api, repo)
    if origin(url) != origin(server):
        raise ArtifactError(f"artifact download URL leaves the forge origin: {url}")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(SameOriginRedirects())
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"artifact-{artifact['id']}-", suffix=".zip", delete=False
    ) as handle:
        archive = Path(handle.name)
    try:
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                with archive.open("wb") as sink:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        sink.write(chunk)
        except urllib.error.HTTPError as error:
            raise ArtifactError(f"artifact download failed: HTTP {error.code}") from error
        files = extract(archive, out_dir)
    finally:
        archive.unlink(missing_ok=True)
    base = out_dir.resolve()
    for path in sorted(files):
        print(f"downloaded {path.relative_to(base)} ({path.stat().st_size} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="exact artifact name")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--server", default=os.environ.get("GITHUB_SERVER_URL"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)
    missing = [
        flag
        for flag, value in (
            ("--run-id/GITHUB_RUN_ID", args.run_id),
            ("--server/GITHUB_SERVER_URL", args.server),
            ("--repo/GITHUB_REPOSITORY", args.repo),
        )
        if not value
    ]
    if missing:
        parser.error(f"missing {', '.join(missing)}")
    try:
        download(args.server, args.repo, args.run_id, args.name, args.out_dir)
    except ArtifactError as error:
        print(f"download-actions-artifact: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
