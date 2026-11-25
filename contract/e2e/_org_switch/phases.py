"""E2E-032: switching the viewed organization on the org page of ``hyprial gui``.

Decision record (2026-11-21):
- Acceptance row 4 (old id F3) of
  docs/design/gui-single-launch-plans-2026-11-15/1e-4-stage-f-acceptance.md,
  restated for dev as of this date in 32-org-switch-browser.md: the top-level
  机器 tab is gone (the machine list is 成员 → 设备, the refresh action sits on
  the 成员 header) and the two organizations come from two local
  ``hyprial org create`` calls on the isolated home, not from joining.
- Request-level claims (which daemon methods the page calls, with which
  ``org``) and boundary booleans (no cross-origin HTTP requests, no
  ``Authorization`` header, no ``grant=`` in request URLs) are the core of
  the row; visible claims are limited to facts the isolated daemon produces
  every time (the viewed org id, the page title, the refresh button). No
  machine name is asserted.
- ``fixture`` parses the two ``org create --json`` responses' ``org`` ids and
  asserts they are two distinct non-empty strings whose set equals ``org.list``.
- Verdict and exit code follow E2E-031: 0 PASS, 1 FAIL, 2 BLOCKED. A missing
  browser, Node or ``gui/node_modules``, or a failing/timed-out probe, is
  BLOCKED with the reason before any process starts; a skip is not a result.
- Teardown always runs; the leftover check tracks only started process groups
  (including the detached Chromium browser group when observed) and
  child-reported PIDs fenced by birth identity, never signalling unverified or
  reused identities and failing OS-07 if any survivor or unverified identity
  remains; ``force_kill_survivors()`` cleans up verified-alive survivors before
  removing the root (still failing OS-07 if any survivor existed after
  ``stop()`` or after force-kill), and on macOS ``launchctl list`` errors or
  non-int counts fail OS-07 rather than reading as zero.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from _daemon_runner import artifacts_root, hyprial_executable, short_temp_root, teardown_guard
from _gui_browser.session import GuiSession, missing_prerequisites, scrub

SCENARIO = "E2E-032"
OWNER = "e2e-org-switch"
BROWSER_SCRIPT = Path(__file__).with_name("browser.mjs")
BROWSER_SECONDS = 180.0
LEFTOVER_SECONDS = 15.0
PASS, FAIL, BLOCKED, NOT_RUN = "PASS", "FAIL", "BLOCKED", "NOT_RUN"


def evidence(step: str, **fields: Any) -> None:
    print(f"EVIDENCE {step} " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str),
          flush=True)


class Report:
    """Per-assertion statuses; a step whose prerequisite did not pass is NOT_RUN."""

    def __init__(self) -> None:
        self.outcomes: dict[str, tuple[str, str]] = {}

    def record(self, step: str, status: str, detail: str = "") -> None:
        self.outcomes[step] = (status, detail)
        print(f"RESULT {status} {SCENARIO} {step}" + (f" detail={detail}" if detail else ""),
              flush=True)

    def check(self, step: str, needs: tuple[str, ...], body: Callable[[], str]) -> None:
        unmet = [name for name in needs if self.outcomes.get(name, (NOT_RUN,))[0] != PASS]
        if unmet:
            self.record(step, NOT_RUN, f"needs {','.join(unmet)}")
            return
        try:
            self.record(step, PASS, body())
        except AssertionError as error:
            self.record(step, FAIL, scrub(str(error))[:3000])

    def verdict(self) -> tuple[str, int]:
        statuses = {status for status, _ in self.outcomes.values()}
        if FAIL in statuses:
            return FAIL, 1
        if BLOCKED in statuses:
            return BLOCKED, 2
        if statuses - {PASS}:
            return FAIL, 1
        return PASS, 0


def launchd_hyprial_labels() -> int | str | None:
    """How many launchd jobs carry a ``com.hyprial`` label; error text on failure, None off macOS."""

    if sys.platform != "darwin":
        return None
    try:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=20,
                                 check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return f"launchctl list failed: {scrub(f'{type(error).__name__}: {error}')}"
    if listing.returncode != 0:
        detail = scrub((listing.stderr or listing.stdout or "").strip())[:300]
        return f"launchctl list exited {listing.returncode}" + (f": {detail}" if detail else "")
    return sum("com.hyprial" in line for line in listing.stdout.splitlines())


def fixture(session: GuiSession, nonce: str) -> dict[str, Any]:
    """Ready home, daemon, recorded first-run skip and two local organizations."""

    home = session.prepare_home()
    session.start_daemon()
    skipped = session.request("onboarding.firstRun.skip",
                              {"expectedRevision": 0, "operationKey": f"e2e032-skip-{nonce}"})
    first_run = skipped.get("firstRun") if isinstance(skipped.get("firstRun"), dict) else {}
    if first_run.get("state") != "recorded":
        raise AssertionError(f"onboarding.firstRun.skip did not record: {first_run}")
    names = [f"e2e032-a-{nonce}", f"e2e032-b-{nonce}"]
    created = [session.command("org", "create", name, "--json") for name in names]
    if not all(item.get("ok") is True for item in created):
        raise AssertionError(f"org create failed: {created}")
    created_ids = [item.get("org") for item in created]
    if (
        len(created_ids) != 2
        or not all(isinstance(item, str) and item for item in created_ids)
        or len(set(created_ids)) != 2
    ):
        raise AssertionError(f"org create did not return two distinct non-empty org ids: {created_ids}")
    listed = session.request("org.list", {})
    rows = listed.get("orgs") if isinstance(listed.get("orgs"), list) else []
    ids = [row.get("org") for row in rows if isinstance(row, dict)]
    if (
        len(ids) != 2
        or not all(isinstance(item, str) and item for item in ids)
        or set(ids) != set(created_ids)
    ):
        raise AssertionError(f"org.list ids {ids!r} did not match created org ids {created_ids!r}")
    return {"homeOwner": home["owner"], "firstRun": first_run.get("state"), "orgIds": ids,
            "orgNames": names, "created": [sorted(item) for item in created],
            "listRows": [{key: row.get(key) for key in ("org", "kind", "role", "state", "current")}
                         for row in rows]}


def of(requests: list[dict[str, Any]], phase: str | None = None,
       method: str | None = None) -> list[dict[str, Any]]:
    return [entry for entry in requests
            if (phase is None or entry.get("phase") == phase)
            and (method is None or entry.get("method") == method)]


def assertions(report: Report, world: dict[str, Any], browser: dict[str, Any]) -> None:
    requests: list[dict[str, Any]] = browser.get("requests") or []
    facts: dict[str, Any] = browser.get("facts") or {}
    org_ids: list[str] = world["orgIds"]
    target = facts.get("target")

    def reached() -> str:
        options = facts.get("selectorOptions") or []
        assert facts.get("handoffConsumed") is True, "the handoff was not consumed"
        assert len(options) == 2, f"查看组织 should offer both orgs: {options}"
        assert facts.get("viewedBefore") in org_ids and target in org_ids, facts
        return f"viewedBefore={facts['viewedBefore']} target={target} options={options}"

    def switch_loads_detail() -> str:
        networks = of(requests, "switch", "org.network")
        assert networks, "no org.network after the switch"
        wrong = [entry for entry in networks if entry.get("org") != target]
        assert not wrong, f"org.network after the switch named another org: {wrong}"
        assert any(entry.get("status") == 200 and entry.get("ok") is True for entry in networks), \
            f"no answered org.network for the switched org: {networks}"
        foreign = [entry for entry in of(requests, method="org.network")
                   if entry.get("org") not in org_ids]
        assert not foreign, f"org.network carried an id the daemon did not list: {foreign}"
        return f"org.network(switch)={[entry.get('org') for entry in networks]}"

    def no_overview_detour() -> str:
        detour = of(requests, "switch", "org.list") + of(requests, "members", "org.list")
        assert not detour, f"the switch went through the overview refresh: {detour}"
        return "org.list(switch,members)=0"

    def shows_second_org() -> str:
        assert facts.get("titleAfter") == facts.get("targetLabel"), facts
        for key in ("viewedInMembers", "viewedAfterRefresh"):
            assert facts.get(key) == target, f"{key}={facts.get(key)!r} target={target!r}"
        return f"title={facts.get('titleAfter')!r}"

    def members_refresh() -> str:
        assert facts.get("membersRefreshVisible") is True and facts.get("membersRefreshEnabled") is True, facts
        assert facts.get("topLevelMachinesTab") == 0, "a top-level 机器 tab is back"
        lists = of(requests, "refresh", "org.list")
        networks = of(requests, "refresh", "org.network")
        assert lists, "刷新 on 成员 sent no org.list"
        assert any(entry.get("org") == target for entry in networks), \
            f"刷新 on 成员 did not reread the viewed org: {networks}"
        return f"refresh org.list={len(lists)} org.network={[entry.get('org') for entry in networks]}"

    def no_fetch() -> str:
        fetches = of(requests, method="org.fetch")
        assert not fetches, f"org.fetch was sent: {fetches}"
        boundary = browser.get("boundary")
        assert isinstance(boundary, dict), f"browser boundary missing: {boundary!r}"
        for key in ("crossOrigin", "hasAuthorization", "hasGrantInUrl"):
            assert boundary.get(key) is False, f"boundary.{key}={boundary.get(key)!r}: {boundary}"
        return f"org.fetch=0 of {len(requests)} daemon requests; boundary={boundary}"

    report.check("OS-01", ("OS-00",), reached)
    report.check("OS-02", ("OS-01",), switch_loads_detail)
    report.check("OS-03", ("OS-01",), no_overview_detour)
    report.check("OS-04", ("OS-02",), shows_second_org)
    report.check("OS-05", ("OS-04",), members_refresh)
    report.check("OS-06", ("OS-01",), no_fetch)


def run() -> int:
    started = time.monotonic()
    report = Report()
    nonce = uuid4().hex[:8]
    out = artifacts_root() / SCENARIO
    out.mkdir(parents=True, exist_ok=True)
    labels_before = launchd_hyprial_labels()
    result: dict[str, Any] = {"scenario": SCENARIO, "launchdLabelsBefore": labels_before}
    missing = missing_prerequisites()
    if missing:
        report.record("OS-00", BLOCKED, "; ".join(missing))
        result["blocked"] = missing
    else:
        executable = hyprial_executable()
        root = Path(tempfile.mkdtemp(prefix="e2e032-", dir=short_temp_root()))
        account_home = Path.home().resolve()
        if root.resolve().is_relative_to(account_home):
            shutil.rmtree(root)
            raise AssertionError("the throwaway root would sit under the account HOME")
        session = GuiSession(executable, root, OWNER)
        browser: dict[str, Any] = {}
        try:
            with teardown_guard(session.stop, record=lambda error: evidence("OS-CLEANUP", error=error)):
                try:
                    world = fixture(session, nonce)
                    result["fixture"] = world
                    report.record("OS-00", PASS, f"orgs={world['orgIds']}")
                except AssertionError as error:
                    report.record("OS-00", FAIL, scrub(str(error))[:3000])
                    world = None
                if world is not None:
                    try:
                        session.start_gui()
                        browser = session.run_browser(BROWSER_SCRIPT, {
                            "handoffPath": session.startup["handoffPath"],
                            "origin": session.startup["origin"],
                            "orgIds": world["orgIds"],
                            "screenshotPath": str(out / "org-switch.png"),
                        }, BROWSER_SECONDS)
                    except AssertionError as error:
                        browser = {"ok": False, "error": scrub(str(error))}
                    result["browser"] = browser
                    if browser.get("ok") is not True:
                        report.record("OS-BROWSER", FAIL, str(browser.get("error"))[:3000])
                    assertions(report, world, browser)
        finally:
            deadline = time.monotonic() + LEFTOVER_SECONDS
            survivors = session.survivors()
            while survivors and time.monotonic() < deadline:
                time.sleep(0.5)
                survivors = session.survivors()
            forced_survivors: list[str] = []
            if survivors:
                forced_survivors = session.force_kill_survivors()
            result["daemonLog"] = session.daemon_log_matches(
                "supplier-not-member", "orgfs.update.rejected", '"level": "error"', '"level":"error"')
            result["exitCodes"] = session.exit_codes()
            result["startedPids"] = {
                "groups": session.groups,
                "reported": session.reported_pids,
                **({"unverified": list(session.unverified_identities)} if session.unverified_identities else {}),
            }
            if forced_survivors:
                result["forcedSurvivors"] = forced_survivors
            if survivors or forced_survivors or session.exit_codes().get("gui") not in (0, None):
                result["guiLogTail"] = session.log_tail("gui-child.log", 2000)
            if any(status == FAIL for status, _ in report.outcomes.values()):
                result["daemonLogTail"] = session.log_tail("daemon-child.log", 2000)
            shutil.rmtree(root, ignore_errors=True)
            labels_after = launchd_hyprial_labels()
            result["launchdLabelsAfter"] = labels_after
            problems = list(survivors)
            problems.extend(f"after force-kill: {item}" for item in forced_survivors)
            if root.exists():
                problems.append("throwaway root still present")
            if sys.platform == "darwin":
                if not isinstance(labels_before, int) or isinstance(labels_before, bool):
                    problems.append(f"launchd labels_before invalid: {labels_before}")
                if not isinstance(labels_after, int) or isinstance(labels_after, bool):
                    problems.append(f"launchd labels_after invalid: {labels_after}")
                if (
                    isinstance(labels_before, int)
                    and not isinstance(labels_before, bool)
                    and isinstance(labels_after, int)
                    and not isinstance(labels_after, bool)
                    and labels_before != labels_after
                ):
                    problems.append(f"launchd com.hyprial labels {labels_before} -> {labels_after}")
            elif labels_before != labels_after:
                problems.append(f"launchd com.hyprial labels {labels_before} -> {labels_after}")
            report.record("OS-07", FAIL if problems else PASS,
                          json.dumps(problems) if problems else
                          f"no survivor of {len(session.groups)} groups and "
                          f"{len(session.reported_pids)} reported pids; root removed")
    verdict, code = report.verdict()
    result["outcomes"] = {step: {"status": status, "detail": detail}
                          for step, (status, detail) in report.outcomes.items()}
    result["verdict"] = verdict
    result["seconds"] = round(time.monotonic() - started, 1)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                     encoding="utf-8")
    requests = (result.get("browser") or {}).get("requests") or []
    evidence("OS-REQUESTS", requests=[{key: entry.get(key) for key in
                                       ("phase", "method", "org", "status", "errorCode")}
                                      for entry in requests])
    print(f"RESULT {verdict} {SCENARIO} org-switch-browser seconds={result['seconds']} "
          f"result={out / 'result.json'}", flush=True)
    return code


def main() -> int:
    try:
        return run()
    except Exception as error:  # noqa: BLE001 - the final RESULT line always prints
        print(f"RESULT FAIL {SCENARIO} org-switch-browser {scrub(str(error))}"[:4000], flush=True)
        return 1


__all__ = ["main"]
