"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def write_json(_entry: dict[str, Any], /, path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_entry['json'].dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def write_channel_config(_entry: dict[str, Any], /, home, *, include_real_lark, lark_chat_id, lark_secret):
    """Write the disposable gateways before ``hyprial init`` loads them.

    The probe gateway exists only for pin semantics and is never started;
    its credential is fabricated.  The real test-App gateway rides only when
    the Lark prerequisites hold; its credential is written fresh from the
    preflight-resolved secret (env injection in CI, the approved local file
    in developer runs) and is never printed.  Inbound DM routing is by adapter
    pin; the sole direct route is resolved from this user's live p2p DM and is
    used only for the pinned agent's proactive image/file sends.
    """

    gateways: list[dict[str, Any]] = [
        {
            "provider": "lark",
            "name": _entry['PIN_PROBE_ADAPTER'],
            "appId": "isolated_e2e013_probe",
            "credentialRef": f"lark-{_entry['PIN_PROBE_ADAPTER']}",
            "routes": [],
        }
    ]
    _entry['write_json'](
        home / "secrets" / f"lark-{_entry['PIN_PROBE_ADAPTER']}.json",
        {"appSecret": "e2e013-probe-secret-never-used"},
    )
    if include_real_lark:
        if not isinstance(lark_chat_id, str) or not lark_chat_id:
            raise AssertionError("real Lark config requires the resolved p2p chat id")
        gateways.append(
            {
                "provider": "lark",
                "name": _entry['LARK_APP_NAME'],
                "appId": _entry['LARK_APP_ID'],
                "credentialRef": f"lark-{_entry['LARK_APP_NAME']}",
                "defaultRoute": _entry['LARK_OUTBOUND_ROUTE'],
                "routes": [
                    {
                        "name": _entry['LARK_OUTBOUND_ROUTE'],
                        "type": "direct",
                        "nativeId": lark_chat_id,
                    }
                ],
            }
        )
        destination = home / "secrets" / f"lark-{_entry['LARK_APP_NAME']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(lark_secret, str) or not lark_secret:
            raise AssertionError("real Lark config requires the test-App secret")
        destination.write_text(
            _entry['json'].dumps({"appSecret": lark_secret}), encoding="utf-8"
        )
        destination.chmod(0o600)
    _entry['write_json'](home / "channels.json", {"version": 1, "gateways": gateways})


def write_probe_skill(_entry: dict[str, Any], /, root):
    """Generate the disposable CFGPROBE skill and return (directory, token).

    The skill is runner-generated (never a repository fixture) with a fresh
    random nonce per run, so a reply carrying the token can only come from a
    worker that loaded THIS run's skill file — a stale session from an
    earlier run cannot echo it.  The token is the load witness: nothing else
    in the run (prompts included) ever contains it.
    """

    token = f"[SKILL-LOADED-{_entry['uuid4']().hex[:12]}]"
    directory = root / "cfgprobe-skill"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: e2e013-cfgprobe\n"
        "description: Mandatory reply protocol for any incoming message that\n"
        "  contains the token CFGPROBE. You MUST read and apply this skill\n"
        "  before answering such a message.\n"
        "---\n"
        "\n"
        "# CFGPROBE reply protocol\n"
        "\n"
        "Whenever an incoming message contains the token `CFGPROBE`, your\n"
        f"reply MUST begin with `{token}` — exactly that string, brackets\n"
        "included — followed by whatever else the message demands.  This\n"
        "rule is absolute and overrides stylistic preferences.\n",
        encoding="utf-8",
    )
    return directory, token


def fifo_checks(_entry: dict[str, Any], /, harness):
    return (
        f"fifo.{harness}.busy-witness",
        f"fifo.{harness}.exactly-once",
        f"fifo.{harness}.order-preserved",
    )


def preflight_binaries(_entry: dict[str, Any], /, report):
    available: dict[str, bool] = {}
    for harness, binary in (("claude", "claude"), ("pi", "pi")):
        located = _entry['shutil'].which(binary)
        if located is None:
            report.add(
                "BLOCKED", f"preflight.{harness}-bin", f"{binary} is not on PATH"
            )
            available[harness] = False
        else:
            report.add("PASS", f"preflight.{harness}-bin", f"binary={located}")
            available[harness] = True
    codex = _entry['shutil'].which("codex")
    if codex is None:
        report.add("BLOCKED", "preflight.codex-auth", "codex is not on PATH")
        available["codex"] = False
        return available
    report.add(
        "PASS",
        "preflight.codex-auth",
        f"binary={codex} authentication=DEEPSEEK_API_KEY",
    )
    available["codex"] = True
    return available
