#!/usr/bin/env python3
"""Offline Codex Squire loader probe; use uv run python, no credentials required.

Install a pinned @openai/codex into an isolated npm prefix, then pass its binary
and a NEW /tmp/hr-* directory. This never starts a daemon or sends a model turn.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time

from hyprial.daemon.impl.squire import SquireSetup


def rpc(process, selector, identifier, method, params):
    process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
    process.stdin.flush()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not selector.select(max(0, deadline - time.monotonic())):
            break
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"Codex exited during {method}: {process.poll()}")
        response = json.loads(line)
        if response.get("id") == identifier:
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            return response["result"]
    raise TimeoutError(method)


def probe(binary, root, skill, text):
    skill.write_text(text, encoding="utf-8")
    env = {
        "HOME": str(root / "home"), "CODEX_HOME": str(root / "native"),
        "PATH": os.pathsep.join([str(Path(shutil.which("node")).parent), "/usr/bin", "/bin"]),
        "TMPDIR": str(root / "tmp"), "XDG_CONFIG_HOME": str(root / "xdg"),
    }
    version = subprocess.check_output([binary, "--version"], env=env, text=True).strip()
    with (root / "stderr.log").open("a") as stderr:
        process = subprocess.Popen(
            [binary, "app-server", "--stdio"], cwd=root / "workspace", env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            text=True, bufsize=1,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            rpc(process, selector, 1, "initialize", {
                "clientInfo": {"name": "squire_loader_probe", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            process.stdin.write('{"method":"initialized","params":{}}\n')
            process.stdin.flush()
            result = rpc(process, selector, 2, "skills/list", {
                "cwds": [str(root / "workspace")], "forceReload": True,
            })
            row = next(row for row in result["data"] if row["cwd"] == str(root / "workspace"))
            thread = rpc(process, selector, 3, "thread/start", {
                "cwd": str(root / "workspace"), "ephemeral": True,
            })
            return {"version": version, "errors": row.get("errors", []),
                    "threadStarted": bool(thread.get("thread", {}).get("id")),
                    "squire": [s for s in row["skills"] if s["name"] == "squire"]}
        finally:
            selector.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.absolute()
    if not str(root).startswith("/tmp/hr-") or root.exists():
        parser.error("--root must be a new /tmp/hr-* directory")
    root.mkdir(mode=0o700)
    root = root.resolve()
    for name in ("home", "native", "tmp", "xdg", "workspace"):
        (root / name).mkdir(mode=0o700)
    setup = SquireSetup(hyprial_home=root / "hyprial", state_dir=root / "hyprial/state")
    home = root / "hyprial/agents/squire"
    setup._ensure_scaffold(home, display_name="Probe Owner", login_name="probe",
                           owner="probe", adapter="probe-squire", dm_route="dm")
    rendered = (home / "config/shared/skills/squire/SKILL.md").read_text()
    assert rendered.startswith("---\n")
    skill = root / "native/skills/squire/SKILL.md"
    skill.parent.mkdir(parents=True)
    old = rendered.split("\n---\n\n", 1)[1]
    red = probe(args.codex, root, skill, old)
    green = probe(args.codex, root, skill, rendered)
    assert any("missing YAML frontmatter" in e["message"] for e in red["errors"]), red
    assert red["threadStarted"] and green["threadStarted"], (red, green)
    assert green["errors"] == [], green
    assert len(green["squire"]) == 1, green
    assert Path(green["squire"][0]["path"]).resolve() == skill.resolve(), green
    print(json.dumps({"ok": True, "red": red, "green": green}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
