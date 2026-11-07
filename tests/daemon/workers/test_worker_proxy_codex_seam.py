"""The #822 x #825 seam, asserted on the env a spawned codex child receives.

#822 decides proxy routing when the daemon composes a worker's complete
launch; #825 makes the codex app-server client overlay its provider delta
(the resolved DeepSeek key) on that launch at spawn.  Both shape the same
child environment and were written without seeing each other, so these
checks read the environment the real spawn path hands to a real child,
never an intermediate return value:

1. codex+deepseek: DEEPSEEK_API_KEY present, no proxy variable at all.
2. codex+openai: proxy variables present, NO_PROXY keeps the tailnet and
   the forge direct.
3. Negative control: the daemon's own environment carries all eight proxy
   names, and the non-listed vendor's child still has none of them -- so
   "removed" cannot pass merely because nothing was ever added.
4. Cross term: the provider delta adds no proxy name, and the composition
   does not drop the provider delta.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import (
    NO_PROXY_ENVIRONMENT_NAMES,
    PROXY_ENVIRONMENT_NAMES,
)
from hyprial.daemon.impl.application import compose_daemon_worker_launch
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

SETTING_PROXY = "http://127.0.0.1:7896"
AMBIENT_PROXY = "http://10.9.9.9:3128"
KEEP_DIRECT = ".ts.net,code.hyprial.com,127.0.0.1,localhost"
ALL_EIGHT = (*PROXY_ENVIRONMENT_NAMES, *NO_PROXY_ENVIRONMENT_NAMES)
HOME_KEY = "SENTINEL-SEAM-KEY"

AMBIENT = {
    **{name: AMBIENT_PROXY for name in PROXY_ENVIRONMENT_NAMES},
    **{name: "ambient.example" for name in NO_PROXY_ENVIRONMENT_NAMES},
}


def _env_dumping_codex(dump: Path) -> str:
    # The dump path is baked into the script, not passed through the env,
    # so a spawn that drops a delta still reports what it did hand over.
    return (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(dump)!r}, 'w', encoding='utf-8') as handle:\n"
        "    json.dump(dict(os.environ), handle)\n"
        "sys.exit(3)\n"
    )


@pytest.fixture
def seam(tmp_path: Path):
    home = tmp_path / "hyprial-home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"workerProxy": {"url": SETTING_PROXY, "noProxy": KEEP_DIRECT}}),
        encoding="utf-8",
    )
    user_home = tmp_path / "user-home"
    auth = user_home / ".pi" / "agent" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": HOME_KEY}}),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    dump = tmp_path / "child-env.json"
    fake = tmp_path / "codex"
    fake.write_text(_env_dumping_codex(dump), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    registry = AgentRegistry(
        state / "agents.sqlite3",
        owner="new-owner",
        machine="seam-node",
        hyprial_home=home,
    )
    registry.create("worker")
    channel = build_worker_channel(
        actor="agent:seam:node:worker",
        session_ref="sess-1",
        hyprial_home=home,
        state_dir=state,
        node_id="seam-node",
        owner="new-owner",
    )
    # The daemon's own environment carries every proxy name.
    daemon_environ = {"PATH": "/usr/bin:/bin", "HOME": str(user_home), **AMBIENT}

    def child_env(model_vendor: str | None) -> tuple[dict, dict]:
        spec = HarnessLaunchSpec(
            harness="codex", name="worker", headless=True, model_provider=model_vendor
        )
        launch = compose_daemon_worker_launch(
            registry=registry,
            hyprial_home=home,
            spec=spec,
            channel=channel,
            environ=daemon_environ,
        )
        client = CodexAppServerClient(
            spec, command=(str(fake),), complete_launch=launch
        )
        dump.unlink(missing_ok=True)

        async def spawn() -> None:
            try:
                await client.__aenter__()
            except Exception:  # noqa: BLE001 - the fake child exits at once
                pass
            finally:
                process = client._process
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()

        asyncio.run(spawn())
        assert dump.exists(), "the codex child was never spawned"
        return json.loads(dump.read_text(encoding="utf-8")), launch.environment.for_exec()

    yield child_env
    registry.close()


def test_seam_1_codex_deepseek_child_has_the_key_and_no_proxy(seam) -> None:
    child, _launch = seam("deepseek")
    assert child["DEEPSEEK_API_KEY"] == HOME_KEY
    assert [name for name in ALL_EIGHT if name in child] == []


def test_seam_2_codex_openai_child_is_proxied_and_keeps_internal_direct(seam) -> None:
    child, _launch = seam("openai")
    for name in PROXY_ENVIRONMENT_NAMES:
        assert child[name] == SETTING_PROXY, name
    for name in NO_PROXY_ENVIRONMENT_NAMES:
        entries = child[name].split(",")
        assert ".ts.net" in entries and "code.hyprial.com" in entries, name
    assert "DEEPSEEK_API_KEY" not in child


def test_seam_3_negative_control_ambient_proxies_are_really_removed(seam) -> None:
    child, _launch = seam("deepseek")
    # Removal is real only if the daemon's environment carried every name.
    assert len(ALL_EIGHT) == 8 and all(AMBIENT.get(name) for name in ALL_EIGHT)
    assert not {name for name in ALL_EIGHT if name in child}


@pytest.mark.parametrize("vendor", ["deepseek", "openai"])
def test_seam_4_cross_term_delta_adds_no_proxy_and_is_not_dropped(seam, vendor) -> None:
    child, launch = seam(vendor)
    # The provider delta adds no proxy name: the child's proxy view equals
    # the composed launch's, name for name.
    assert {n: child.get(n) for n in ALL_EIGHT} == {n: launch.get(n) for n in ALL_EIGHT}
    # The composition does not drop the delta: the key the launch lacks
    # (no grant) reaches the deepseek child.
    if vendor == "deepseek":
        assert "DEEPSEEK_API_KEY" not in launch
        assert child["DEEPSEEK_API_KEY"] == HOME_KEY
