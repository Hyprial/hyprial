"""mock-model-harness-e2e (formerly hosted-agent-e2e) runs in a Linux container with the Mac's guarantees rebuilt.

2026-10-04 (Allen): the job moved off ``macos-host``. It needs no credentials
and no macOS API; it ran there only for the host-installed Pi and a dedicated
disk. The container must still provide what the scenarios rely on:

1. Pi at the pinned version, in ``/opt/homebrew/bin`` together with Node --
   the scenarios run Pi under a hermetic PATH that adds exactly that
   directory, so widening their PATH is not an option;
2. a fresh run root from ``mktemp -d /tmp/hyprial-hosted-ci-...`` and a
   socket-length check for every daemon socket under it;
3. a *stand-in production daemon*: every scenario pings the "production"
   daemon before and after and is INCONCLUSIVE unless its pid/epoch are
   unchanged. The container has no production daemon, so one is started at
   ``$HOME/.hyprial`` BEFORE the suite and stopped in an ``always()`` step
   AFTER it, and the suite is pointed at exactly that socket;
4. the evidence leaves the disposable container as an artifact;
5. pinned, checksummed fd and ripgrep are on the hermetic PATH before the
   loopback gate: Pi waits for their downloads before session registration;
6. E2E-031 runs with ``--harness pi`` after the Pi install (2026-11-20):
   without the flag the runner exits 0 with GC-11 NOT_RUN, so a dropped flag
   would turn the one real group-chat harness check into a silent skip.

Mutation checks prove the reader rejects the regressions it exists to catch.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[4] / ".github/workflows/mock-model-harness-e2e.yml"
PI = "@earendil-works/pi-coding-agent@0.85.1"
CODEX = "@openai/codex@0.164.0"
CLAUDE = "@anthropic-ai/claude-code@2.1.299"
LOOPBACK = "contract/e2e/run-real-harness-loopback.py"
GROUP = "contract/e2e/run-group-conversation.py"
STANDIN_SOCKET = '"${HOME}/.hyprial/state/daemon.sock"'
TOOLS_STEP = "Install pinned Pi startup tools"


def _startup_tool_violations(steps: list[dict], loopback: int | None) -> list[str]:
    index = _index(steps, lambda _, step: step.get("name") == TOOLS_STEP)
    if index is None:
        return ["pinned fd/ripgrep startup tools are not installed"]
    problems = []
    run = str(steps[index].get("run") or "")
    if loopback is not None and index >= loopback:
        problems.append("fd/ripgrep installation must precede the loopback gate")
    for required in (
        'fd_version=10.3.0',
        'rg_version=15.1.0',
        'aarch64-unknown-linux-gnu',
        'x86_64-unknown-linux-musl',
        'curl --fail --location --retry 2 --connect-timeout 10 --max-time 60',
        'sha256sum --check --status',
        'install -m 0755',
        '"/opt/homebrew/bin/$binary"',
        'test "$(PATH=/opt/homebrew/bin:/usr/bin:/bin fd --version)" = "fd $fd_version"',
        'rg_line="$(PATH=/opt/homebrew/bin:/usr/bin:/bin rg --version)"',
        'case "$rg_line" in "ripgrep $rg_version"|"ripgrep $rg_version "*) ;;',
    ):
        if required not in run:
            problems.append(f"fd/ripgrep prerequisite missing: {required}")
    # Fixed upstream SHA-256 pins, not checksums fetched beside the archives.
    for digest in (
        '66f297e404400a3358e9a0c0b2f3f4725956e7e4435427a9ae56e22adbe73a68',
        '2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e',
        '2b6bfaae8c48f12050813c2ffe1884c61ea26e750d803df9c9114550a314cd14',
        '1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599',
    ):
        if digest not in run:
            problems.append("fd/ripgrep archive checksum pin missing")
    verify, extract, install = (run.find(token) for token in (
        'sha256sum --check --status', 'tar -xzf', 'install -m 0755'
    ))
    if not 0 <= verify < extract < install:
        problems.append("fd/ripgrep archives must be verified before extraction and installation")
    if steps[index].get("timeout-minutes") != 8:
        problems.append("fd/ripgrep provisioning must have an eight-minute step bound")
    return problems


def _job(text: str) -> dict:
    return (yaml.safe_load(text).get("jobs") or {}).get("hosted") or {}


def _index(steps: list[dict], predicate) -> int | None:
    return next((i for i, step in enumerate(steps) if predicate(str(step.get("run") or ""), step)), None)


def violations_in(text: str) -> list[str]:
    job = _job(text)
    steps = list(job.get("steps") or [])
    problems: list[str] = []
    if job.get("runs-on") != "ubuntu-latest" or not (job.get("container") or {}).get("image", "").startswith(
        "docker.io/library/node:24"
    ):
        problems.append("hosted job is not an ubuntu-latest node:24 container")
    if "/Volumes/" in text:
        problems.append("hosted job still refers to a macOS volume")

    pi = _index(steps, lambda run, _: PI in run)
    if pi is None:
        problems.append(f"Pi is not installed at the pinned {PI}")
    else:
        run = str(steps[pi]["run"])
        if "--prefix /opt/homebrew" not in run or "/opt/homebrew/bin/node" not in run:
            problems.append("Pi and Node are not provided in /opt/homebrew/bin (the scenarios' hermetic PATH)")
        if '= "0.85.1"' not in run:
            problems.append("the installed Pi version is not asserted")
        if "--prefer-offline" not in run:
            problems.append("the Pi install does not read the warm npm cache (--prefer-offline)")
        # 2026-10-08 follow-up to #1261: #1260's run 3 hung ~6 min in this
        # install; a socket that never errors never trips the fetch retries,
        # so each attempt is bounded by timeout -k 10 150 (SIGKILL 10 s
        # after TERM -- review 1170 on #1264) and only 3 may run.
        if "timeout -k 10 150 npm install -g --prefix /opt/homebrew" not in run:
            problems.append("the Pi install does not run each attempt under timeout -k 10 150")
        if "for attempt in 1 2 3; do" not in run or 'if [ "$attempt" -eq 3 ]; then' not in run:
            problems.append("the Pi install has no bounded 3-attempt retry that fails after the third")
        if "sleep 10" not in run:
            problems.append("the Pi install retries without a sleep between attempts")
        if steps[pi].get("timeout-minutes") != 9:
            problems.append("the Pi install timeout-minutes does not cover 3 x (150+10) + 2 x 10 plus the assertion")

    real_harness_install = _index(
        steps, lambda run, _: CODEX in run and CLAUDE in run
    )
    if real_harness_install is None:
        problems.append("the PR gate does not install pinned Codex and Claude binaries")
    else:
        install_run = str(steps[real_harness_install]["run"])
        for version in ("codex-cli 0.164.0", "2.1.299 (Claude Code)"):
            if version not in install_run:
                problems.append(f"the real harness install does not assert {version}")
        if 'echo "/opt/homebrew/bin" >> "$GITHUB_PATH"' not in install_run:
            problems.append("the installed real harnesses are not published on PATH")
        # Runs 26664/26926: a warm cache whose metadata predates the pinned
        # release answers ETARGET on every --prefer-offline retry.
        if "cache_mode=--prefer-offline" not in install_run or "cache_mode=--prefer-online" not in install_run:
            problems.append("the real harness install never revalidates stale npm metadata on retry")
        elif install_run.index("cache_mode=--prefer-online") < install_run.index("for attempt in 1 2 3; do"):
            problems.append("the real harness install skips the warm cache on its first attempt")

    loopback = _index(steps, lambda run, _: LOOPBACK in run)
    if loopback is None:
        problems.append("the hosted PR job does not run the real-harness loopback gate")
    elif steps[loopback].get("timeout-minutes") != 15:
        problems.append(
            "the real-harness loopback gate needs 15 minutes: "
            "600s SDK pre-warm + 300s for the five turns and overhead"
        )
    problems.extend(_startup_tool_violations(steps, loopback))
    group = _index(steps, lambda run, _: GROUP in run)
    if group is None or "--harness pi" not in str(steps[group]["run"]):
        problems.append("the hosted job does not run E2E-031 with --harness pi (GC-11)")
    elif pi is None or group < pi:
        problems.append("E2E-031 must run after the pinned Pi install")
    if 'branches: [dev, "intg/*"]' not in text:
        problems.append("the hosted PR gate does not cover dev and intg/*")
    for path in (
        LOOPBACK,
        "contract/e2e/_daemon_runner.py",
        "contract/e2e/_fake_provider.py",
        GROUP,
        "contract/e2e/_group_conversation/**",
    ):
        if text.count(f'- "{path}"') < 2:
            problems.append(f"the hosted workflow path filters omit {path}")

    # 2026-10-08: the container job shares gate-python.yml's hb-ci-cache volume, so
    # the SDK download must use the persistent npm cache plus fetch retries
    # instead of dying on the first registry blip (#1260).
    npm_cache = _index(steps, lambda run, _: 'npm_config_cache=$CI_CACHE_ROOT/npm' in run)
    if npm_cache is None:
        problems.append("the Pi install has no persistent npm cache under CI_CACHE_ROOT")
    else:
        cache_run = str(steps[npm_cache]["run"])
        if "CI_CACHE_ROOT unset" not in cache_run:
            problems.append("the npm cache step has no explicit cold fallback echo")
        for setting in ("npm_config_fetch_retries=5",
                        "npm_config_fetch_retry_mintimeout=10000",
                        "npm_config_fetch_retry_maxtimeout=120000"):
            if setting not in cache_run:
                problems.append(f"the npm cache step is missing the fetch retry {setting}")
        if "npm_config_fetch_timeout=60000" not in cache_run:
            problems.append("the npm cache step is missing the fetch timeout npm_config_fetch_timeout=60000")
        if pi is not None and npm_cache > pi:
            problems.append("the npm cache step must precede the Pi install")

    root = _index(steps, lambda run, _: "mktemp -d /tmp/hyprial-hosted-ci-XXXXXX" in run)
    if root is None:
        problems.append("no fresh run root from mktemp -d /tmp/hyprial-hosted-ci-XXXXXX")
    if _index(steps, lambda run, _: "sun_path" in run and "guard.sock" in run) is None:
        problems.append("no step asserts guard socket paths against the sun_path limit")

    start = _index(steps, lambda run, _: "hyprial init" in run and ".hyprial" in run)
    suite = _index(steps, lambda run, _: "run-hosted-suite.py" in run)
    stop = _index(steps, lambda run, _: "daemon stop" in run and ".hyprial" in run)
    if start is None:
        problems.append("no stand-in production daemon is started")
    if suite is None:
        problems.append("the hosted suite does not run")
    elif STANDIN_SOCKET not in str(steps[suite]["run"]):
        problems.append("the suite is not pointed at the stand-in production socket")
    if start is not None and suite is not None and start > suite:
        problems.append("the stand-in production daemon starts after the suite")
    if stop is None or (suite is not None and stop < suite) or steps[stop].get("if") != "always()":
        problems.append("the stand-in production daemon is not stopped in an always() step after the suite")
    if _index(steps, lambda _, step: "hosted-evidence" in str((step.get("with") or {}).get("name", ""))
              and step.get("if") == "always()") is None:
        problems.append("the evidence is not uploaded with if: always()")
    return problems


def test_hosted_agent_e2e_runs_in_a_container_with_the_mac_guarantees_rebuilt() -> None:
    assert violations_in(WORKFLOW.read_text(encoding="utf-8")) == []


def _cut_step(text: str, name: str) -> str:
    start = text.index(f"      - name: {name}")
    end = text.index("      - name:", start + 1)
    return text[:start] + text[end:]


def test_mutation_missing_pi_startup_tools_is_rejected() -> None:
    mutated = _cut_step(WORKFLOW.read_text(encoding="utf-8"), TOOLS_STEP)
    assert any("startup tools are not installed" in p for p in violations_in(mutated))


def test_mutation_pi_startup_tools_without_checksum_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace("sha256sum --check --status", "true")
    assert any("verified before extraction" in p for p in violations_in(mutated))


def test_mutation_pi_startup_tool_checksum_pin_removed_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        "66f297e404400a3358e9a0c0b2f3f4725956e7e4435427a9ae56e22adbe73a68", ""
    )
    assert any("checksum pin missing" in p for p in violations_in(mutated))


def test_mutation_pi_startup_tool_presence_not_asserted_is_rejected() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for tool in ("fd", "rg"):
        mutated = text.replace(f"{tool} --version", f"echo {tool}-unchecked")
        assert any(f"{tool} --version" in p for p in violations_in(mutated))


def test_mutation_pi_startup_tools_after_loopback_is_rejected() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = document["jobs"]["hosted"]["steps"]
    tools = next(step for step in steps if step.get("name") == TOOLS_STEP)
    steps.remove(tools)
    steps.append(tools)
    assert any("must precede" in p for p in violations_in(yaml.safe_dump(document)))


def test_mutation_group_scenario_without_the_harness_flag_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(f"{GROUP} --harness pi", GROUP)
    assert any("--harness pi" in p for p in violations_in(mutated))


def test_mutation_back_on_the_mac_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace("runs-on: ubuntu-latest", "runs-on: macos-host")
    assert any("container" in p for p in violations_in(mutated))


def test_mutation_unpinned_pi_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(PI, "@earendil-works/pi-coding-agent@latest")
    assert any("pinned" in p for p in violations_in(mutated))


def test_mutation_cold_npm_cache_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        'echo "npm_config_cache=$CI_CACHE_ROOT/npm" >> "$GITHUB_ENV"', ""
    )
    assert any("persistent npm cache" in p for p in violations_in(mutated))


def test_mutation_without_npm_fetch_retries_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        'echo "npm_config_fetch_retries=5" >> "$GITHUB_ENV"', ""
    )
    assert any("fetch retry" in p for p in violations_in(mutated))


def test_mutation_without_npm_fetch_timeout_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        'echo "npm_config_fetch_timeout=60000" >> "$GITHUB_ENV"', ""
    )
    assert any("fetch timeout" in p for p in violations_in(mutated))


def test_mutation_unbounded_pi_install_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        "timeout -k 10 150 npm install -g --prefix /opt/homebrew", "npm install -g --prefix /opt/homebrew"
    )
    assert any("timeout -k 10 150" in p for p in violations_in(mutated))


def test_mutation_pi_install_without_the_kill_timer_is_rejected() -> None:
    # Review 1170 on #1264: TERM alone does not end an npm that ignores it;
    # the -k 10 SIGKILL is what keeps the attempt inside its budget.
    mutated = WORKFLOW.read_text(encoding="utf-8").replace("timeout -k 10 150", "timeout 150")
    assert any("timeout -k 10 150" in p for p in violations_in(mutated))


def test_mutation_pi_step_timeout_back_at_8_is_rejected() -> None:
    # 3 x (150 + 10) + 2 x 10 = 500 s plus the symlink and version assertion
    # no longer fits in 8 minutes once every attempt carries the kill timer.
    mutated = WORKFLOW.read_text(encoding="utf-8").replace("timeout-minutes: 9", "timeout-minutes: 8")
    assert any("timeout-minutes" in p for p in violations_in(mutated))


def test_mutation_loopback_step_timeout_back_at_8_is_rejected() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert violations_in(text) == []
    mutated = text.replace(
        "- name: Real harness local-provider turn gate\n        timeout-minutes: 15",
        "- name: Real harness local-provider turn gate\n        timeout-minutes: 8",
    )
    assert mutated != text
    assert any("600s SDK pre-warm + 300s" in p for p in violations_in(mutated))


def test_mutation_pi_retry_without_fail_after_the_third_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        'if [ "$attempt" -eq 3 ]; then', 'if false; then'
    )
    assert any("3-attempt" in p for p in violations_in(mutated))


def test_mutation_pi_outside_the_hermetic_path_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace("--prefix /opt/homebrew ", "")
    assert any("hermetic PATH" in p for p in violations_in(mutated))


def test_mutation_without_the_stand_in_is_rejected() -> None:
    mutated = _cut_step(WORKFLOW.read_text(encoding="utf-8"), "Start the stand-in production daemon")
    assert any("stand-in production daemon is started" in p for p in violations_in(mutated))


def test_mutation_suite_watching_another_socket_is_rejected() -> None:
    mutated = WORKFLOW.read_text(encoding="utf-8").replace(
        f"--production-socket {STANDIN_SOCKET}", '--production-socket "/tmp/elsewhere.sock"'
    )
    assert any("stand-in production socket" in p for p in violations_in(mutated))


def test_mutation_stand_in_left_running_is_rejected() -> None:
    mutated = _cut_step(WORKFLOW.read_text(encoding="utf-8"), "Stop the stand-in production daemon")
    assert any("not stopped" in p for p in violations_in(mutated))


def test_mutation_harness_install_without_online_retry_is_rejected() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert violations_in(text) == []
    mutated = text.replace("            cache_mode=--prefer-online\n", "")
    assert any("revalidates stale npm metadata" in p for p in violations_in(mutated))
