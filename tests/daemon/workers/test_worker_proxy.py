"""hyprial's own worker proxy (``workerProxy``) and the all_proxy fallback.

Incident 2026-09-25: a daemon restarted from a shell with only ``all_proxy``
handed codex/pi workers no usable proxy.  Allen's ruling: a hyprial setting
of its own, applied per model vendor -- only OpenAI/Anthropic workers get
the proxy, every other vendor's worker gets NO proxy variable, and with no
setting the ambient behaviour stays plus the ``all_proxy`` derivation.

Every environment here is composed, never spawned: no network is touched.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import (
    NO_PROXY_ENVIRONMENT_NAMES,
    PROXY_ENVIRONMENT_NAMES,
    compose_worker_child_launch,
    derived_proxy_environment,
    whitelist_replacement_environment,
)
from hyprial.identity.impl.agents.state.worker_proxy import (
    DEFAULT_WORKER_PROXY_VENDORS,
    WORKER_PROXY_SETTINGS_INVALID,
    WORKER_PROXY_URL_INVALID,
    WorkerProxyError,
    WorkerProxySetting,
    ambient_proxy_absent,
    model_vendor_family,
    read_worker_proxy,
    write_worker_proxy_field,
)
from hyprial.daemon.impl.application import (
    DaemonApplication,
    compose_daemon_worker_launch,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel import TIERS
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

SETTING_PROXY = "http://127.0.0.1:7896"
AMBIENT_PROXY = "http://10.9.9.9:3128"
KEEP_DIRECT = ".ts.net,code.hyprial.com,127.0.0.1,localhost"
ALL_SIX_AMBIENT = {name: AMBIENT_PROXY for name in PROXY_ENVIRONMENT_NAMES}
ALL_NAMES = (*PROXY_ENVIRONMENT_NAMES, *NO_PROXY_ENVIRONMENT_NAMES)


# -- fixtures ---------------------------------------------------------------


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _settings(home: Path, record: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(record), encoding="utf-8")


_OPEN_REGISTRIES: dict[Path, AgentRegistry] = {}


def _registry(tmp_path: Path) -> AgentRegistry:
    """One registry per test home: the agent is created once, closed after."""

    registry = _OPEN_REGISTRIES.get(tmp_path)
    if registry is None:
        registry = AgentRegistry(
            tmp_path / "state" / "agents.sqlite3",
            owner="new-owner",
            machine="proxy-node",
            hyprial_home=_home(tmp_path),
        )
        registry.create("worker")
        _OPEN_REGISTRIES[tmp_path] = registry
    return registry


@pytest.fixture(autouse=True)
def _close_registries():
    yield
    while _OPEN_REGISTRIES:
        _OPEN_REGISTRIES.popitem()[1].close()


def _channel(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return build_worker_channel(
        actor="agent:proxy:node:worker",
        session_ref="sess-1",
        hyprial_home=_home(tmp_path),
        state_dir=state,
        node_id="proxy-node",
        owner="new-owner",
    )


def _spec(harness: str, model_vendor: str | None) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name="worker",
        headless=True,
        model_provider=model_vendor,
    )


def _daemon_env(tmp_path: Path, spec: HarnessLaunchSpec, environ: dict) -> dict:
    """The environment the daemon's factory composes for one launch."""

    launch = compose_daemon_worker_launch(
        registry=_registry(tmp_path),
        hyprial_home=_home(tmp_path),
        spec=spec,
        channel=_channel(tmp_path),
        environ={"PATH": "/usr/bin:/bin", **environ},
    )
    return launch.environment.for_exec()


def _compose_env(tmp_path: Path, environ: dict) -> dict:
    return compose_worker_child_launch(
        registry=_registry(tmp_path),
        hyprial_home=_home(tmp_path),
        channel=_channel(tmp_path),
        environ={"PATH": "/usr/bin:/bin", **environ},
        agent_name="worker",
    ).environment.for_exec()


def _whitelist_env(_tmp_path: Path, environ: dict) -> dict:
    return whitelist_replacement_environment({"PATH": "/usr/bin:/bin", **environ})


COMPOSITIONS = pytest.mark.parametrize(
    "compose", [_compose_env, _whitelist_env], ids=["compose", "whitelist"]
)


def _proxy_view(environment: dict) -> dict:
    return {name: environment[name] for name in ALL_NAMES if name in environment}


