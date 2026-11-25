# Feishu login hint reading

## Implementation

Changed only the user-facing Feishu device-login hint and its pinned
assertions. The device-flow, polling intervals, credential handling, token
storage, and network code are unchanged. The hint now directs users to choose
Feishu sign-in and states that the sign-in approves the device. The existing
signup URL remains in the hint.

New-text grep command:

```text
rg -n -F -e 'that sign-in now approves this device' -e 'sign in with Feishu' src/hyprial/shell/impl/login/oidc.py src/hyprial/identity/impl/onboarding/credentials/oidc.py src/hyprial/shell/impl/cli/commands/common/support.py
```

New-text grep output (exit 0):

```text
src/hyprial/shell/impl/cli/commands/common/support.py:492:        "on the sign-in page choose \"sign in with Feishu\" "
src/hyprial/shell/impl/cli/commands/common/support.py:493:        "(使用飞书登录); that sign-in now approves this device. "
src/hyprial/identity/impl/onboarding/credentials/oidc.py:350:                "in with Feishu, that sign-in now approves this device",
src/hyprial/shell/impl/login/oidc.py:365:                "that sign-in now approves this device",
src/hyprial/shell/impl/login/oidc.py:424:                    "that sign-in now approves this device",
```

Old-text grep command:

```text
rg -n -F -e 'did not approve this device' -e 'currently does not approve this device' -e 'sign in with an existing account rather than Feishu' -e '使用已有账户登录' src/hyprial/shell/impl/login/oidc.py src/hyprial/identity/impl/onboarding/credentials/oidc.py src/hyprial/shell/impl/cli/commands/common/support.py
```

Old-text grep output: exit 1, no output.

Test result:

```text
NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 UV_CACHE_DIR=/private/tmp/uv-cache-feishu-login TMPDIR=/private/tmp/feishu-login-tmp uv run --python 3.12 --extra test pytest tests/shell/login/device/test_polling.py tests/shell/login/test_login_cli.py
25 passed, 2 warnings in 15.67s
```

The test-only assertion change produced the required RED result first: 22
passed and 3 failed. The implementation change then produced the GREEN result
above.
