"""Isolated S3-like object store; no live endpoints or credentials."""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest


@pytest.fixture
def source_repo(tmp_path):
    """Real source remote and movable tags, entirely inside the test directory."""
    work = tmp_path / "source"
    remote = tmp_path / "source.git"
    home = tmp_path / "git-home"
    work.mkdir()
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}

    def git(*args):
        return subprocess.run(["git", *args], cwd=work, env=env, check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    git("init", "--bare", str(remote))
    git("init")
    git("remote", "add", "origin", str(remote))
    git("commit", "--allow-empty", "-m", "A")
    old = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "B")
    new = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:refs/heads/dev")

    def move(track, commit, annotated=False):
        args = ["tag", "--force"]
        if annotated:
            args += ["--annotate", "--message", "fixture tag"]
        git(*args, track, commit)
        git("push", "--force", "origin", f"refs/tags/{track}")

    for track in ("stable", "nightly", "internal"):
        move(track, old)
    return SimpleNamespace(path=work, remote=remote, old=old, new=new, move=move, git=git)


@pytest.fixture
def s3(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    state = SimpleNamespace(objects={"unrelated/old": b"keep",
                                     "wheel/nightly/pre-existing.whl": b"pre-existing"}, events=[],
                            outside_change=None, concurrency_key=None, bad_read=None, fail_put=None,
                            list_denied=False, wrong_list=False)

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            key = unquote(self.path).removeprefix("/test-bucket/")
            state.events.append(("PUT", key))
            assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
            assert self.headers["Cache-Control"] == "max-age=60, must-revalidate"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert self.headers["x-amz-content-sha256"] == sha256(body).hexdigest()
            if state.fail_put == key:
                self.send_response(503)
            else:
                state.objects[key] = body
                self.send_response(200)
            self.end_headers()
            if state.outside_change == "added":
                state.objects[state.concurrency_key or "unrelated/new"] = b"concurrent"
                state.objects.setdefault("unrelated/new", b"new")
            elif state.outside_change == "added_same_track":
                state.objects["wheel/nightly/unexpected.w."] = b"x"
            elif state.outside_change == "removed_same_track":
                # 删除**同前缀**里的一个**快照前就存在**的对象：本前缀内 changed、且不在 intended 里。
                state.objects.pop("wheel/nightly/pre-existing.whl", None)
            elif state.outside_change == "removed":
                state.objects.pop("unrelated/old", None)
            elif state.outside_change == "changed":
                state.objects["unrelated/old"] = b"edit"  # Same size, different ETag.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/test-bucket":
                state.events.append(("LIST", parsed.query))
                assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
                if state.list_denied:
                    self.send_response(403)
                    self.end_headers()
                    return
                params = parse_qs(parsed.query)
                assert params["list-type"] == ["2"]
                offset = int(params.get("continuation-token", ["0"])[0])
                # S3 的 prefix 是**服务端**过滤: 假实现必须照做，否则"快照级收敛"无法被测到。
                prefix = params.get("prefix", [""])[0]
                keys = sorted(k for k in state.objects if k.startswith(prefix))
                page = keys[offset:offset + 2]
                truncated = offset + 2 < len(keys)
                xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                for key in page:
                    body = state.objects[key]
                    xml += (f"<Contents><Key>{escape(key)}</Key><Size>{len(body)}</Size>"
                            f'<ETag>"{sha256(body).hexdigest()}"</ETag></Contents>')
                xml += f"<IsTruncated>{str(truncated).lower()}</IsTruncated>"
                if truncated:
                    xml += f"<NextContinuationToken>{offset + 2}</NextContinuationToken>"
                xml += "</ListBucketResult>"
                body = b"not xml" if state.wrong_list else xml.encode()
            else:
                key = unquote(parsed.path).lstrip("/")
                state.events.append(("GET", key))
                assert self.headers.get("Authorization") is None
                assert self.headers.get("x-amz-date") is None
                body = state.objects.get(key)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if key == state.bad_read:
                    body = b"stale object"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
