"""Synthetic config/transcript writer and forbidden-host-path audit, no vendor CLI."""
from pathlib import Path


def audit_prefix(bait: Path, violation: Path) -> str:
    # Install in both the daemon and fake worker. Reads are observed, not just
    # inferred from an unchanged final tree. No credential contents are logged.
    return (
        "import os, sys\n"
        f"bait = {str(bait.resolve())!r}\n"
        "def audit(event, args):\n"
        " if event not in {'open', 'os.listdir', 'os.scandir', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir'}: return\n"
        " for value in args:\n"
        "  if not isinstance(value, (str, bytes)): continue\n"
        "  path = os.path.realpath(os.fsdecode(value))\n"
        "  if path == bait or path.startswith(bait + os.sep):\n"
        f"   with open({str(violation)!r}, 'a') as out: out.write(event + '\\n')\n"
        "   raise RuntimeError('synthetic host bait accessed')\n"
        "sys.addaudithook(audit)\n"
    )


def prepare_bait_worker(root: Path, home: Path, protocol: Path) -> tuple[Path, str]:
    bait = home / ".claude"
    bait.mkdir(mode=0o700)
    (bait / ".credentials.json").write_text('{"synthetic_state":"host-bait"}')
    prefix = audit_prefix(bait, root / "host-bait-accessed")
    worker = root / "fake_with_state.py"
    worker.write_text(prefix +
        "from pathlib import Path\nimport runpy\n"
        "native = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude')))\n"
        "native.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
        "config = native / '.synthetic-config'\n"
        "config.write_text('synthetic config')\n"
        "assert config.read_text() == 'synthetic config'\n"
        "project = native / 'projects' / 'synthetic'\n"
        "project.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
        "(project / 'transcript.jsonl').write_text('{\"synthetic\":true}\\n')\n"
        f"runpy.run_path({str(protocol)!r}, run_name='__main__')\n")
    return worker, prefix
