#!/usr/bin/env python3
# Decisions:
# - Notarization for packaging/clients/sign_bundle.py, moved here
#   (2026-11-19, PR #2980 review) to keep that file under the 1000-line
#   limit; the functions take the sign_bundle module, as signing/modes.py
#   does, so its Tools, SignError and constants stay in one place.
# - The submission id must be on disk as soon as it exists, because the
#   runner kills a cancelled or timed-out step with SIGKILL and a long upload
#   may be cut (measured 2026-11-19, local run: `notarytool submit
#   --output-format json` without --wait printed nothing for 300 s while
#   Apple's history already listed the submission, created in the first
#   second; the upload of a 409 MB app was still running). notarytool's own
#   output cannot be relied on mid-run (it may print the id only at the end,
#   and a killed process loses buffered output), so while `submit` runs the
#   tool asks `notarytool history` every NOTARY_POLL_SECONDS for a
#   submission that was not there before and carries the file's name; the
#   first one found is recorded, printed and the evidence saved at once.
#   When the history cannot be read before the submit, nothing is polled
#   (an older submission of the same file name could otherwise be taken for
#   this one); the id then comes from notarytool's own reply.
#   `submit` itself is bounded by NOTARY_SUBMIT_SECONDS (the upload);
#   `notarytool wait <id> --timeout <m>m` by m minutes plus
#   NOTARY_WAIT_SLACK_SECONDS; the log fetch by NOTARY_LOG_SECONDS. A cut
#   submit or wait is never resubmitted: the failure names the id with "do
#   not resubmit".
"""Submit, wait for and log one notarization, the submission id saved as soon as Apple lists it."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def reply_json(stdout: str) -> dict[str, Any]:
    """The last JSON object notarytool printed, or {}."""

    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    try:
        value = json.loads(stdout)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def history(sb: Any, tools: Any, profile: str) -> list[dict[str, Any]] | None:
    """The account's submission history, or None when it cannot be read."""

    try:
        result = tools.run("xcrun", "notarytool", "history", "--keychain-profile", profile, "--output-format", "json", timeout=sb.NOTARY_LOG_SECONDS)
    except sb.SignError:
        return None
    entries = reply_json(result.stdout).get("history") if result.returncode == 0 else None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


def _new_id(sb: Any, tools: Any, profile: str, before: set[str] | None, name: str) -> str:
    if before is None:
        return ""
    fresh = [entry["id"] for entry in history(sb, tools, profile) or [] if entry["id"] not in before and entry.get("name") == name]
    return fresh[0] if len(fresh) == 1 else ""


def submit(sb: Any, tools: Any, path: Path, profile: str, record: dict[str, Any], saved: Any) -> None:
    """Upload ``path``; the id goes into ``record`` (and the evidence) as soon as Apple lists it."""

    listed = history(sb, tools, profile)
    before = None if listed is None else {entry["id"] for entry in listed}
    started = time.monotonic()
    polled = started
    with tempfile.TemporaryDirectory(prefix="hsb-notary-") as scratch:
        process, entry = tools.start("xcrun", "notarytool", "submit", str(path), "--keychain-profile", profile, "--output-format", "json", sink=Path(scratch))
        try:
            while process.poll() is None:
                if not record["id"] and time.monotonic() - polled >= sb.NOTARY_POLL_SECONDS:
                    polled = time.monotonic()
                    record["id"] = _new_id(sb, tools, profile, before, path.name)
                    if record["id"]:
                        record["status"] = "uploading"
                        sb.say(f"notary {record['label']}: submission id={record['id']} (in the evidence now; upload running)")
                        saved()
                if time.monotonic() - started > sb.NOTARY_SUBMIT_SECONDS:
                    raise sb.SignError(f"notarytool submit timeout after {sb.NOTARY_SUBMIT_SECONDS:.0f}s")
                time.sleep(0.2)
        except (sb.SignError, KeyboardInterrupt) as error:
            process.kill()
            process.wait()
            entry.update({"exit": None, "seconds": round(time.monotonic() - started, 1)})
            record["id"] = record["id"] or sb.found_id(Path(scratch)) or _new_id(sb, tools, profile, before, path.name)
            record["detail"] = tools.redact(str(error) or "interrupted")
            if record["id"]:
                raise sb.SignError(f"notarization of the {record['label']} cut short during the upload ({record['detail']}); submission {record['id']} may still count, check it with `notarytool info` and do not resubmit") from error
            raise
        entry.update({"exit": process.returncode, "seconds": round(time.monotonic() - started, 1)})
        stdout, stderr = (sb._text(Path(scratch) / part) for part in ("stdout", "stderr"))
        reported = str(reply_json(stdout).get("id") or sb.found_id(Path(scratch)))
    if record["id"] and reported and reported != record["id"]:
        record["historyId"] = record["id"]
    record["id"] = reported or record["id"]
    if process.returncode != 0 or not record["id"]:
        record.update({"exit": process.returncode, "detail": tools.redact(" | ".join(stderr.strip().splitlines()[-3:]))})
        raise sb.SignError(f"notarization of the {record['label']}: submit failed (exit {process.returncode}, id {record['id'] or '-'}; do not resubmit before checking `notarytool history`)")