# -- derived_proxy_environment (the no-setting fallback) ----------------------


def test_an_http_all_proxy_alone_fills_the_four_scheme_names() -> None:
    assert derived_proxy_environment({"all_proxy": AMBIENT_PROXY}) == {
        "HTTPS_PROXY": AMBIENT_PROXY,
        "https_proxy": AMBIENT_PROXY,
        "HTTP_PROXY": AMBIENT_PROXY,
        "http_proxy": AMBIENT_PROXY,
    }
    assert derived_proxy_environment({"ALL_PROXY": AMBIENT_PROXY}) == {
        name: AMBIENT_PROXY
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    }


def test_an_explicit_https_proxy_derives_nothing() -> None:
    assert (
        derived_proxy_environment(
            {"all_proxy": AMBIENT_PROXY, "https_proxy": "http://explicit:1"}
        )
        == {}
    )


@COMPOSITIONS
def test_a_socks_all_proxy_derives_nothing_but_passes_through(tmp_path, compose) -> None:
    socks = "socks5://127.0.0.1:7897"
    assert derived_proxy_environment({"all_proxy": socks}) == {}
    assert _proxy_view(compose(tmp_path, {"all_proxy": socks})) == {"all_proxy": socks}


@COMPOSITIONS
def test_incident_regression_all_proxy_alone_reaches_the_worker(tmp_path, compose) -> None:
    """No setting, daemon env holding only all_proxy=http://... (13:34Z)."""

    environment = compose(tmp_path, {"all_proxy": AMBIENT_PROXY})

    assert environment["HTTPS_PROXY"] == AMBIENT_PROXY
    assert environment["https_proxy"] == AMBIENT_PROXY
    assert environment["HTTP_PROXY"] == AMBIENT_PROXY
    assert environment["http_proxy"] == AMBIENT_PROXY
    assert environment["all_proxy"] == AMBIENT_PROXY


@COMPOSITIONS
def test_an_explicit_scheme_proxy_is_never_mixed_with_a_derived_one(
    tmp_path, compose
) -> None:
    environment = compose(
        tmp_path, {"all_proxy": AMBIENT_PROXY, "HTTPS_PROXY": "http://explicit:1"}
    )
    assert _proxy_view(environment) == {
        "all_proxy": AMBIENT_PROXY,
        "HTTPS_PROXY": "http://explicit:1",
    }


# -- classification -----------------------------------------------------------

#: Every (harness, model vendor) the matrix declares, with the family it must
#: classify to.  A new TIERS entry missing here fails the test below instead
#: of silently going direct under the setting.
EXPECTED_MATRIX_FAMILIES = {
    ("codex", None): "openai",
    ("claude", None): "anthropic",
    ("pi", "openai-codex"): "openai",
    ("pi", "deepseek"): "deepseek",
    ("pi", "zai-coding-cn"): "zai-coding-cn",
    ("pi", "kimi-coding"): "kimi-coding",
}


def test_every_matrix_candidate_classifies() -> None:
    declared = {
        (candidate.harness, candidate.provider)
        for pool in TIERS.values()
        for candidate in pool
    }
    unlisted = declared - set(EXPECTED_MATRIX_FAMILIES)
    assert not unlisted, f"classify these new matrix entries: {sorted(unlisted, key=str)}"
    for harness, vendor in sorted(declared, key=str):
        family = model_vendor_family(harness, vendor)
        assert family, (harness, vendor)
        assert family == EXPECTED_MATRIX_FAMILIES[(harness, vendor)], (harness, vendor)


@pytest.mark.parametrize(
    ("harness", "vendor", "family"),
    [
        ("pi", "openai", "openai"),
        ("pi", "openai-codex-plus", "openai"),
        ("pi", "anthropic", "anthropic"),
        ("pi", "anthropic-vertex", "anthropic"),
        ("pi", "OpenAI-Codex", "openai"),
        ("codex", "openai", "openai"),
        ("codex", "deepseek", "deepseek"),
        ("claude", "anthropic", "anthropic"),
        ("claude", "deepseek", "deepseek"),
        # Unknowable: pi on its own default, harnesses that are not matrix
        # models.  The caller keeps the ambient behaviour for these.
        ("pi", None, None),
        ("jev", None, None),
        ("dsh", None, None),
    ],
)
def test_classification_rules(harness, vendor, family) -> None:
    assert model_vendor_family(harness, vendor) == family


