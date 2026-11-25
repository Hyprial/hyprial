"""Run scripts/build/setup-macos-signing-identity.sh against fake host tools.

The script takes every tool from ``/usr/bin:/bin:/usr/sbin:/sbin`` unless the
test-only variable ``HYPRIAL_SIGNING_SETUP_TOOL_PATH`` names a PATH to use
instead. These helpers build that PATH from scratch: one fake written in
Python (``security``, ``openssl``, ``xcrun``, ``codesign``, ``curl``,
``uname``) plus links to the few real utilities the script needs, so no tool
of the machine running the tests can leak in and the tests run on Linux too.

The fakes keep a tiny keychain model in ``<state>/keychain.json`` (keys,
certificates, lock state, password) and append one JSON line per call to
``<state>/calls.jsonl`` with the tool, its argv and whatever it read on
stdin, so a test can prove that a secret travelled only on stdin. A fake
certificate is a JSON file naming its key, subject, fingerprints and
encoding; a fake key is a text file carrying a key id.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from tests.tooling.release.shell.support import ROOT

SCRIPT = ROOT / "scripts" / "build" / "setup-macos-signing-identity.sh"
BASH = shutil.which("bash") or "/bin/bash"
FAKE_NAMES = ("security", "openssl", "xcrun", "codesign", "curl", "uname")
REAL_TOOLS = (
    "awk", "basename", "cat", "chmod", "cp", "dirname", "grep", "head", "ln", "ls",
    "mkdir", "mktemp", "readlink", "rm", "sed", "tail", "tr", "wc", "env", "bash", "sh", "true", "fold", "mv", "rmdir", "stty",
)
PIN = "F16CD3C54C7F83CEA4BF1A3E6A0819C8AAA8E4A1528FD144715F350643D2DF3A"
G2_SHA1 = "5B45F61068B29FCC8FFFF1A7E99B78DA9E9C4635"
DEVID_CN = "Developer ID Application: Example Developer (ABCDE12345)"
G2_PEM = """-----BEGIN CERTIFICATE-----
MIIEPjCCAyagAwIBAgIUf7QAP82XSXrLg02SpIp4c8KEXUMwDQYJKoZIhvcNAQEL
BQAwYjELMAkGA1UEBhMCVVMxEzARBgNVBAoTCkFwcGxlIEluYy4xJjAkBgNVBAsT
HUFwcGxlIENlcnRpZmljYXRpb24gQXV0aG9yaXR5MRYwFAYDVQQDEw1BcHBsZSBS
b290IENBMB4XDTIxMDkyMjE4NTUxMFoXDTMxMDkxNzAwMDAwMFowXjEtMCsGA1UE
AwwkRGV2ZWxvcGVyIElEIENlcnRpZmljYXRpb24gQXV0aG9yaXR5MQswCQYDVQQL
DAJHMjETMBEGA1UECgwKQXBwbGUgSW5jLjELMAkGA1UEBhMCVVMwggEiMA0GCSqG
SIb3DQEBAQUAA4IBDwAwggEKAoIBAQDTLJVoIVSRir9lccIYTvbNDeGRKHXXA7qV
H9MSmVJE3oBGZ/wpiwCFiIowh01Mbkq0x+UC49djTKrM6uzbQi+RfSXpX6RLKlRv
F6wwnykoNKR/j+V4410jKiFG0InDX/hql51xGxiosgzJAwo1sgKnLfWz3BuDxSch
QHKm5zd2Mee7Br90svZwrEysLc3JXkWLz/sI5mLVQJW1N1zMGw7h79yl5Y3JULdA
nwojJUVAj5Rne+CU6jnTeKU00cLL4DN/LrYsw+Um1SXOqo0qd/yeFJaX4PVfLvLo
p3GnO6OZTHoio03vcBHV1oaBgvFnM5MzWkCxksSqc5YdxFz/4es3AgMBAAGjge8w
gewwEgYDVR0TAQH/BAgwBgEB/wIBADAfBgNVHSMEGDAWgBQr0GlHlHYJ/vRrjS5A
pvdHTX8IXjBEBggrBgEFBQcBAQQ4MDYwNAYIKwYBBQUHMAGGKGh0dHA6Ly9vY3Nw
LmFwcGxlLmNvbS9vY3NwMDMtYXBwbGVyb290Y2EwLgYDVR0fBCcwJTAjoCGgH4Yd
aHR0cDovL2NybC5hcHBsZS5jb20vcm9vdC5jcmwwHQYDVR0OBBYEFPg6DGkRduDt
rNHrpln6N9XEVbAeMA4GA1UdDwEB/wQEAwIBBjAQBgoqhkiG92NkBgIGBAIFADAN
BgkqhkiG9w0BAQsFAAOCAQEAwf1DClm/8bG3QxBa1hgyMBRWJuERSGMraXKXSB+O
W8peJhX7dCOAM1QzGaEihlrYuEtpqqfOlpbCCsc1atBvzoppg2JtSjn1/oNN16LU
iB/tiMAP+PkzHrQQt8JcETdENwSwpESdjGgNe30dfsy1k/DWbz0VeoCldNPhj33S
iOgxp5jFH9/iglmugIFgRhbsnrmKQKobKuFhhgqMcA/ZSizsTKIFEdZ5jKVjJSEj
Am+TW1pXoddFbd/M93+fX9vH7WSnb0Qvb009eHgKVrQ71zrIK2Wik+rbWOqO1LnE
wDD8MhpNupIMJDmYgjqgmd7n5OS8DryFr1qrXND828wQww==
-----END CERTIFICATE-----
"""

FAKE_TOOL = r'''
import base64, binascii, json, os, shlex, sys, uuid
from pathlib import Path

TOOL = os.environ["FAKE_TOOL_NAME"]
ARGS = sys.argv[1:]
STATE = Path(os.environ["FAKE_STATE"])
KEYCHAIN = STATE / "keychain.json"


def log(entry):
    with open(STATE / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def load():
    if KEYCHAIN.exists():
        return json.loads(KEYCHAIN.read_text())
    return {"keys": [], "certs": [], "locked": True, "password": os.environ.get("FAKE_KC_PASSWORD", "")}


def save(model):
    KEYCHAIN.write_text(json.dumps(model))


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def read_cert(path):
    try:
        text = Path(path).read_text()
        if text.startswith("-----BEGIN CERTIFICATE-----"):
            body = "".join(text.splitlines()[1:-1])
            return dict(json.loads(base64.b64decode(body, validate=True)), format="PEM")
        return json.loads(text)
    except (OSError, ValueError, UnicodeError, binascii.Error):
        return None


def failed(name):
    return name in os.environ.get("FAKE_FAIL", "").split()


def colon(hexstring):
    return ":".join(hexstring[i:i + 2] for i in range(0, len(hexstring), 2))


def openssl(args):
    command = args[0]
    if command == "genrsa":
        Path(option(args, "-out")).write_text("-----BEGIN RSA PRIVATE KEY-----\nfake:" + os.environ.get("FAKE_KEY_ID", uuid.uuid4().hex) + "\n-----END RSA PRIVATE KEY-----\n")
        return 0
    if command == "req":
        key = Path(option(args, "-key")).read_text().split("fake:")[1].split()[0]
        Path(option(args, "-out")).write_text(json.dumps({"csr": True, "key": key, "subj": option(args, "-subj")}))
        return 0
    if command == "pkey":
        key = Path(option(args, "-in")).read_text().split("fake:")[1].split()[0]
        print("-----BEGIN PUBLIC KEY-----\npub:" + key + "\n-----END PUBLIC KEY-----")
        return 0
    if command == "x509":
        cert = read_cert(option(args, "-in"))
        if cert is None or cert.get("format") != option(args, "-inform", "PEM"):
            print("unable to load certificate", file=sys.stderr)
            return 1
        if "-out" in args:
            Path(option(args, "-out")).write_text(json.dumps(dict(cert, format=option(args, "-outform", "PEM"))))
            return 0
        if "-subject" in args:
            print("subject= /" + cert["subject"])
        if "-fingerprint" in args and "-sha1" in args:
            print("SHA1 Fingerprint=" + colon(cert["sha1"]))
        if "-fingerprint" in args and "-sha256" in args:
            print("SHA256 Fingerprint=" + colon(cert["sha256"]))
        if "-pubkey" in args:
            print("-----BEGIN PUBLIC KEY-----\npub:" + cert["key"] + "\n-----END PUBLIC KEY-----")
        if "-checkend" in args and cert.get("expired"):
            print("Certificate will expire")
            return 1
        return 0
    return 2


def common_name(cert):
    return cert["subject"].split("CN=")[1].split("/")[0]


def security_command(args, model):
    command, rest = args[0], args[1:]
    if command == "unlock-keychain":
        if option(rest, "-p") != model["password"]:
            print("security: SecKeychainUnlock: The user name or passphrase you entered is not correct.", file=sys.stderr)
            return 51
        model["locked"] = False
        return 0
    if command == "import":
        if model["locked"]:
            print("security: SecKeychainItemImport: User interaction is not allowed.", file=sys.stderr)
            return 1
        source = rest[0]
        if option(rest, "-t") == "priv":
            key = Path(source).read_text().split("fake:")[1].split()[0]
            if key in model["keys"]:
                print("security: SecKeychainItemImport: The specified item already exists in the keychain.", file=sys.stderr)
                return 1
            model["keys"].append(key)
            print("1 key imported.")
            return 0
        cert = read_cert(source)
        if any(item["sha1"] == cert["sha1"] for item in model["certs"]):
            print("security: SecKeychainItemImport: The specified item already exists in the keychain.", file=sys.stderr)
            return 1
        model["certs"].append(cert)
        print("1 certificate imported.")
        return 0
    if command == "set-key-partition-list":
        if failed("partition"):
            return 1
        for _ in model["keys"]:
            print('keychain: "' + rest[-1] + '"')
        return 0
    if command == "find-identity":
        identities = [cert for cert in model["certs"] if cert.get("key") in model["keys"]]
        valid = [cert for cert in identities if cert.get("valid") and any(item["sha256"] == os.environ.get("FAKE_PIN") for item in model["certs"])]
        listed = valid if "-v" in rest else identities
        print("\nPolicy: Code Signing\n  Matching identities")
        for number, cert in enumerate(listed, 1):
            print(f"  {number}) {cert['sha1']} \"{common_name(cert)}\"")
        print(f"     {len(listed)} identities found\n\n  Valid identities only")
        for number, cert in enumerate(valid, 1):
            print(f"  {number}) {cert['sha1']} \"{common_name(cert)}\"")
        print(f"     {len(valid)} valid identities found")
        return 0
    if command == "find-certificate":
        name = option(rest, "-c")
        for cert in model["certs"]:
            if common_name(cert) == name:
                print("SHA-256 hash: " + cert["sha256"] + "\nSHA-1 hash: " + cert["sha1"])
        return 0
    if command == "list-keychains":
        for path in os.environ.get("FAKE_SEARCH_LIST", "").split(":"):
            if path:
                print('    "' + path + '"')
        return 0
    return 2


def security(args, stdin):
    model = load()
    if args == ["-i"]:
        status = 0
        for line in stdin.splitlines():
            status = security_command(shlex.split(line), model)
        save(model)
        return status
    status = security_command(args, model)
    save(model)
    return status


def main():
    stdin = sys.stdin.read() if TOOL == "security" and ARGS == ["-i"] else ""
    log({"tool": TOOL, "argv": ARGS, "stdin": stdin, "home": os.environ.get("HOME", "")})
    if TOOL == "uname":
        print(os.environ.get("FAKE_UNAME", "Darwin"))
        return 0
    if TOOL == "openssl":
        return openssl(ARGS)
    if TOOL == "security":
        return security(ARGS, stdin)
    if TOOL == "curl":
        if failed("curl"):
            return 22
        Path(option(ARGS, "-o")).write_bytes(Path(os.environ["FAKE_CURL_FILE"]).read_bytes())
        return 0
    if TOOL == "codesign":
        if "--sign" in ARGS and load()["locked"]:
            print("errSecInternalComponent", file=sys.stderr)
            return 1
        if failed("codesign"):
            print("errSecInternalComponent", file=sys.stderr)
            return 1
        return 0
    if TOOL == "xcrun":
        if failed("notarytool"):
            print("Error: HTTP status code: 401. Unable to authenticate.", file=sys.stderr)
            return 69
        return 0
    return 2


sys.exit(main())
'''


def fake_cert(path: Path, *, key: str, subject: str, valid: bool = False, expired: bool = False, form: str = "DER", sha1: str | None = None, sha256: str | None = None) -> Path:
    """Write a fake certificate the fake ``openssl`` and ``security`` understand."""

    digest = uuid.uuid4().hex.upper()
    body = {
        "key": key,
        "subject": subject,
        "sha1": sha1 or digest[:40].ljust(40, "0"),
        "sha256": sha256 or (digest * 2)[:64],
        "valid": valid,
        "expired": expired,
        "format": form,
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def key_id(directory: Path) -> str:
    """The fake key id written by the fake ``openssl genrsa`` in ``directory``."""

    return (directory / "devid.key").read_text(encoding="utf-8").split("fake:")[1].split()[0]


class Host:
    """A from-scratch tool PATH, a fake keychain and a private work area."""

    def __init__(self, tmp_path: Path, *, password: str = "kc pass \"q\" \\ $x") -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.home = tmp_path / "home"
        self.password = password
        for directory in (self.bin, self.state, self.home / "Library" / "Keychains"):
            directory.mkdir(parents=True)
        self.keychain = self.home / "Library" / "Keychains" / "login.keychain-db"
        self.keychain.write_text("fake keychain\n", encoding="utf-8")
        self.private = tmp_path / "private"
        fake = self.bin / "_fake_tool.py"
        fake.write_text(FAKE_TOOL, encoding="utf-8")
        for name in FAKE_NAMES:
            launcher = self.bin / name
            launcher.write_text(f'#!/bin/sh\nFAKE_TOOL_NAME={name} exec "{sys.executable}" -I "{fake}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        for name in REAL_TOOLS:
            found = shutil.which(name)
            if found and not (self.bin / name).exists():
                (self.bin / name).symlink_to(found)
        self.intermediate = fake_cert(tmp_path / "g2.cer", key="apple-g2", subject="CN=Developer ID Certification Authority/OU=G2/O=Apple Inc./C=US", sha1=G2_SHA1, sha256=PIN)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C",
            "TMPDIR": str(tmp_path),
            "HYPRIAL_SIGNING_SETUP_TOOL_PATH": str(self.bin),
            "FAKE_STATE": str(self.state),
            "FAKE_KC_PASSWORD": password,
            "FAKE_PIN": PIN,
            "FAKE_CURL_FILE": str(self.intermediate),
            "FAKE_SEARCH_LIST": str(self.keychain),
        }

    def run(self, *args: str, stdin: str = "", extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run the script with this host's PATH and environment."""

        env = dict(self.env)
        env.update(extra or {})
        return subprocess.run([BASH, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60, check=False)

    def calls(self) -> list[dict]:
        """Every recorded fake call, in order."""

        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def model(self) -> dict:
        """The fake keychain's state."""

        return json.loads((self.state / "keychain.json").read_text(encoding="utf-8"))

    def make_private(self) -> Path:
        """Create the private directory with a fake key and CSR through the script."""

        result = self.run("csr", "--dir", str(self.private), "--email", "dev@example.invalid", "--common-name", "Example Developer")
        assert result.returncode == 0, result.stdout + result.stderr
        return self.private

    def developer_id_cert(self, **overrides: object) -> Path:
        """A fake Developer ID Application certificate for the key in the private directory."""

        values: dict = {"key": key_id(self.private), "subject": f"UID=ABCDE12345/CN={DEVID_CN}/OU=ABCDE12345/O=Example Developer/C=US", "valid": True}
        values.update(overrides)
        return fake_cert(self.tmp / "developerID_application.cer", **values)


def isolated_env(home: Path) -> dict[str, str]:
    """A from-nothing environment whose HOME is ``home`` (real macOS tools)."""

    return {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
