<!-- Required first line: replace <URI> with the submitting agent's URI. The
     first non-empty line after this comment (and the BOM/whitespace) must be
     `Submitted-by: agent:<URI>`; Forgejo's author is the shared account, so
     the body is the only place the submitting agent's identity can live.
     scripts/ci/check_pr_hygiene.py submitted-by enforces this: a PR created
     after the transition cutoff fails with `missing` or
     `present-but-not-first-line`, whichever applies. -->
Submitted-by: agent:<URI>

<!-- Declaration lines are matched per line, so they follow this comment fine.
     skill-declaration accepts EXACTLY two forms:
         Skill-Impact: updated
         Skill-Impact: not-needed: <reason>
     Do NOT append free text after `updated` -- `Skill-Impact: updated: <reason>`
     is rejected (#1310); put the reason on `not-needed` instead.
     contract-impact is per line too: use
         Contract-Impact: <why this release is safe>
     only when published field semantics change without a same-PR `contract/`
     sync. See .agents/skills/forgejo-ops/SKILL.md. -->

## Summary

<!-- What changed, and why? -->

## Evidence

<!-- Separate measured results from inference and unverified assumptions. -->

## Local gates:

<!-- Required for PRs targeting intg/batch-*: list each local command, test paths,
its PASS/FAIL/BLOCKED/NOT_RUN result, and exact pass/skip counts. -->

## Checklist

- [ ] Production issue fixes include a regression test with red-to-green evidence.
- [ ] Measured facts and inference are clearly separated.
- [ ] Broad `except` blocks were audited; retained cases are explicitly justified.
- [ ] Windows portability: no new platform branch (`os.name`/`sys.platform`/`platform.system()`),
      Windows-only API (`msvcrt`/`winreg`/`subprocess.CREATE_*`/`ctypes.windll`), or explicit
      platform-refusal `raise` outside `kernel/impl/manysys/**` (`scripts/ci/check_windows_portability.py`
      W015/W019/W021, AST); no handwritten local IPC (`AF_UNIX` in any spelling, asyncio/socketserver unix
      endpoints, `connect_named_pipe`/`listen_named_pipe`, `win32pipe`) outside the seam — use
      `hyprial.kernel.listen_local_ipc` / `connect_local_ipc` (W020, AST, no baseline); no file deleted
      or replaced (`unlink`/`os.replace`) while a child process may still have it open (W017, checklist);
      a spawned process is identified by a launch nonce, never by `Popen.pid` (W018, checklist).