# -- the daemon's launch composition, with the setting ------------------------

LISTED = pytest.mark.parametrize(
    ("harness", "vendor"),
    [("codex", None), ("claude", None), ("pi", "openai-codex")],
    ids=["codex-native", "claude-native", "pi-openai-codex"],
)
UNLISTED = pytest.mark.parametrize(
    ("harness", "vendor"),
    [("pi", "deepseek"), ("pi", "kimi-coding"), ("pi", "zai-coding-cn")],
    ids=["deepseek", "kimi", "zai"],
)


@LISTED
def test_a_listed_vendor_worker_gets_the_setting_proxy(tmp_path, harness, vendor) -> None:
    _settings(_home(tmp_path), {"workerProxy": {"url": SETTING_PROXY}})

    environment = _daemon_env(
        tmp_path,
        _spec(harness, vendor),
        {**ALL_SIX_AMBIENT, "NO_PROXY": KEEP_DIRECT},
    )

    for name in PROXY_ENVIRONMENT_NAMES:
        assert environment[name] == SETTING_PROXY, name


@LISTED
def test_a_listed_vendor_keeps_the_configured_no_proxy(tmp_path, harness, vendor) -> None:
    _settings(
        _home(tmp_path),
        {"workerProxy": {"url": SETTING_PROXY, "noProxy": KEEP_DIRECT}},
    )

    environment = _daemon_env(
        tmp_path, _spec(harness, vendor), {"NO_PROXY": "unrelated.example"}
    )

    for name in NO_PROXY_ENVIRONMENT_NAMES:
        assert ".ts.net" in environment[name].split(","), name
        assert "code.hyprial.com" in environment[name].split(","), name
        assert environment[name] == KEEP_DIRECT


@LISTED
@pytest.mark.parametrize("ambient_name", NO_PROXY_ENVIRONMENT_NAMES)
def test_a_listed_vendor_falls_back_to_the_ambient_no_proxy(
    tmp_path, harness, vendor, ambient_name
) -> None:
    _settings(_home(tmp_path), {"workerProxy": {"url": SETTING_PROXY}})

    environment = _daemon_env(
        tmp_path, _spec(harness, vendor), {ambient_name: KEEP_DIRECT}
    )

    # Both spellings, whichever one the daemon had: the proxy was set in
    # both cases, so a client reading either must also see the exclusions.
    for name in NO_PROXY_ENVIRONMENT_NAMES:
        assert environment[name] == KEEP_DIRECT, name


@UNLISTED
@pytest.mark.parametrize(
    "ambient",
    [
        {**ALL_SIX_AMBIENT, "NO_PROXY": KEEP_DIRECT, "no_proxy": KEEP_DIRECT},
        {"all_proxy": AMBIENT_PROXY},
    ],
    ids=["all-six-ambient", "all-proxy-only"],
)
def test_an_unlisted_vendor_worker_has_no_proxy_variables(
    tmp_path, harness, vendor, ambient
) -> None:
    _settings(_home(tmp_path), {"workerProxy": {"url": SETTING_PROXY}})

    environment = _daemon_env(tmp_path, _spec(harness, vendor), ambient)

    assert _proxy_view(environment) == {}
    assert environment["PATH"] == "/usr/bin:/bin"


def test_vendors_decide_who_is_proxied(tmp_path) -> None:
    _settings(
        _home(tmp_path),
        {"workerProxy": {"url": SETTING_PROXY, "vendors": ["deepseek"]}},
    )

    deepseek = _daemon_env(tmp_path, _spec("pi", "deepseek"), ALL_SIX_AMBIENT)
    codex = _daemon_env(tmp_path, _spec("codex", None), ALL_SIX_AMBIENT)

    assert deepseek["HTTPS_PROXY"] == SETTING_PROXY
    assert _proxy_view(codex) == {}


def test_an_unknowable_vendor_keeps_the_ambient_behaviour(tmp_path) -> None:
    _settings(_home(tmp_path), {"workerProxy": {"url": SETTING_PROXY}})

    environment = _daemon_env(tmp_path, _spec("pi", None), {"all_proxy": AMBIENT_PROXY})

    assert environment["HTTPS_PROXY"] == AMBIENT_PROXY
    assert environment["all_proxy"] == AMBIENT_PROXY


