"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
import subprocess
from pathlib import Path
def main(_entry: dict[str, Any], /):
    if len(_entry['sys'].argv) >= 2 and _entry['sys'].argv[1] == "--fake-worker":
        return _entry['fake_worker'](_entry['sys'].argv[2] if len(_entry['sys'].argv) > 2 else "unknown")
    if len(_entry['sys'].argv) >= 2 and _entry['sys'].argv[1] == "--channel-client":
        return _entry['channel_client_mode'](_entry['sys'].argv[2:])
    if len(_entry['sys'].argv) >= 2 and _entry['sys'].argv[1] == "--fake-claude":
        return _entry['fake_claude_mode'](_entry['sys'].argv[2:])

    uv = None
    git = None
    from shutil import which
    uv, git = which("uv"), which("git")
    report = _entry['Report']()
    if uv is None or git is None:
        report.add("FAIL", "prerequisites", f"missing {'uv' if uv is None else 'git'}")
        return report.exit_code()

    with _entry['tempfile'].TemporaryDirectory(
        prefix="hyprial-upgcont-",
        dir=_entry['os'].environ.get("HYPRIAL_E2E_ROOT") or _entry['short_temp_root'](),
    ) as raw:
        root = _entry['Path'](raw)
        home = root / "h"
        state = root / "s"
        tool_dir = root / "ut"
        bin_dir = root / "ub"
        cache_dir = root / "uc"
        uv_home = root / "uh"
        for directory in (home, state, tool_dir, bin_dir, cache_dir, uv_home):
            directory.mkdir(parents=True, exist_ok=True)

        host_env = dict(_entry['os'].environ)
        env = dict(_entry['os'].environ)
        env.update({
            "HOME": str(uv_home),
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "UV_CACHE_DIR": str(cache_dir),
            "UV_LINK_MODE": "copy",
            # The OLD build performs the upgrade. Historical known-limitation
            # baselines predate 9e8421a2's --compile-bytecode support; keep this
            # explicit for every case. Otherwise the upgraded daemon compiles every
            # module on its first start, inside the old CLI's 15 s restart
            # budget (measured on this Mac: 11.6 s cold vs 4.4 s precompiled at
            # load 16), and E2E-010 goes red under CI load.  uv reads this for
            # the same `uv tool install`, matching the current product.
            "UV_COMPILE_BYTECODE": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": f"{bin_dir}{_entry['os'].pathsep}{env.get('PATH', '')}",
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            # An e2e test node (refuse_unisolatable_daemon): owner "e2e", an
            # e2e- node id, and a loopback listen so the old build never
            # binds the production daemon's tailnet address on this machine.
            "HYPRIAL_OWNER": "e2e",
            "HYPRIAL_NODE_ID": _entry['NODE_ID'],
            "HYPRIAL_ZENOH_LISTEN": _entry['free_tcp_endpoint'](),
            # Discovery defaults ON in every version this scenario installs;
            # without this the throwaway daemon joined the host's production
            # mesh -- and the macos-host runner IS a production host.
            "HYPRIAL_PEER_DISCOVERY": "0",
            # Honoured by builds that know it; the older build this scenario
            # starts from predates it (listed as uncovered in the inventory).
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_LARK_WORKER_COMMAND": _entry['json'].dumps(
                [_entry['sys'].executable, str(_entry['Path'](_entry['__file__']).resolve()), "--fake-worker"]
            ),
        })
        case = _entry['os'].environ.get("HYPRIAL_E2E_UPGRADE_CASE", "same-interpreter")
        if case == "same-interpreter":
            env["UV_PYTHON"] = _entry['sys'].executable
        elif case in {"known-limitation-pre051", "known-limitation-cross-minor"}:
            env["UV_PYTHON"] = "3.14"
        else:
            raise ValueError(f"unsupported upgrade case: {case}")
        print(
            f"RESULT NOTE E2E-010 case=upgrade-continuity-{case} "
            f"initialPython={_entry['sys'].version_info.major}.{_entry['sys'].version_info.minor} "
            f"upgradePython={env['UV_PYTHON']}", flush=True,
        )
        # PYTHONPATH too: it outranks site-packages, so an inherited one makes
        # the INSTALLED old daemon import the candidate's source and the run
        # tests the candidate against itself (it happened, silently).
        for leak in ("HYPRIAL_ZENOH_CONNECT", "HARNESS_SOCKET_PATH", "PYTHONPATH"):
            env.pop(leak, None)
        # The old build predates HYPRIAL_NETWORK_ISOLATED: judged on the exact
        # dict its daemon is launched from.
        refused = _entry['refuse_unisolatable_daemon']("upgrade-continuity", env)
        if refused is not None:
            return refused

        executable = bin_dir / "hyprial"
        # uv's tool layout: the installed package's own interpreter.
        installed_python = tool_dir / "hyprial" / "bin" / "python"
        installed = False
        daemon_up = False
        old_channel: subprocess.Popen[bytes] | None = None
        old_channel_stop: Path | None = None
        old_channel_log: Path | None = None
        old_channel_ready: Path | None = None
        old_channel_phase = root / "old-channel.phase"
        new_channel: subprocess.Popen[bytes] | None = None
        new_channel_stop: Path | None = None
        new_channel_log: Path | None = None
        new_channel_ready: Path | None = None
        new_channel_phase = root / "new-channel.phase"
        try:
            _entry['write_channels'](home)
            try:
                remote, seed, candidate_revision, upgrade_from, old_revision = (
                    _entry['seed_remote'](root, env, host_env)
                )
            except _entry['NoUpgradeDelta'] as reason:
                # Nothing merged since the last dual-gate promotion: there is
                # no version boundary to cross, so there is nothing to prove
                # or disprove.  Reported, not failed -- a red here would hold
                # `nightly` back precisely because it is already current.
                report.add("PASS", "upgrade-delta", f"no upgrade to test: {reason}")
                return report.exit_code()
            report.add(
                "PASS",
                "upgrade-delta",
                f"old={upgrade_from}@{old_revision[:12]} "
                f"new=candidate@{candidate_revision[:12]}",
            )
            old_protocol = _entry['channel_protocol_version_at'](seed, old_revision, env)
            # A held-open old client stays "current" on the upgraded daemon
            # only while both trees speak one protocol; past a bump the new
            # daemon correctly classifies it as legacy and cannot vouch for it.
            old_generation_after = (
                "current"
                if old_protocol == _entry['CHANNEL_PROTOCOL_VERSION']
                else "legacy_or_unknown"
            )
            _entry['git_config'](_entry['Path'](env["GIT_CONFIG_GLOBAL"]), remote)

            # --- boot the running deployment on the OLD version ------------- #
            _entry['run'](
                uv, "tool", "install", f"git+{_entry['GIT_SOURCE']}@v{_entry['INITIAL_VERSION']}",
                "--python", _entry['sys'].executable, env=env,
            )
            installed = True
            if not executable.is_file() or not _entry['os'].access(executable, _entry['os'].X_OK):
                raise AssertionError("uv install did not create an executable hyprial")
            initial_version = _entry['hyprial'](executable, ["version", "--json"], env)
            old_version = str(initial_version["packageVersion"])
            init = _entry['hyprial'](executable, _entry['init_command'](executable, env), env)
            daemon_up = True
            daemon = _entry['init_daemon_identity'](init)
            if daemon.get("running") is not True:
                raise AssertionError(f"daemon did not boot on old version: {init}")
            old_pid = daemon["pid"]
            old_epoch = str(daemon.get("epoch") or "")
            if not old_epoch:
                raise AssertionError(f"old daemon omitted generation epoch: {init}")
            owner = daemon.get("owner")
            node_id = daemon.get("nodeId")
            channel_actor = _entry['local_channel_actor'](daemon)
            report.add("PASS", "install-boot",
                       f"old={old_version} daemon-pid={old_pid} running")

            # --- connectors online ----------------------------------------- #
            for name in _entry['ADAPTER_NAMES']:
                started = _entry['hyprial'](executable, ["adapter", "start", name, "--json"], env)
                if started.get("adapter", {}).get("status") != "online":  # type: ignore[union-attr]
                    raise AssertionError(f"{name} did not become online: {started}")
            before_adapters = _entry['wait_for_adapters'](executable, env)
            old_adapter_pids = {n: before_adapters[n].get("pid") for n in _entry['ADAPTER_NAMES']}  # type: ignore[union-attr]
            desired = _entry['read_desired_state'](state, installed_python, env)
            recorded = {(i["provider"], i["name"]) for i in desired.get("providers", [])}
            if recorded != {("lark", n) for n in _entry['ADAPTER_NAMES']}:
                raise AssertionError(f"desired state missing lark providers: {desired}")
            report.add("PASS", "connectors-online",
                       f"{len(_entry['ADAPTER_NAMES'])} adapters online, recorded in desired state")

            # --- a real OLD stdio Channel remains loaded across upgrade ---- #
            old_channel_ref = f"old-channel-{_entry['uuid4']()}"
            (
                old_channel,
                old_channel_stop,
                old_channel_log,
                old_channel_ready,
            ) = _entry['start_channel_client'](
                executable=executable,
                env=env,
                root=root,
                actor=channel_actor,
                session_ref=old_channel_ref,
                phase=old_channel_phase,
            )
            _entry['wait_for_channel_current'](
                executable,
                env,
                actor=channel_actor,
                session_ref=old_channel_ref,
                build_version=old_version,
                process=old_channel,
                error_log=old_channel_log,
                daemon_epoch=old_epoch,
                stage="old-channel-initial-register",
                protocol_version=old_protocol,
                generation="current",
                require_operational_liveness=True,
            )
            old_desired = _entry['read_desired_state'](state, installed_python, env)
            old_session = next(
                item
                for item in old_desired.get("interactiveSessions", [])
                if item.get("actor") == channel_actor
            )
            if not old_session.get("channelLeaseDigest"):
                raise AssertionError(
                    f"old-version session persisted no lease digest: {old_session}"
                )
            report.add(
                "PASS",
                "old-channel-running",
                f"old-version child registered build={old_version} "
                f"protocol={old_protocol} alive with a persisted lease",
            )
            old_tool_calls = _entry['wait_for_new_tool_call'](
                old_channel_ready,
                0,
                daemon_epoch=old_epoch,
                process=old_channel,
                error_log=old_channel_log,
                stage="old-channel-initial-tool",
            )
            old_channel_document = _entry['json'].loads(
                old_channel_ready.read_text(encoding="utf-8")
            )
            old_channel_child_pid = old_channel_document.get("childPid")
            if not isinstance(old_channel_child_pid, int):
                raise AssertionError(
                    f"old stdio child PID was not captured: {old_channel_document}"
                )

            # --- interactive session registered ---------------------------- #
            ref1 = f"sref-{_entry['uuid4']()}"
            reg1 = _entry['ipc'](state, "session.register", {
                "actor": _entry['SESSION_ACTOR'], "sessionRef": ref1,
                "command": [_entry['sys'].executable, "-c", "pass"],
                "cwd": str(root), "source": "e2e-upgrade-continuity", "runtime": "pi",
            })
            if reg1.get("registered") is not True:
                raise AssertionError(f"session did not register: {reg1}")
            who1 = _entry['ipc'](state, "identity.whoami",
                       {"actor": _entry['SESSION_ACTOR'], "sessionRef": ref1})
            if who1.get("sessionRegistered") is not True:
                raise AssertionError(f"whoami did not confirm registration: {who1}")
            # Judge `ps` by the actor the daemon itself reports, as the
            # post-upgrade check does: whether a bare name is stored verbatim
            # or canonicalized depends on the version, not on continuity.
            canonical_actor1 = who1.get("actor")
            if not isinstance(canonical_actor1, str):
                raise AssertionError(f"whoami omitted the session actor: {who1}")
            sessions = _entry['ps'](env).get("interactiveSessions", [])
            if not any(s.get("actor") == canonical_actor1 and s.get("sessionRef") == ref1
                       for s in sessions):  # type: ignore[union-attr]
                raise AssertionError(f"session not visible in ps: {sessions}")
            report.add("PASS", "session-registered",
                       f"{canonical_actor1} registered sessionRef={ref1[:14]}")

            driver_uri = f"agent:{owner}:{node_id}:{_entry['DRIVER']}"
            driver_ref = f"sref-{_entry['uuid4']()}"
            driver_reg = _entry['ipc'](state, "session.register", {
                "actor": driver_uri, "sessionRef": driver_ref,
                "command": [_entry['sys'].executable, "-c", "pass"],
                "cwd": str(root), "source": "e2e-upgrade-continuity", "runtime": "pi",
            })
            if driver_reg.get("registered") is not True:
                raise AssertionError(f"driver persona did not register: {driver_reg}")
            # Presence for a freshly registered session is event-driven:
            # LivelinessDirectory materializes even the daemon's own token
            # via an async subscription callback.  Wait for the online
            # verdict before sending, or delivery parks the probes in the
            # outbox as "target-not-visible" instead of landing them in the
            # local persona inbox.
            driver_deadline = _entry['time'].monotonic() + 20.0
            while True:
                driver_sessions = _entry['ps'](env).get(
                    "interactiveSessions", []
                )
                driver_online = next(
                    (
                        s for s in driver_sessions  # type: ignore[union-attr]
                        if s.get("actor") == driver_uri
                        and s.get("sessionRef") == driver_ref
                    ),
                    None,
                )
                if driver_online is not None and driver_online.get("online") is True:
                    break
                if _entry['time'].monotonic() > driver_deadline:
                    raise AssertionError(
                        f"driver persona never came online: {driver_sessions}"
                    )
                _entry['time'].sleep(0.2)

            # --- in-flight message A (before upgrade) ---------------------- #
            send_command = _entry['message_send_command'](executable, env)
            summary_a = _entry['message_summary_args'](executable, env, "before-upgrade")
            summary_b = _entry['message_summary_args'](executable, env, "also-before-upgrade")
            send_a = _entry['hyprial'](executable, [
                *send_command, "--from", driver_uri, "--to", driver_uri,
                *summary_a, "before-upgrade", "--json",
            ], env)
            msg_a = str(send_a["messageId"])
            send_b = _entry['hyprial'](executable, [
                *send_command, "--from", driver_uri, "--to", driver_uri,
                *summary_b, "also-before-upgrade", "--json",
            ], env)
            msg_b = str(send_b["messageId"])
            expected_summaries = {
                msg_a: "before-upgrade" if summary_a else None,
                msg_b: "also-before-upgrade" if summary_b else None,
            }
            # The pre-upgrade FIFO and deliveryId are read over IPC
            # (message.pending.list on the verbatim actor key), not from
            # `ps`: which rows `ps` pendingMessages lists has differed by
            # version, while the IPC read has meant the same on every version
            # this scenario installs.
            pre_pending = _entry['ipc'](state, "message.pending.list", {"actor": driver_uri})
            pre_ids = [
                item["messageId"]
                for item in pre_pending.get("messages", [])  # type: ignore[union-attr]
            ]
            if pre_ids != [msg_a, msg_b]:
                raise AssertionError(
                    f"pre-upgrade persona FIFO not queued under {_entry['DRIVER']}: "
                    f"{pre_pending} receipts: {send_a.get('deliveries')} "
                    f"{send_b.get('deliveries')}"
                )
            stale_delivery = _entry['pending_delivery_for'](pre_pending, msg_a)
            channel_name = channel_actor.rsplit(':', 1)[1]
            identity_names = (channel_name, _entry['SESSION_ACTOR'], _entry['DRIVER'])
            original_identities = _entry['read_agent_identities'](
                state, installed_python, env, owner=owner, machine=node_id,
                names=identity_names,
            )

            # --- upgrade and automatically restart the running deployment -- #
            _entry['_set_phase'](old_channel_phase, "upgrade-auto-restart")
            new_version = _entry['UPGRADED_VERSION']
            upgraded_commit = _entry['push_upgrade'](seed, candidate_revision, new_version, env)
            # The executable is still the promoted old tree here. Its own
            # top-level upgrade command performs the transition; only calls
            # after this point use the candidate's grouped command surface.
            upgrade = _entry['hyprial'](
                executable, _entry['upgrade_command'](executable, env), env
            )
            if upgrade.get("ok") is not True or upgrade.get("upgraded") is not True:
                raise AssertionError(f"hyprial upgrade did not report success: {upgrade}")
            if upgrade.get("resolvedTag") != f"v{new_version}":
                raise AssertionError(f"upgrade did not report resolved tag: {upgrade}")
            if upgrade.get("resolvedCommit") != upgraded_commit:
                raise AssertionError(f"upgrade did not report resolved commit: {upgrade}")
            if upgrade.get("restartRequired") is not False:
                raise AssertionError(f"upgrade left a restart pending: {upgrade}")
            restart = upgrade.get("restart")
            if not isinstance(restart, dict):
                raise AssertionError(f"upgrade omitted restart evidence: {upgrade}")
            if restart.get("attempted") is not True or restart.get("restarted") is not True:
                raise AssertionError(f"upgrade did not restart the daemon: {upgrade}")
            before_restart = restart.get("before")
            after_restart = restart.get("after")
            if not isinstance(before_restart, dict) or not isinstance(after_restart, dict):
                raise AssertionError(f"upgrade omitted before/after restart facts: {upgrade}")
            verified_new_epoch = _entry['verify_restart_identity'](
                before_restart, after_restart, _entry['ipc'](state, "ping", {}),
                old_pid=old_pid, old_epoch=old_epoch,
                old_version=old_version, new_version=new_version,
            )
            new_pid = after_restart["pid"]
            epoch_source = "cli-and-daemon" if "epoch" in after_restart else "independent-daemon"
            observed = str(_entry['hyprial'](executable, ["version", "--json"], env)["packageVersion"])
            if observed != new_version or observed == old_version:
                raise AssertionError(
                    f"upgrade did not change version: old={old_version} new={new_version} got={observed}")
            interpreter = _entry['run'](
                installed_python, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                env=env,
            ).strip()
            expected_interpreter = f"{_entry['sys'].version_info.major}.{_entry['sys'].version_info.minor}"
            if case == "same-interpreter" and interpreter != expected_interpreter:
                raise AssertionError(f"same-interpreter upgrade changed Python: {expected_interpreter} -> {interpreter}")
            live = _entry['ps'](env)["daemon"]
            new_epoch = str(live.get("epoch") or "")  # type: ignore[union-attr]
            if live.get("pid") != new_pid or live.get("running") is not True:  # type: ignore[union-attr]
                raise AssertionError(f"automatic restart did not yield the reported daemon: {live}")
            if not new_epoch or new_epoch == old_epoch:
                raise AssertionError(
                    f"automatic restart did not yield a fresh daemon epoch: {live}"
                )
            if new_epoch != verified_new_epoch:
                raise AssertionError(
                    "verified restart epoch does not match the live daemon: "
                    f"verified={verified_new_epoch} live={new_epoch}"
                )
            _entry['wait_for_channel_child_alive'](
                old_channel_ready,
                old_channel,
                old_channel_log,
                child_pid=old_channel_child_pid,
                phase="upgrade-auto-restart",
            )
            report.add(
                "PASS",
                "upgrade-auto-restart",
                f"{old_version}/pid={old_pid} -> {new_version}/pid={new_pid} "
                f"epoch={old_epoch}->{new_epoch} proof={epoch_source}",
            )
            current_identities = _entry['read_agent_identities'](
                state, installed_python, env, owner=owner, machine=node_id,
                names=identity_names,
            )
            identities = _entry['migrated_identities'](original_identities, current_identities)
            current_channel_actor = identities[channel_name]['current']['uri']
            current_driver_uri = identities[_entry['DRIVER']]['current']['uri']
            report.add(
                "PASS", "registry-identity-continuity",
                "three original public agent ids retained; current entity addresses resolved independently",
            )

            # --- track resolution touchpoints on the upgraded deployment -- #
            # Three-track channels (task 25): an explicit settings.json
            # updateTrack resolves the movable tag of the same name.  The
            # no-op here must NOT restart the daemon (epoch must survive),
            # and a missing track tag must fail loud without falling back
            # to the highest version tag.
            _entry['run']("git", "tag", "-f", "stable", upgraded_commit, env=env, cwd=seed)
            _entry['run']("git", "push", "--force", "origin", "refs/tags/stable",
                env=env, cwd=seed)
            settings = home / "settings.json"
            settings.write_text(
                '{"version":1,"updateTrack":"stable"}\n', encoding="utf-8"
            )
            track_noop = _entry['hyprial'](
                executable, ["daemon", "upgrade", "--json"], env
            )
            if (
                track_noop.get("upgraded") is not False
                or track_noop.get("track") != "stable"
                or track_noop.get("resolvedTag") != "stable"
                or track_noop.get("resolvedCommit") != upgraded_commit
            ):
                raise AssertionError(
                    f"stable track did not resolve as a no-op: {track_noop}"
                )
            epoch_after_track_noop = str(
                _entry['ps'](env)["daemon"].get("epoch")  # type: ignore[union-attr]
            )
            if epoch_after_track_noop != new_epoch:
                raise AssertionError(
                    "track no-op restarted the daemon: "
                    f"{new_epoch} -> {epoch_after_track_noop}"
                )
            settings.write_text(
                '{"version":1,"updateTrack":"nightly"}\n', encoding="utf-8"
            )
            antifallback = _entry['hyprial'](
                executable,
                ["daemon", "upgrade", "--json"],
                env,
                exits=(1,),
            )
            if (
                antifallback.get("code") != "UPGRADE_CHECK_FAILED"
                or "'nightly'" not in str(antifallback.get("error"))
            ):
                raise AssertionError(
                    f"missing track tag fell back or failed wrong: {antifallback}"
                )
            epoch_after_antifallback = str(
                _entry['ps'](env)["daemon"].get("epoch")  # type: ignore[union-attr]
            )
            if epoch_after_antifallback != new_epoch:
                raise AssertionError(
                    "failed track probe disturbed the daemon: "
                    f"{new_epoch} -> {epoch_after_antifallback}"
                )
            settings.unlink()
            report.add(
                "PASS",
                "track-resolution",
                "stable track resolved as a daemon-quiet no-op; missing nightly "
                "tag failed UPGRADE_CHECK_FAILED without fallback",
            )

            if old_channel.poll() is not None:
                raise AssertionError("old stdio Channel exited across daemon upgrade")
            old_tool_calls = _entry['wait_for_new_tool_call'](
                old_channel_ready,
                old_tool_calls,
                daemon_epoch=new_epoch,
                process=old_channel,
                error_log=old_channel_log,
                stage="old-channel-tool-on-new-daemon",
            )
            _entry['wait_for_channel_current'](
                executable,
                env,
                actor=channel_actor,
                session_ref=old_channel_ref,
                build_version=old_version,
                process=old_channel,
                error_log=old_channel_log,
                daemon_epoch=new_epoch,
                stage="old-channel-refreshed-on-new-daemon",
                protocol_version=old_protocol,
                generation=old_generation_after,
                require_operational_liveness=old_generation_after == "current",
                actor_identity=identities[channel_name],
            )
            report.add(
                "PASS",
                "old-channel-new-daemon",
                "held-open old-version client re-registered on the upgraded daemon, "
                f"owner-fenced, generation={old_generation_after} "
                f"(old protocol {old_protocol}, new {_entry['CHANNEL_PROTOCOL_VERSION']})",
            )

            # --- connectors recovered (fresh worker PIDs) ------------------ #
            after_adapters = _entry['wait_for_adapters'](executable, env)
            for name in _entry['ADAPTER_NAMES']:
                if after_adapters[name].get("pid") == old_adapter_pids[name]:  # type: ignore[union-attr]
                    raise AssertionError(f"adapter {name} kept its pre-upgrade PID")
            report.add("PASS", "connectors-recovered",
                       f"{len(_entry['ADAPTER_NAMES'])} adapters online with fresh PIDs after upgrade")

            # --- interactive session re-registers on the new daemon -------- #
            ref2 = f"sref-{_entry['uuid4']()}"
            reg2 = _entry['ipc'](state, "session.register", {
                "actor": _entry['SESSION_ACTOR'], "sessionRef": ref2,
                "command": [_entry['sys'].executable, "-c", "pass"],
                "cwd": str(root), "source": "e2e-upgrade-continuity", "runtime": "pi",
            })
            if reg2.get("registered") is not True:
                raise AssertionError(f"session did not re-register after upgrade: {reg2}")
            who2 = _entry['ipc'](state, "identity.whoami",
                       {"actor": _entry['SESSION_ACTOR'], "sessionRef": ref2})
            if who2.get("sessionRegistered") is not True:
                raise AssertionError(f"whoami did not confirm re-registration: {who2}")
            canonical_actor = who2.get("actor")
            if not isinstance(canonical_actor, str):
                raise AssertionError(
                    f"whoami omitted the canonical session actor: {who2}"
                )
            if canonical_actor != identities[_entry['SESSION_ACTOR']]['current']['uri']:
                raise AssertionError("re-registration changed the original session agent incarnation")
            sessions2 = _entry['ps'](env).get("interactiveSessions", [])
            if not any(s.get("actor") == canonical_actor and s.get("sessionRef") == ref2
                       for s in sessions2):  # type: ignore[union-attr]
                raise AssertionError(f"re-registered sessionRef not visible: {sessions2}")
            if ref2 == ref1:
                raise AssertionError("re-registration must be a fresh generation")
            report.add("PASS", "session-reregistered",
                       f"{canonical_actor} re-registered fresh generation on new daemon")

            # --- new package: config fence/canonical actor + restart ------- #
            assert old_channel is not None and old_channel_stop is not None
            _entry['stop_channel_client'](
                old_channel,
                old_channel_stop,
                old_channel_ready,
                old_channel_phase,
            )
            old_channel = None
            old_channel_stop = None

            new_channel_ref = f"new-channel-{_entry['uuid4']()}"
            (
                new_channel,
                new_channel_stop,
                new_channel_log,
                new_channel_ready,
            ) = _entry['start_channel_client'](
                executable=executable,
                env=env,
                root=root,
                actor=current_channel_actor,
                session_ref=new_channel_ref,
                phase=new_channel_phase,
            )
            _entry['wait_for_channel_current'](
                executable,
                env,
                actor=current_channel_actor,
                session_ref=new_channel_ref,
                build_version=new_version,
                process=new_channel,
                error_log=new_channel_log,
                daemon_epoch=new_epoch,
                stage="new-channel-initial-register",
                protocol_version=2,
                generation="current",
                require_operational_liveness=True,
            )
            new_tool_calls = _entry['wait_for_new_tool_call'](
                new_channel_ready,
                0,
                daemon_epoch=new_epoch,
                process=new_channel,
                error_log=new_channel_log,
                stage="new-channel-initial-tool",
            )

            fake_claude = root / "fake-claude"
            facts_path = root / "generated-channel-facts.json"
            probe_workspace = root / "config-probe-workspace"
            probe_workspace.mkdir(mode=0o700)
            _entry['hyprial'](
                executable,
                ["agent", "create", "--name", _entry['CHANNEL_NAME'],
                 "--cwd", str(probe_workspace), "--json"], env,
            )
            # The executable below is our local fixture, not Claude. Supply
            # only a non-secret sentinel for the native file-shape preflight.
            native = home / "agents" / _entry['CHANNEL_NAME'] / "secrets/native/claude"
            for directory in (native.parent.parent, native.parent, native):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            credential = native / ".credentials.json"
            credential.write_text('{"fixture":"e2e-sentinel-not-a-credential"}\n', encoding="utf-8")
            credential.chmod(0o600)
            _entry['write_fake_claude_launcher'](
                fake_claude, facts=facts_path, phase=root / "fake-claude.phase"
            )
            launch_env = {
                **env,
                "HARNESS_CLAUDE_BIN": str(fake_claude),
                "HYPRIAL_E2E_CHANNEL_FACTS": str(facts_path),
                "HYPRIAL_E2E_CHANNEL_PHASE": str(root / "fake-claude.phase"),
            }
            _entry['_set_phase'](_entry['Path'](launch_env["HYPRIAL_E2E_CHANNEL_PHASE"]), "generated-config")
            launched = _entry['hyprial'](
                executable,
                [
                    "agent", "start", "claude", "--name", _entry['CHANNEL_NAME'],
                    "--cwd", str(probe_workspace), "--json",
                ],
                launch_env,
            )
            if launched.get("runtimeExitCode") != 0:
                raise AssertionError(f"isolated fake Claude launch failed: {launched}")
            facts = _entry['json'].loads(facts_path.read_text(encoding="utf-8"))
            if not all(
                facts.get(key) is True
                for key in (
                    "bareActor",
                    "ownerPidPresent",
                    "ownerPidMatchesLauncher",
                    "ownerIdentityPresent",
                )
            ):
                raise AssertionError(f"generated MCP config lacks stable identity fence: {facts}")
            report.add(
                "PASS",
                "new-config-fenced",
                "upgraded launcher generated logical actor selector and PID-birth owner fence",
            )

            _entry['hyprial'](executable, ["daemon", "stop", "--json"], env)
            daemon_up = False
            final_reinit = _entry['hyprial'](executable, _entry['init_command'](executable, env), env)
            daemon_up = True
            final_daemon = _entry['init_daemon_identity'](final_reinit)
            final_epoch = str(final_daemon.get("epoch") or "")
            if not final_epoch or final_epoch == new_epoch:
                raise AssertionError(
                    "second restart did not yield a fresh daemon epoch: "
                    f"{final_reinit}"
                )
            if new_channel.poll() is not None:
                raise AssertionError("upgraded stdio Channel exited on daemon restart")
            _entry['wait_for_new_tool_call'](
                new_channel_ready,
                new_tool_calls,
                daemon_epoch=final_epoch,
                process=new_channel,
                error_log=new_channel_log,
                stage="new-channel-tool-after-daemon-restart",
            )
            _entry['wait_for_channel_current'](
                executable,
                env,
                actor=current_channel_actor,
                session_ref=new_channel_ref,
                build_version=new_version,
                process=new_channel,
                error_log=new_channel_log,
                daemon_epoch=final_epoch,
                stage="new-channel-refreshed-after-daemon-restart",
                protocol_version=2,
                generation="current",
                require_operational_liveness=True,
            )
            report.add(
                "PASS",
                "new-channel-daemon-restart",
                "upgraded fenced child re-registered without MCP client reconnect",
            )

            # --- messages not lost across the upgrade boundary ------------- #
            after = _entry['ps'](env)
            recovered = _entry['pending_ids'](after)
            if recovered != [msg_a, msg_b]:
                raise AssertionError(f"pending FIFO not preserved: {recovered}")
            if _entry['delivery_for'](after, msg_a) == stale_delivery:
                raise AssertionError("recovered message reused its pre-upgrade deliveryId")
            # C48: the rows the OLD version wrote must stay exactly readable on
            # the new version: exact old summary, body byte count and body.
            _entry['assert_legacy_rows'](
                state,
                current_driver_uri,
                {msg_a: "before-upgrade", msg_b: "also-before-upgrade"},
                summaries=expected_summaries,
            )
            report.add(
                "PASS",
                "legacy-rows-readable",
                "pre-upgrade rows keep exact summaries (legacy=null), exact bodyBytes, readable body",
            )
            ack_a = _entry['hyprial'](executable, ["message", "ack", "--from", _entry['DRIVER'], msg_a, "--json"], env)
            if ack_a.get("acknowledged") is not True:
                raise AssertionError(f"ack of message A failed: {ack_a}")
            mid = _entry['pending_ids'](_entry['ps'](env))
            if mid != [msg_b]:
                raise AssertionError(f"FIFO recovery order changed after ack: {mid}")
            ack_b = _entry['hyprial'](executable, ["message", "ack", "--from", _entry['DRIVER'], msg_b, "--json"], env)
            if ack_b.get("acknowledged") is not True:
                raise AssertionError(f"ack of message B failed: {ack_b}")
            _entry['hyprial'](executable, ["daemon", "stop", "--json"], env)
            daemon_up = False
            replay_init = _entry['hyprial'](executable, _entry['init_command'](executable, env), env)
            daemon_up = True
            replay_epoch = str(_entry['init_daemon_identity'](replay_init).get("epoch") or "")
            if not replay_epoch or replay_epoch == final_epoch:
                raise AssertionError(
                    f"replay-check restart did not yield a fresh daemon epoch: {replay_init}"
                )
            replayed = _entry['pending_ids'](_entry['ps'](env))
            if replayed != []:
                raise AssertionError(f"acknowledged messages replayed after restart: {replayed}")
            report.add("PASS", "messages-no-loss",
                       "two in-flight messages survived with fresh deliveryIds, FIFO, no replay")

            # Every other daemon restart in this scenario re-verifies the
            # held-open child before moving on; the replay-check restart must
            # too.  Tearing down while the child is still mid-recovery races
            # the stdio shutdown grace and strands the child on a SIGTERM
            # instead of a clean exit.
            _entry['wait_for_channel_current'](
                executable,
                env,
                actor=current_channel_actor,
                session_ref=new_channel_ref,
                build_version=new_version,
                process=new_channel,
                error_log=new_channel_log,
                daemon_epoch=replay_epoch,
                stage="new-channel-current-before-teardown",
                protocol_version=2,
                generation="current",
                require_operational_liveness=True,
            )
            report.add(
                "PASS",
                "new-channel-replay-restart",
                "held-open child re-registered on the replay-check epoch before teardown",
            )

            # --- post-upgrade send with the driver persona as sender ------- #
            # The persona is otherwise an ack-only consumer after the
            # boundary; this seam proves a NEW send carrying it as sender is
            # also admitted on the upgraded daemon: re-register the persona
            # on the replay-check epoch (fresh generation, same canonical
            # spelling), wait for event-driven presence, then send through
            # the new daemon's resolve-or-reject send gate.  The durable
            # session carried over from the old version plus this fresh
            # registration is the backing that lets the gate resolve the
            # canonical persona instead of rejecting it.
            driver_ref2 = f"sref-{_entry['uuid4']()}"
            driver_reg2 = _entry['ipc'](state, "session.register", {
                "actor": current_driver_uri, "sessionRef": driver_ref2,
                "command": [_entry['sys'].executable, "-c", "pass"],
                "cwd": str(root), "source": "e2e-upgrade-continuity", "runtime": "pi",
            })
            if driver_reg2.get("registered") is not True:
                raise AssertionError(
                    f"driver persona did not re-register after upgrade: {driver_reg2}"
                )
            driver2_deadline = _entry['time'].monotonic() + 20.0
            while True:
                driver2_sessions = _entry['ps'](env).get(
                    "interactiveSessions", []
                )
                driver2_online = next(
                    (
                        s for s in driver2_sessions  # type: ignore[union-attr]
                        if s.get("actor") == current_driver_uri
                        and s.get("sessionRef") == driver_ref2
                    ),
                    None,
                )
                if driver2_online is not None and driver2_online.get("online") is True:
                    break
                if _entry['time'].monotonic() > driver2_deadline:
                    raise AssertionError(
                        f"driver persona never came online after upgrade: "
                        f"{driver2_sessions}"
                    )
                _entry['time'].sleep(0.2)
            send_c = _entry['hyprial'](executable, [
                "message", "send", "--from", current_driver_uri, "--to", current_driver_uri,
                "--summary", "after-upgrade", "after-upgrade", "--json",
            ], env)
            msg_c = str(send_c["messageId"])
            post_deadline = _entry['time'].monotonic() + 20.0
            while True:
                post_pending = _entry['ipc'](state, "message.pending.list", {"actor": current_driver_uri})
                post_ids = [
                    item["messageId"]
                    for item in post_pending.get("messages", [])  # type: ignore[union-attr]
                ]
                if post_ids == [msg_c]:
                    break
                if _entry['time'].monotonic() > post_deadline:
                    raise AssertionError(
                        f"post-upgrade driver send did not commit under {_entry['DRIVER']}: "
                        f"{post_pending} receipt: {send_c.get('deliveries')}"
                    )
                _entry['time'].sleep(0.2)
            ack_c = _entry['hyprial'](executable, ["message", "ack", "--from", _entry['DRIVER'], msg_c, "--json"], env)
            if ack_c.get("acknowledged") is not True:
                raise AssertionError(f"ack of post-upgrade message C failed: {ack_c}")
            report.add(
                "PASS",
                "post-upgrade-driver-send",
                "new send with driver persona as sender admitted post-upgrade, "
                "committed to the persona inbox, and acked",
            )
        except Exception as error:  # noqa: BLE001 - executable contract boundary
            report.add("FAIL", "upgrade-continuity", str(error))
        finally:
            for process, stop in (
                (old_channel, old_channel_stop),
                (new_channel, new_channel_stop),
            ):
                if process is not None and stop is not None and process.poll() is None:
                    try:
                        if process is old_channel:
                            assert old_channel_ready is not None
                            _entry['stop_channel_client'](
                                process, stop, old_channel_ready, old_channel_phase
                            )
                        else:
                            assert new_channel_ready is not None
                            _entry['stop_channel_client'](
                                process, stop, new_channel_ready, new_channel_phase
                            )
                    except Exception as error:  # noqa: BLE001
                        report.add("BLOCKED", "cleanup-channel", str(error))
            if daemon_up:
                try:
                    _entry['hyprial'](executable, ["daemon", "stop", "--json"], env, exits=(0, 1))
                except Exception as error:  # noqa: BLE001
                    report.add("BLOCKED", "cleanup-daemon", str(error))
            if installed:
                try:
                    _entry['run'](uv, "tool", "uninstall", "hyprial", env=env)
                except AssertionError as error:
                    report.add("BLOCKED", "cleanup-uninstall", str(error))
    return report.exit_code()