def notarize(sb: Any, tools: Any, path: Path, label: str, profile: str, evidence_path: Path, evidence: dict[str, Any], minutes: int, saved: Any) -> None:
    """Submit ``path`` (id saved at once), wait, save the log next to the evidence; anything but Accepted fails."""

    started = time.monotonic()
    record: dict[str, Any] = {"label": label, "id": "", "status": "unknown", "message": "", "exit": None}
    evidence["notary"].append(record)
    try:
        submit(sb, tools, path, profile, record, saved)
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        saved()
    record["status"] = "submitted"
    sb.say(f"notary {label}: submitted id={record['id']} (in the evidence now; waiting up to {minutes} minutes)")
    saved()
    with tempfile.TemporaryDirectory(prefix="hsb-notary-") as scratch:
        try:
            result = tools.run("xcrun", "notarytool", "wait", record["id"], "--keychain-profile", profile, "--timeout", f"{minutes}m", "--output-format", "json", timeout=minutes * 60.0 + sb.NOTARY_WAIT_SLACK_SECONDS, sink=Path(scratch))
        except (sb.SignError, KeyboardInterrupt) as error:
            record.update({"seconds": round(time.monotonic() - started, 1), "detail": tools.redact(str(error) or "interrupted")})
            raise sb.SignError(f"notarization of the {label} cut short ({record['detail']}); submission {record['id']} still counts, check it with `notarytool info` and do not resubmit") from error
    reply = reply_json(result.stdout)
    record.update({"status": str(reply.get("status", "unknown")), "message": str(reply.get("message", "")), "exit": result.returncode, "seconds": round(time.monotonic() - started, 1)})
    sb.say(f"notary {label}: id={record['id']} status={record['status']} message={record['message']} ({record['seconds']}s)")
    log = evidence_path.with_name(f"{evidence_path.stem}-notary-{label}.json")
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hsb-notary-") as scratch:
        fetched = tools.run("xcrun", "notarytool", "log", record["id"], "--keychain-profile", profile, str(Path(scratch) / "log.json"), timeout=sb.NOTARY_LOG_SECONDS)
        if fetched.returncode == 0 and (Path(scratch) / "log.json").is_file():
            shutil.copyfile(Path(scratch) / "log.json", log)
    if fetched.returncode == 0 and log.is_file():
        record["log"] = log.name
    else:
        record["logError"] = f"exit {fetched.returncode}: " + tools.redact(" | ".join((fetched.stderr or fetched.stdout).strip().splitlines()[-2:]))
        sb.say(f"notary {label}: log not fetched ({record['logError']})")
    if record["status"] == "Accepted" and result.returncode == 0:
        return
    record["detail"] = tools.redact(" | ".join(result.stderr.strip().splitlines()[-3:]))
    if record["status"] not in {"Invalid", "Rejected"}:
        raise sb.SignError(f"notarization of the {label} not finished: status {record['status']} (exit {result.returncode}); submission {record['id']} still counts, check it with `notarytool info` and do not resubmit")
    raise sb.SignError(f"notarization of the {label} not accepted: status {record['status']} (exit {result.returncode})")


def start(tools: Any, executable: str, args: list[str], sink: Path) -> subprocess.Popen[bytes]:
    """Start ``executable`` with the tools' environment, no stdin, output to files in ``sink``."""

    with (sink / "stdout").open("w") as out, (sink / "stderr").open("w") as err:
        return subprocess.Popen([executable, *args], env=tools.env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