@pytest.mark.parametrize(
    ("harness", "vendor"),
    [("codex", None), ("pi", "deepseek"), ("pi", None)],
)
@pytest.mark.parametrize(
    "ambient",
    [
        {},
        {"all_proxy": AMBIENT_PROXY},
        {"all_proxy": "socks5://127.0.0.1:1"},
        {**ALL_SIX_AMBIENT, "NO_PROXY": KEEP_DIRECT},
        {"https_proxy": AMBIENT_PROXY, "no_proxy": KEEP_DIRECT},
    ],
    ids=["none", "all-proxy", "socks", "all-six", "lower-https"],
)
def test_no_setting_is_todays_composition_byte_for_byte(
    tmp_path, harness, vendor, ambient
) -> None:
    """The daemon factory with no setting == the plain composition."""

    assert _daemon_env(tmp_path, _spec(harness, vendor), ambient) == _compose_env(
        tmp_path, ambient
    )


def test_a_setting_change_applies_to_the_next_launch(tmp_path) -> None:
    spec = _spec("pi", "deepseek")
    before = _daemon_env(tmp_path, spec, ALL_SIX_AMBIENT)
    write_worker_proxy_field("url", SETTING_PROXY, _home(tmp_path))
    after = _daemon_env(tmp_path, spec, ALL_SIX_AMBIENT)
    write_worker_proxy_field("url", "", _home(tmp_path))
    cleared = _daemon_env(tmp_path, spec, ALL_SIX_AMBIENT)

    assert before["HTTPS_PROXY"] == AMBIENT_PROXY
    assert _proxy_view(after) == {}
    assert cleared == before


def test_a_damaged_setting_fails_the_launch_loudly(tmp_path) -> None:
    _settings(_home(tmp_path), {"workerProxy": {"url": "socks5://127.0.0.1:1"}})

    with pytest.raises(WorkerProxyError) as caught:
        _daemon_env(tmp_path, _spec("codex", None), {})

    assert caught.value.code == WORKER_PROXY_SETTINGS_INVALID


# -- persistence and the CLI ----------------------------------------------------


def test_the_setting_reads_back_with_defaults(tmp_path) -> None:
    home = _home(tmp_path)
    assert read_worker_proxy(home) is None
    _settings(home, {"workerProxy": {"vendors": ["openai"]}})
    assert read_worker_proxy(home) is None  # no url: not configured
    _settings(home, {"workerProxy": {"url": SETTING_PROXY}})
    assert read_worker_proxy(home) == WorkerProxySetting(
        SETTING_PROXY, DEFAULT_WORKER_PROXY_VENDORS, None
    )


def test_the_writer_keeps_unrelated_settings(tmp_path) -> None:
    home = _home(tmp_path)
    _settings(home, {"owner": "o", "forwarding": {"mode": "off"}})

    write_worker_proxy_field("url", SETTING_PROXY, home)
    write_worker_proxy_field("vendors", "OpenAI, anthropic,deepseek", home)
    write_worker_proxy_field("noProxy", KEEP_DIRECT, home)

    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {
        "owner": "o",
        "forwarding": {"mode": "off"},
        "workerProxy": {
            "url": SETTING_PROXY,
            "vendors": ["openai", "anthropic", "deepseek"],
            "noProxy": KEEP_DIRECT,
        },
    }


@pytest.mark.parametrize(
    "bad",
    ["socks5://127.0.0.1:7897", "ftp://proxy:21", "127.0.0.1:7896", "http://", "http://h:x"],
)
def test_a_bad_url_is_refused_with_a_named_error(tmp_path, bad) -> None:
    home = _home(tmp_path)
    _settings(home, {"owner": "o"})

    with pytest.raises(WorkerProxyError) as caught:
        write_worker_proxy_field("url", bad, home)

    assert caught.value.code == WORKER_PROXY_URL_INVALID
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {
        "owner": "o"
    }


