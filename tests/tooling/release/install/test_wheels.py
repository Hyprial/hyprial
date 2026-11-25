from hashlib import sha256
import io
import os
import runpy
import time
import zipfile

import pytest
import yaml

from test_publish import CREDS, ROOT

TAGS = ["macosx_13_0_arm64", "macosx_13_0_x86_64",
        "manylinux_2_17_x86_64.manylinux2014_x86_64",
        "manylinux_2_17_aarch64.manylinux2014_aarch64", "win_amd64"]


@pytest.fixture
def wheels(monkeypatch, source_repo):
    monkeypatch.chdir(source_repo.path)
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(source_repo.path.parent / "git-home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    return runpy.run_path(str(ROOT / "scripts/ci/publish_wheels.py"))


PUBLIC_REMOTE = "https://github.com/Hyprial/hyprial.git"


def wheel_body(tag, remote=PUBLIC_REMOTE):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hyprial/_gui/release.json", '{"sourceRemote": "%s"}' % remote)
        archive.writestr("platform", tag)
    return buffer.getvalue()


def distribution(tmp_path, version="0.0.1+gabcdef0", abi="py3-none", remote=PUBLIC_REMOTE):
    lines = []
    names = []
    for tag in TAGS:
        name = f"hyprial-{version}-{abi}-{tag}.whl"
        body = wheel_body(tag, remote)
        (tmp_path / name).write_bytes(body)
        lines.append(f"{sha256(body).hexdigest()}  {name}\n")
        names.append(name)
    (tmp_path / "SHA256SUMS").write_text("".join(lines))
    return names


def test_native_wheel_names_are_accepted_without_relabelling(wheels, tmp_path):
    names = distribution(tmp_path, abi="cp312-abi3")
    objects = wheels["wheel_objects"](tmp_path, "internal")
    assert [obj.key for obj in objects] == [f"wheel/internal/{name}" for name in names + ["SHA256SUMS"]]


@pytest.mark.parametrize("remote", ["ssh://git@git.internal.invalid/org/repo.git", ""])
def test_wheel_with_a_non_public_source_remote_is_never_published(wheels, tmp_path, s3, remote, source_repo):
    distribution(tmp_path, remote=remote)
    with pytest.raises(RuntimeError, match="Refusing to publish"):
        wheels["publish"](tmp_path, "stable", CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert s3.events == []


@pytest.mark.parametrize("problem", ["mixed-abi", "unsupported-abi", "unsafe-name"])
def test_native_manifest_rejects_abi_mismatch_before_network(wheels, tmp_path, problem):
    distribution(tmp_path, abi="cp312-abi3")
    manifest = tmp_path / "SHA256SUMS"
    text = manifest.read_text()
    if problem == "mixed-abi":
        text = text.replace("cp312-abi3", "py3-none", 1)
    elif problem == "unsupported-abi":
        text = text.replace("cp312-abi3", "cp313-abi3")
    else:
        text = text.replace("  hyprial-", "  ../hyprial-", 1)
    manifest.write_text(text)
    with pytest.raises(RuntimeError):
        wheels["wheel_objects"](tmp_path, "internal")


@pytest.mark.parametrize("track", ["stable", "nightly", "internal"])
def test_wheels_verified_before_sums_switch(wheels, tmp_path, s3, track, source_repo):
    names = distribution(tmp_path)
    wheels["publish"](tmp_path, track, CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    prefix = f"wheel/{track}/"
    assert [key for method, key in s3.events if method == "PUT"] == [
        prefix + name for name in names + ["SHA256SUMS"]]
    switch = s3.events.index(("PUT", prefix + "SHA256SUMS"))
    for name in names:
        assert s3.events.index(("GET", prefix + name)) < switch
    assert ("GET", prefix + "SHA256SUMS") in s3.events[switch:]
    assert s3.events[0][0] == s3.events[-1][0] == "LIST"
    assert s3.objects["unrelated/old"] == b"keep"


@pytest.mark.parametrize("problem", ["incomplete", "hash", "duplicate", "missing", "wrong-platform", "wrong-track"])
def test_bad_wheel_set_never_touches_bucket(wheels, tmp_path, s3, problem, source_repo):
    names = distribution(tmp_path)
    manifest = tmp_path / "SHA256SUMS"
    lines = manifest.read_text().splitlines(True)
    track = "stable"
    if problem == "incomplete":
        manifest.write_text("".join(lines[:-1]))
    elif problem == "hash":
        (tmp_path / names[0]).write_bytes(b"damaged")
    elif problem == "duplicate":
        manifest.write_text("".join(lines[:-1] + lines[:1]))
    elif problem == "missing":
        (tmp_path / names[0]).unlink()
    elif problem == "wrong-platform":
        manifest.write_text("".join(lines).replace("win_amd64", "win_arm64"))
    else:
        track = "stable/../internal"
    with pytest.raises(RuntimeError):
        wheels["publish"](tmp_path, track, CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert s3.events == []


@pytest.mark.parametrize("failure", ["readback", "upload"])
def test_failed_wheel_never_switches_manifest(wheels, tmp_path, s3, failure, source_repo):
    names = distribution(tmp_path)
    key = "wheel/internal/" + names[1]
    if failure == "readback":
        s3.bad_read = key
    else:
        s3.fail_put = key
    with pytest.raises(RuntimeError):
        wheels["publish"](tmp_path, "internal", CREDS, source_sha=source_repo.old, endpoint=s3.url, public_base=s3.url)
    assert ("PUT", "wheel/internal/SHA256SUMS") not in s3.events
    assert s3.events[-1][0] == "LIST"


@pytest.mark.parametrize("publishing,concurrent", [
    ("stable", "nightly"),
    ("nightly", "stable"),
])
def test_concurrent_other_track_write_does_not_fail(wheels, tmp_path, s3, source_repo, publishing, concurrent):
    """M-0038: 另一条 track 并发发布时，本 track 的守卫不得判红。

    现场（2026-10-30）: guard 用整桶快照 ⇒ 每条 track 都能看见对方的正常写入 ⇒ 双方都 raise，
    但 wheels 其实都已上传（run 40721/40722，task 98687/98688）。
    现在快照按 `wheel/<track>/` 由**服务端**过滤，守卫只看本 track 的作用域。
    """
    distribution(tmp_path)
    other_key = f"wheel/{concurrent}/hyprial-0.0.0-py3-none-any.whl"
    s3.outside_change = "added"                     # 假服务端在本次 PUT 之后写别的 key
    s3.concurrency_key = other_key                  # 具体写哪条 track 的前缀
    wheels["publish"](tmp_path, publishing, CREDS, source_sha=source_repo.old,
                      endpoint=s3.url, public_base=s3.url)
    assert other_key in s3.objects, "并发写入的对象不该被本 run 覆盖或删除"
    lists = [query for method, query in s3.events if method == "LIST"]
    wanted = (f"prefix=wheel/{publishing}/", f"prefix=wheel%2F{publishing}%2F")
    assert lists and all(any(w in q for w in wanted) for q in lists), \
        "守卫的 LIST 必须带本 track 的 prefix（服务端过滤，而不是本地丢弃）"


def _released_names(tmp_path):
    return [p.name for p in tmp_path.glob("*.whl")]


def test_in_scope_unexpected_change_still_fails(wheels, tmp_path, s3, source_repo):
    """守卫的目的不变: **本 track 前缀内**出现非预期变更 ⇒ 仍然红。

    e2e 核过一条: 两类违规原先共用同一句错误串，无法区分"被谁写的"与"为什么不在清单里"。
    现在分开: 越界 ⇒ `outside allowed scope`；作用域内但不在清单 ⇒ `inside scope but not intended`。
    """
    distribution(tmp_path)
    s3.outside_change = "added_same_track"
    with pytest.raises(RuntimeError, match="inside scope but not intended"):
        wheels["publish"](tmp_path, "nightly", CREDS, source_sha=source_repo.old,
                          endpoint=s3.url, public_base=s3.url)


def test_in_scope_deletion_fails_closed(wheels, tmp_path, s3, source_repo):
    """同前缀内的**删除**是有意 fail-closed（发布器不做 prune）。

    将来真要 prune，必须新增显式入口（`intended_deletes=…`），不能把守卫放松：
    否则"我删的旧 wheel"和"别人删的/丢的"会无法区分。
    """
    distribution(tmp_path)
    s3.outside_change = "removed_same_track"
    with pytest.raises(RuntimeError, match="inside scope but not intended"):
        wheels["publish"](tmp_path, "nightly", CREDS, source_sha=source_repo.old,
                          endpoint=s3.url, public_base=s3.url)


def test_publish_refuses_key_outside_scope_before_any_put(wheels, s3):
    """补偿钉: 前缀外写入不再由守卫告警，靠 `publish_objects` 的 **PUT 前** `allowed()` 校验拦住。

    这条是 e2e 要求的**变异反控**: 把 `publish_objects` 里的
    `if not objects or any(not allowed(obj.key) ...)` 改成恒真/删掉，本条必须红。
    """
    client = wheels["R2"](CREDS, endpoint=s3.url)
    obj = wheels["Object"]("wheel/other-track/x.whl", b"x", "text/plain")
    with pytest.raises(RuntimeError, match="Publication key outside allowed scope"):
        wheels["publish_objects"](client, [obj], lambda key: False)
    assert not [e for e in s3.events if e[0] == "PUT"], "拒绝必须发生在任何 PUT 之前"


def test_wheel_ci_publishes_before_github_dependency():
    doc = yaml.safe_load((ROOT / ".forgejo/workflows/wheel-release.yml").read_text())
    steps = doc["jobs"]["build-publish"]["steps"]
    index = next(i for i, step in enumerate(steps) if "publish_wheels.py" in step.get("run", ""))
    assert "--track" in steps[index]["run"] and '"${WHEEL_TRACK}"' in steps[index]["run"]
    assert set(CREDS) <= set(steps[index]["env"])
    assert '--source-sha "${WHEEL_SOURCE_SHA}"' in steps[index]["run"]
    assert "GIT_CONFIG_VALUE_0" in steps[index]["run"]
    assert "GITHUB_TOKEN" in steps[index]["env"]
    assert "if" not in steps[index], "all three tracks publish without a variable gate"
    build = next(i for i, step in enumerate(steps) if step["name"] == "Stamp local version segment and build wheel")
    assert index == build + 1
    assert all("GH_PUBLISH_TOKEN" not in str(step) for step in steps[:index])


def test_success_line_proves_scope_and_intended(wheels, tmp_path, s3, source_repo, capsys):
    """成功行必须一行同时证两件事（e2e 加码）:

    ① guard 跑完且 intended 判定通过
    ② 快照真的按 prefix 走了**服务端过滤**

    判据: `Bucket guard verified: prefix=wheel/<track>/ N changed keys, all within allowed scope and intended`
    `prefix=` 的取值来自 client 在**发送点**的记录（`list_prefixes_used`），不是形参回显 ——
    所以这一行不能被"传过一遍参数"伪造。
    """
    distribution(tmp_path)
    wheels["publish"](tmp_path, "stable", CREDS, source_sha=source_repo.old,
                      endpoint=s3.url, public_base=s3.url)
    out = capsys.readouterr().out
    # 逐次可见: 两次快照都必须带 prefix，否则"生效"这句话就分不出 1/2（e2e 指出的缝合点）
    assert "Bucket guard verified: prefix=wheel/stable/ snapshots=prefix,prefix " in out, out
    assert "changed keys, all within allowed scope and intended" in out


def test_one_snapshot_without_prefix_is_a_hard_failure(wheels, tmp_path, s3, source_repo, monkeypatch):
    """只有一次快照带了 prefix ⇒ 必须红，而不是显示成"生效"。

    这是 e2e 指出的缝合点: 原实现用 set 跨两次快照累加 ⇒ 一次带、一次不带也显示 prefix=…。
    现在逐次记录 + 逐次核对。
    """
    distribution(tmp_path)
    real_snapshot = wheels["R2"].snapshot
    calls = {"n": 0}

    def snapshot_without_prefix_on_second(self, prefix=None):
        calls["n"] += 1
        return real_snapshot(self, prefix if calls["n"] == 1 else None)

    monkeypatch.setattr(wheels["R2"], "snapshot", snapshot_without_prefix_on_second)
    with pytest.raises(RuntimeError, match="did not use the server-side prefix filter"):
        wheels["publish"](tmp_path, "stable", CREDS, source_sha=source_repo.old,
                          endpoint=s3.url, public_base=s3.url)
