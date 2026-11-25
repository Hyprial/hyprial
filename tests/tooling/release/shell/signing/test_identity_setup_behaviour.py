"""setup-macos-signing-identity.sh against fake security/openssl/xcrun/codesign.

Runs on every platform. Pins the order of the tool calls, that the keychain
password reaches ``security`` only on the stdin of ``security -i`` (never an
argv, never the script's output), the refusals that must happen before the
keychain is touched (existing key, a directory inside a git work tree or open
to others, a certificate of another key or type, an intermediate that does
not match the pin) and that a second ``import`` neither duplicates nor fails.
"""

from __future__ import annotations

import json
import os
import select
import termios
import subprocess
import time
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.identity_fakes import BASH, DEVID_CN, SCRIPT, Host, fake_cert, key_id


def _security(host: Host) -> list[dict]:
    return [call for call in host.calls() if call["tool"] == "security"]


def _every_argv(host: Host) -> str:
    return "\n".join(" ".join(call["argv"]) for call in host.calls())


def _import(host: Host, cer: Path, *extra: str, stdin: str | None = None, env: dict[str, str] | None = None):
    return host.run("import", "--dir", str(host.private), "--cer", str(cer), *extra, stdin=host.password + "\n" if stdin is None else stdin, extra=env)


def test_csr_creates_a_private_directory_key_and_csr(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.run("csr", "--dir", str(host.private), "--email", "dev@example.invalid", "--common-name", "Example Developer")
    assert result.returncode == 0, result.stdout + result.stderr
    assert oct(host.private.stat().st_mode & 0o777) == "0o700"
    assert oct((host.private / "devid.key").stat().st_mode & 0o777) == "0o600"
    csr = json.loads((host.private / "devid.csr").read_text(encoding="utf-8"))
    assert csr["subj"] == "/emailAddress=dev@example.invalid/CN=Example Developer"
    openssl = [call["argv"] for call in host.calls() if call["tool"] == "openssl"]
    assert [argv[0] for argv in openssl] == ["genrsa", "req"]
    assert openssl[0][-1] == "2048" and "-utf8" in openssl[1]
    text = " ".join(result.stdout.split())
    assert "Developer ID Application" in text and "developer.apple.com" in text
    assert f"{host.private}/developerID_application.cer" in text
    assert not _security(host)


def test_csr_prompts_for_missing_subject_fields(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.run("csr", "--dir", str(host.private), stdin="dev@example.invalid\nExample Developer\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((host.private / "devid.csr").read_text(encoding="utf-8"))["subj"] == "/emailAddress=dev@example.invalid/CN=Example Developer"


@pytest.mark.parametrize(("email", "name"), [("", "Example Developer"), ("not-an-email", "Example Developer"), ("dev@example.invalid", "A/B"), ("dev@example.invalid", "")])
def test_csr_refuses_a_bad_subject(tmp_path: Path, email: str, name: str) -> None:
    host = Host(tmp_path)
    result = host.run("csr", "--dir", str(host.private), "--email", email, "--common-name", name, stdin="\n\n")
    assert result.returncode == 2, result.stdout + result.stderr
    assert not (host.private / "devid.key").exists()


def test_csr_refuses_to_overwrite_an_existing_key(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    before = (host.private / "devid.key").read_text(encoding="utf-8")
    result = host.run("csr", "--dir", str(host.private), "--email", "dev@example.invalid", "--common-name", "Example Developer")
    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert (host.private / "devid.key").read_text(encoding="utf-8") == before


def test_csr_refuses_a_directory_inside_a_git_work_tree(tmp_path: Path) -> None:
    host = Host(tmp_path)
    tree = tmp_path / "checkout"
    (tree / "sub").mkdir(parents=True)
    (tree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    result = host.run("csr", "--dir", str(tree / "sub" / "keys"), "--email", "dev@example.invalid", "--common-name", "Example Developer")
    assert result.returncode == 2
    assert "git work tree" in result.stderr
    assert not (tree / "sub" / "keys").exists()


def test_csr_refuses_a_directory_open_to_others_or_a_symlink(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.private.mkdir(mode=0o700)
    host.private.chmod(0o750)
    result = host.run("csr", "--dir", str(host.private), "--email", "dev@example.invalid", "--common-name", "Example Developer")
    assert result.returncode == 2 and "group or others" in result.stderr
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    for given in (str(link), str(link) + "/", str(link) + "//"):
        result = host.run("csr", "--dir", given, "--email", "dev@example.invalid", "--common-name", "Example Developer")
        assert result.returncode == 2 and "symlink" in result.stderr, given
    assert not any(target.iterdir())


def test_import_calls_in_order_and_keeps_the_password_off_argv_and_output(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    cer = host.developer_id_cert()
    result = _import(host, cer)
    assert result.returncode == 0, result.stdout + result.stderr
    assert host.password not in result.stdout + result.stderr
    assert host.password not in _every_argv(host)
    steps = []
    for call in _security(host):
        if call["argv"] == ["-i"]:
            steps.append("-i " + call["stdin"].split()[0])
            assert host.password.replace("\\", "\\\\").replace('"', '\\"') in call["stdin"]
        else:
            steps.append(" ".join(call["argv"][:1] + [arg for arg in call["argv"][1:] if arg in ("-t", "priv", "cert", "-v")]))
    assert steps == [
        "-i unlock-keychain",
        "find-identity",
        "import -t priv",
        "import -t cert",
        "-i set-key-partition-list",
        "find-certificate",
        "import -t cert",
        "find-identity",
        "find-identity -v",
    ]
    key_import = next(call["argv"] for call in _security(host) if call["argv"][:1] == ["import"] and "priv" in call["argv"])
    assert key_import[key_import.index("-T") + 1] == "/usr/bin/codesign" and "-A" not in key_import
    assert key_import[key_import.index("-k") + 1] == str(host.keychain)
    partition = next(call["stdin"] for call in _security(host) if call["stdin"].startswith("set-key-partition-list"))
    assert '-S apple-tool:,apple: -s -t private -l "Imported Private Key"' in partition
    assert len(host.model()["certs"]) == 2 and len(host.model()["keys"]) == 1
    text = " ".join(result.stdout.split())
    sha1 = json.loads(cer.read_text(encoding="utf-8"))["sha1"]
    assert f"identity {sha1}" in text
    assert "delete or move" in text and str(host.private) in text


def test_a_second_import_is_idempotent(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    cer = host.developer_id_cert()
    assert _import(host, cer).returncode == 0
    again = _import(host, cer)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already installed" in again.stdout
    assert len(host.model()["certs"]) == 2 and len(host.model()["keys"]) == 1
    assert "already exists" not in again.stdout + again.stderr


def test_a_key_left_by_an_interrupted_import_is_tolerated(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    cer = host.developer_id_cert()
    model = {"keys": [key_id(host.private)], "certs": [], "locked": True, "password": host.password}
    (host.state / "keychain.json").write_text(json.dumps(model), encoding="utf-8")
    result = _import(host, cer)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(host.model()["keys"]) == 1 and len(host.model()["certs"]) == 2


def test_a_wrong_password_fails_before_anything_is_imported(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(), stdin="not the password\n")
    assert result.returncode == 1
    assert "could not unlock" in result.stderr
    assert "not the password" not in result.stdout + result.stderr
    assert host.model()["keys"] == [] and host.model()["certs"] == []


def test_a_certificate_of_another_key_is_refused_before_the_keychain_is_touched(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(key="someone-else"))
    assert result.returncode == 2
    assert "does not match the private key" in result.stderr
    assert not _security(host)


def test_a_certificate_of_another_type_is_refused_unless_allowed(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    other = host.developer_id_cert(subject="CN=Apple Development: Example (ABCDE12345)/OU=ABCDE12345")
    refused = _import(host, other)
    assert refused.returncode == 2 and "not a Developer ID Application certificate" in refused.stderr
    assert not _security(host)
    allowed = _import(host, other, "--allow-other-type")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_an_identity_that_is_not_valid_fails_without_allow_other_type(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(valid=False))
    assert result.returncode == 1
    assert "not valid" in result.stderr


def test_an_expired_certificate_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(expired=True))
    assert result.returncode == 2 and "expired" in result.stderr
    assert not _security(host)


def test_a_pem_certificate_is_accepted(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(form="PEM"))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("source", ["download", "file"])
def test_an_intermediate_that_does_not_match_the_pin_is_refused(tmp_path: Path, source: str) -> None:
    host = Host(tmp_path)
    host.make_private()
    forged = fake_cert(tmp_path / "forged.cer", key="forged", subject="CN=Developer ID Certification Authority/OU=G2/O=Apple Inc./C=US", sha256="0" * 64)
    if source == "download":
        result = _import(host, host.developer_id_cert(), env={"FAKE_CURL_FILE": str(forged)})
    else:
        result = _import(host, host.developer_id_cert(), "--intermediate", str(forged))
    assert result.returncode == 2
    assert "does not match the pinned SHA-256" in result.stderr
    assert not _security(host)


def test_an_offline_intermediate_is_used_without_a_download(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(), "--intermediate", str(host.intermediate))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [call for call in host.calls() if call["tool"] == "curl"]


def test_a_failed_download_points_to_the_offline_option(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    result = _import(host, host.developer_id_cert(), env={"FAKE_FAIL": "curl"})
    assert result.returncode == 1
    assert "--intermediate" in result.stderr
    assert not _security(host)


def test_an_explicit_keychain_is_the_only_one_touched(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    separate = tmp_path / "separate.keychain-db"
    separate.write_text("fake keychain\n", encoding="utf-8")
    result = _import(host, host.developer_id_cert(), "--keychain", str(separate))
    assert result.returncode == 0, result.stdout + result.stderr
    for call in _security(host):
        named = [arg for arg in call["argv"] + call["stdin"].replace('"', " ").split() if arg.endswith("keychain-db")]
        assert named == [str(separate)] or call["argv"][:1] == ["list-keychains"], call


def test_check_reports_identities_intermediate_and_signs(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    cer = host.developer_id_cert()
    assert _import(host, cer).returncode == 0
    result = host.run("check")
    assert result.returncode == 0, result.stdout + result.stderr
    sha1 = json.loads(cer.read_text(encoding="utf-8"))["sha1"]
    text = " ".join(result.stdout.split())
    assert "Developer ID Application identities: 1" in text and sha1 in text
    assert DEVID_CN not in text
    assert "intermediate: present" in text
    assert "non-interactive signing: PASS" in text
    codesign = [call["argv"] for call in host.calls() if call["tool"] == "codesign"]
    assert codesign[0][:2] == ["--force", "--sign"] and codesign[0][2] == sha1 and "--timestamp=none" in codesign[0]
    assert codesign[1][:2] == ["--verify", "--strict"]
    assert not [call for call in host.calls() if call["tool"] == "xcrun"]
    assert not [path for path in tmp_path.iterdir() if path.name.startswith("setup-signing.")]


def test_check_signs_with_an_isolated_home_when_the_keychain_is_not_on_the_search_list(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    separate = tmp_path / "separate.keychain-db"
    separate.write_text("fake keychain\n", encoding="utf-8")
    assert _import(host, host.developer_id_cert(), "--keychain", str(separate)).returncode == 0
    result = host.run("check", "--keychain", str(separate))
    assert result.returncode == 0, result.stdout + result.stderr
    sign = next(call for call in host.calls() if call["tool"] == "codesign")
    assert sign["home"] != str(host.home) and "not on the search list" in result.stdout


def test_check_fails_when_the_key_cannot_sign(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    assert _import(host, host.developer_id_cert()).returncode == 0
    result = host.run("check", extra={"FAKE_FAIL": "codesign"})
    assert result.returncode == 1
    assert "non-interactive signing: FAIL" in result.stdout
    assert "unlock" in result.stdout


def test_check_with_a_profile_asks_notarytool_and_reports_failure(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    assert _import(host, host.developer_id_cert()).returncode == 0
    good = host.run("check", "--profile", "hyprial-notary")
    assert good.returncode == 0 and "notary profile hyprial-notary: PASS" in good.stdout
    xcrun = [call["argv"] for call in host.calls() if call["tool"] == "xcrun"]
    assert xcrun == [["notarytool", "history", "--keychain-profile", "hyprial-notary"]]
    bad = host.run("check", "--profile", "hyprial-notary", extra={"FAKE_FAIL": "notarytool"})
    assert bad.returncode == 1 and "notary profile hyprial-notary: FAIL" in bad.stdout


def test_check_with_no_identity_fails(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.run("check")
    assert result.returncode == 1
    assert "Developer ID Application identities: 0" in " ".join(result.stdout.split())


def test_check_is_read_only(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    assert _import(host, host.developer_id_cert()).returncode == 0
    before = len(host.calls())
    assert host.run("check").returncode == 0
    for call in host.calls()[before:]:
        if call["tool"] == "security":
            assert call["argv"][0] in ("find-identity", "find-certificate", "list-keychains"), call


def test_notary_wraps_store_credentials_and_takes_no_password(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.run("notary", "--profile", "hyprial-notary", "--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345")
    assert result.returncode == 0, result.stdout + result.stderr
    xcrun = [call["argv"] for call in host.calls() if call["tool"] == "xcrun"]
    assert xcrun == [["notarytool", "store-credentials", "hyprial-notary", "--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345"]]
    with_keychain = host.run("notary", "--profile", "p", "--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345", "--keychain", str(host.keychain))
    assert with_keychain.returncode == 0
    assert [call["argv"] for call in host.calls() if call["tool"] == "xcrun"][-1][-2:] == ["--keychain", str(host.keychain)]


@pytest.mark.parametrize(
    "args",
    [
        ["--profile", "p", "--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345", "--password", "secret"],
        ["--profile", "p", "--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345", "-p", "secret"],
        ["--profile", "p", "--apple-id", "dev@example.invalid", "--team-id", "abc"],
        ["--profile", "p", "--apple-id", "nobody", "--team-id", "ABCDE12345"],
        ["--profile", "bad name", "--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345"],
        ["--apple-id", "dev@example.invalid", "--team-id", "ABCDE12345"],
    ],
)
def test_notary_refuses_bad_input_and_any_password_option(tmp_path: Path, args: list[str]) -> None:
    host = Host(tmp_path)
    result = host.run("notary", *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "secret" not in result.stdout + result.stderr
    assert not [call for call in host.calls() if call["tool"] == "xcrun"]


@pytest.mark.parametrize("sub", ["csr", "import", "notary", "check"])
def test_every_subcommand_refuses_a_non_macos_host(tmp_path: Path, sub: str) -> None:
    host = Host(tmp_path)
    result = host.run(sub, extra={"FAKE_UNAME": "Linux"})
    assert result.returncode == 2
    assert "macOS only" in result.stderr


def test_the_test_tool_path_is_announced(tmp_path: Path) -> None:
    host = Host(tmp_path)
    result = host.run("check")
    assert "HYPRIAL_SIGNING_SETUP_TOOL_PATH" in result.stderr


def test_without_the_override_the_script_uses_system_tools_only(tmp_path: Path) -> None:
    host = Host(tmp_path)
    env = {key: value for key, value in host.env.items() if key != "HYPRIAL_SIGNING_SETUP_TOOL_PATH"}
    env["PATH"] = f"{host.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = subprocess.run([BASH, str(SCRIPT), "check", "--keychain", str(tmp_path / "missing.keychain-db")], capture_output=True, text=True, env=env, timeout=30, check=False)
    assert host.calls() == []
    assert result.returncode == 2


def test_on_a_terminal_the_password_is_not_echoed_even_when_sent_at_once(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.make_private()
    cer = host.developer_id_cert()
    master, slave = os.openpty()
    output = b""
    try:
        process = subprocess.Popen(
            [BASH, str(SCRIPT), "import", "--dir", str(host.private), "--cer", str(cer)],
            stdin=slave, stdout=slave, stderr=slave, env=host.env, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        deadline = time.monotonic() + 60
        while b"(input hidden): " not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.5)[0]:
                output += os.read(master, 65536)
        os.write(master, (host.password + "\n").encode())
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.5)[0]:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        returncode = process.wait(timeout=60)
        echo_after = bool(termios.tcgetattr(master)[3] & termios.ECHO)
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
    text = output.decode(errors="replace")
    assert returncode == 0, text
    assert "(input hidden): " in text and "Installed identity" in text
    assert host.password not in text
    assert echo_after