def test_config_set_worker_proxy_round_trip(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    import hyprial.shell.impl.cli.runtime.entry as cli

    home = _home(tmp_path)
    _settings(home, {"owner": "o"})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    def config_set(key: str, value: str):
        return CliRunner().invoke(cli.app, ["config", "set", key, value, "--json"])

    result = config_set("workerProxy.url", SETTING_PROXY)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["workerProxy"] == {
        "url": SETTING_PROXY,
        "vendors": ["openai", "anthropic"],
        "noProxy": None,
    }
    assert payload["appliesOn"] == "next worker launch"

    assert config_set("workerProxy.vendors", "openai").exit_code == 0
    assert config_set("workerProxy.noProxy", KEEP_DIRECT).exit_code == 0
    assert read_worker_proxy(home) == WorkerProxySetting(
        SETTING_PROXY, ("openai",), KEEP_DIRECT
    )

    bad = config_set("workerProxy.url", "socks5://127.0.0.1:7897")
    assert bad.exit_code != 0
    assert "INVALID_CONFIGURATION" in bad.output
    assert WORKER_PROXY_URL_INVALID in bad.output
    assert read_worker_proxy(home) == WorkerProxySetting(
        SETTING_PROXY, ("openai",), KEEP_DIRECT
    )

    cleared = config_set("workerProxy.url", "")
    assert cleared.exit_code == 0, cleared.output
    assert json.loads(cleared.output)["workerProxy"] is None
    assert read_worker_proxy(home) is None
    assert "workerProxy" not in json.loads(
        (home / "settings.json").read_text(encoding="utf-8")
    )


# -- startup warning and the read surface ----------------------------------------


def test_absent_means_no_setting_and_no_daemon_proxy() -> None:
    setting = WorkerProxySetting(SETTING_PROXY)
    assert ambient_proxy_absent(None, {}) is True
    assert ambient_proxy_absent(None, {"NO_PROXY": KEEP_DIRECT}) is True
    assert ambient_proxy_absent(setting, {}) is False
    for name in PROXY_ENVIRONMENT_NAMES:
        assert ambient_proxy_absent(None, {name: AMBIENT_PROXY}) is False, name


def _fake_daemon(home: Path) -> tuple[SimpleNamespace, list[tuple]]:
    lines: list[tuple] = []
    fake = SimpleNamespace(
        hyprial_home=home,
        _log=lambda level, component, event, **fields: lines.append(
            (level, component, event)
        ),
    )
    return fake, lines


@pytest.mark.parametrize(
    ("record", "environ", "expected"),
    [
        (None, {}, [("warn", "daemon", "daemon.proxy.absent")]),
        (None, {"all_proxy": AMBIENT_PROXY}, []),
        ({"workerProxy": {"url": SETTING_PROXY}}, {}, []),
        (
            {"workerProxy": {"url": "socks5://x:1"}},
            {},
            [("warn", "daemon", "daemon.proxy.settings_invalid")],
        ),
    ],
    ids=["absent", "ambient", "configured", "damaged"],
)
def test_startup_warns_once_only_when_workers_will_go_direct(
    tmp_path, monkeypatch, record, environ, expected
) -> None:
    home = _home(tmp_path)
    if record is not None:
        _settings(home, record)
    for name in PROXY_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    fake, lines = _fake_daemon(home)

    DaemonApplication._warn_if_worker_proxy_absent(fake)  # type: ignore[arg-type]

    assert lines == expected


def test_status_shows_the_setting_the_next_launch_uses(tmp_path) -> None:
    home = _home(tmp_path)
    fake, _lines = _fake_daemon(home)
    status = DaemonApplication._worker_proxy_status_json

    assert status(fake) == {"configured": False}  # type: ignore[arg-type]
    _settings(
        home, {"workerProxy": {"url": "http://user:secret@127.0.0.1:7896"}}
    )
    assert status(fake) == {  # type: ignore[arg-type]
        "configured": True,
        "url": "http://user:***@127.0.0.1:7896",
        "vendors": ["openai", "anthropic"],
        "noProxy": None,
    }
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    damaged = status(fake)  # type: ignore[arg-type]
    assert damaged["configured"] is False
    assert damaged["error"] == WORKER_PROXY_SETTINGS_INVALID


def test_the_ps_payload_carries_the_worker_proxy_block(tmp_path) -> None:
    """The status method is tested above; this pins that `ps` actually exposes it."""

    from tests.daemon.delivery.origin.test_inbound_origin import _app

    app = _app(tmp_path)
    try:
        (app.hyprial_home / "settings.json").write_text(
            json.dumps({"workerProxy": {"url": "http://127.0.0.1:7896"}}),
            encoding="utf-8",
        )
        payload = app.handle("ps", {})
    finally:
        app._agent_session_domains.close()
        app._inbox.close()

    assert payload["workerProxy"]["configured"] is True
    assert payload["workerProxy"]["url"] == "http://127.0.0.1:7896"
