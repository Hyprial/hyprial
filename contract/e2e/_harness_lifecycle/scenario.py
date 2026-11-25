"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def main(_entry: dict[str, Any], /):
    parser = _entry['argparse'].ArgumentParser(description=_entry['__doc__'])
    parser.add_argument(
        "--skip-reply-bridge",
        action="store_true",
        help=(
            "classify the reply-bridge assertion group BLOCKED instead of "
            "running it; only meaningful for checkouts predating PR #110 "
            "(the bridge is on dev)"
        ),
    )
    options = parser.parse_args()

    report = _entry['Report']()
    executable = _entry['hyprial_executable']()
    available = _entry['preflight_binaries'](report)
    lark_preflight = _entry['preflight_lark'](report)
    lark_secret = lark_preflight[0] if lark_preflight is not None else None
    lark_chat_id = lark_preflight[1] if lark_preflight is not None else None
    if lark_secret is not None:
        report.forbidden.append(lark_secret)

    with _entry['tempfile'].TemporaryDirectory(
        prefix="hyprial-e2e-013-", dir=_entry['short_temp_root']()
    ) as temporary:
        root = _entry['Path'](temporary)
        # The CFGPROBE skill must exist before the pi worker launches with
        # --skill pointing at it (and it persists for the liveness phase's
        # desired-state relaunch of the same args).
        skill_dir, skill_token = _entry['write_probe_skill'](root)
        cases = {
            "claude": _entry['HarnessCase'](
                "claude", _entry['DEEPSEEK_PROVIDER'], "e2e013-claude", "e2e013c",
                (), "E2E013_CLAUDE_OK",
            ),
            "pi": _entry['HarnessCase'](
                "pi", _entry['PI_PROVIDER'], "e2e013-pi", "e2e013p",
                (
                    "--skill", str(skill_dir), "--approve",
                ),
                "E2E013_PI_OK",
            ),
            "codex": _entry['HarnessCase'](
                "codex", _entry['DEEPSEEK_PROVIDER'], "e2e013-codex", "e2e013x",
                ("-a", "never", "-s", "workspace-write"),
                "E2E013_CODEX_OK",
            ),
        }

        _entry['write_channel_config'](
            root / "home",
            include_real_lark=lark_secret is not None,
            lark_chat_id=lark_chat_id,
            lark_secret=lark_secret,
        )
        daemon = _entry['IsolatedDaemon'](executable, root, "e2e013-node")
        daemon.allow_feishu_test_apps(_entry['Path'](_entry['__file__']).name)
        try:
            daemon.env.update(_entry['deepseek_environment']())
        except _entry['DeepSeekUnavailable'] as error:
            report.add("BLOCKED", "preflight.deepseek", str(error))
            return report.exit_code()
        report.add(
            "PASS",
            "preflight.deepseek",
            f"provider={_entry['DEEPSEEK_PROVIDER']} model={_entry['DEEPSEEK_MODEL']} credential=ready",
        )
        try:
            daemon.start()
        except (AssertionError, OSError, KeyError) as error:
            report.add("FAIL", "preflight.isolated-daemon", str(error))
            return report.exit_code()
        report.add(
            "PASS",
            "preflight.isolated-daemon",
            f"HYPRIAL_HOME={root / 'home'} HARNESS_STATE_DIR={root / 'state'}",
        )
        # resolve-or-reject (Allen b): the CLI probe sender must be a
        # registered identity — no more bare node_id as sender.
        daemon.command("agent", "create", "--name", _entry['DRIVER'], "--json")

        def _down_all() -> None:
            try:
                daemon.command("agent", "stop", "--all", "--json")
            except (AssertionError, OSError):
                pass

        with _entry['teardown_guard'](
            _down_all,
            daemon.stop,
            record=lambda detail: report.add(
                "FAIL", "teardown", f"daemon teardown also raised: {detail}"
            ),
        ):
            # Phase 1: one full lifecycle per harness.
            for harness in ("claude", "pi", "codex"):
                case = cases[harness]
                if not available.get(harness, False):
                    report.block(
                        tuple(f"{harness}.{check}" for check in _entry['HARNESS_CHECKS']),
                        f"{harness} prerequisites are unavailable",
                    )
                    continue
                _entry['run_harness_case'](daemon, root, case, report)

            # Phase 1b: behavioral skill-load proof on the pi worker (the
            # one harness with an explicit --skill launch flag).
            if available.get("pi"):
                _entry['run_skill_probe_phase'](
                    daemon, root, cases["pi"], report, skill_token
                )
            else:
                report.block(_entry['SKILL_CHECKS'], "pi is unavailable")

            # Phase 1c: headless busy FIFO (E2E-008's cc TUI counterpart),
            # once each against pi and codex.
            for harness in ("pi", "codex"):
                if available.get(harness):
                    _entry['run_headless_fifo_phase'](
                        daemon, root, cases[harness], report
                    )
                else:
                    report.block(
                        _entry['fifo_checks'](harness), f"{harness} is unavailable"
                    )

            # Phase 2: A9 mid-life harness switch (claude -> pi).
            if available.get("claude") and available.get("pi"):
                _entry['run_morph_phase'](daemon, root, report)
            else:
                report.block(
                    _entry['MORPH_CHECKS'], "claude and pi are both required for the switch"
                )

            # Phase 3: same-harness model switch on the claude worker.
            if available.get("claude"):
                _entry['run_model_switch_phase'](daemon, root, cases["claude"], report)
            else:
                report.block(_entry['MODEL_SWITCH_CHECKS'], "claude is unavailable")

            # Phase 4: #106 convergence under kill -9 (single pi child left).
            if available.get("pi"):
                _entry['run_liveness_phase'](daemon, root, cases["pi"], report)
            else:
                report.block(_entry['LIVENESS_CHECKS'], "pi is unavailable")

            # Phase 5: real Lark leg pinned to the recovered pi worker.
            if lark_secret is None:
                report.block(
                    _entry['LARK_CHECKS']
                    + _entry['LARK_BRIDGE_CHECKS']
                    + _entry['LARK_AGENT_ATTACHMENT_CHECKS'],
                    "Lark prerequisites are unavailable",
                )
            elif not available.get("pi"):
                report.block(
                    _entry['LARK_CHECKS']
                    + _entry['LARK_BRIDGE_CHECKS']
                    + _entry['LARK_AGENT_ATTACHMENT_CHECKS'],
                    "the pinned pi worker is unavailable",
                )
            else:
                assert lark_chat_id is not None
                _entry['run_lark_phase'](
                    daemon,
                    root,
                    cases["pi"],
                    report,
                    chat_id=lark_chat_id,
                    skip_reply_bridge=options.skip_reply_bridge,
                )

            # Phase 6: teardown persistence — a downed worker must stay down
            # across a daemon restart while desired workers revive.
            if available.get("codex"):
                _entry['run_teardown_persistence_phase'](
                    daemon, root, cases, available, report
                )
            else:
                report.block(_entry['TEARDOWN_CHECKS'], "codex is unavailable")

    if report.secrets_clean():
        report.add("PASS", "no-secret-emitted", "no forbidden value in any line")
    else:
        report.add("FAIL", "no-secret-emitted", "a forbidden value escaped")
    return report.exit_code()
