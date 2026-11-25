"""``onboarding.firstRun.begin`` never makes the user agent the first-run agent.

The method records which agent the first message goes to.  It reads that
agent through the daemon's agent registry and accepts only an agent with a
harness and exactly one credential; the user agent has no harness, so the
existing check answers ``FIRST_RUN_CONFIGURATION_CHANGED`` before the attempt
store is touched.  No dedicated code is needed (``OTHER_NOT_A_TARGET`` in
``test_admin_refusals``).  The tests run the attempt service over the real
registry of an application whose user agent was ensured, because the request
handler needs a logged-in onboarding subject.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.first_run.selection import (
    agent_selection_reader,
)
from hyprial.daemon.impl.application.onboarding.first_run.service import (
    FirstRunAttemptService,
    FirstRunAuthorityError,
)
from hyprial.daemon.impl.onboarding.first_run import (
    FIRST_RUN_CONFIGURATION_CHANGED,
    FirstRunStore,
)
from hyprial.daemon.impl.onboarding.first_run_attempt import FILENAME, FirstRunAttemptStore
from hyprial.daemon.impl.user_agent import (
    USER_AGENT_NAME,
    local_user_agent,
    run_user_agent_ensure,
)
from hyprial.identity import OnboardingSubject, SecretResolver, SecretSource
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)

KEY = "opkey-0123456789abcdef"
SUBJECT = OnboardingSubject(
    issuer="https://issuer.example.test",
    user_id="user-123",
    device_uid="12345678123442348234123456789abc",
)


def _service(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    assert run_user_agent_ensure(app) == "created"
    registry = app._agent_session_domains._registry
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None and not registry.get(agent.uri).preferred_harness
    root = tmp_path / "onboarding"
    service = FirstRunAttemptService(
        home=app.hyprial_home,
        onboarding_root=root,
        attempts=FirstRunAttemptStore(root),
        receipts=FirstRunStore(root),
        login_epoch=lambda _home: 1,
        select_agent=agent_selection_reader(registry),
        send_binding=lambda *_args: None,
        reply_for=lambda *_args: None,
    )
    return app, registry, agent, service, root / FILENAME


def _forms(agent) -> dict[str, str]:  # type: ignore[no-untyped-def]
    ident = agent.agent_id
    assert agent.uri.endswith(":" + ident)
    return {
        "name": USER_AGENT_NAME,
        "id": ident,
        "name-uri": f"agent:{OWNER}:{NODE}:{USER_AGENT_NAME}",
        "id-uri": agent.uri,
    }


@pytest.mark.parametrize("form", ["name", "id", "name-uri", "id-uri"])
def test_the_user_agent_is_never_the_first_run_agent(tmp_path: Path, form: str) -> None:
    _app, registry, agent, service, written = _service(tmp_path)
    target = _forms(agent)[form]
    assert registry.get(target).uri == agent.uri

    with pytest.raises(FirstRunAuthorityError) as refused:
        service.begin(SUBJECT, operation_key=KEY, agent_uri=target)

    assert refused.value.code == FIRST_RUN_CONFIGURATION_CHANGED
    assert not written.exists()


def test_an_ordinary_agent_with_a_harness_and_one_key_begins(tmp_path: Path) -> None:
    """The control: the same service and subject record an attempt for a real agent."""

    app, registry, _agent, service, written = _service(tmp_path)
    plain = registry.create(
        "first-run", preferred_harness="pi", provider="deepseek", model="deepseek-flash"
    )
    SecretResolver(app.hyprial_home, registry).write_user_provider("deepseek", {"apiKey": "k"})
    registry.grant_secret(
        plain.actor,
        grant_id="model-deepseek",
        source=SecretSource.USER_PROVIDER,
        entry_id="deepseek",
        field_name="apiKey",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )

    attempt = service.begin(SUBJECT, operation_key=KEY, agent_uri=plain.uri)

    assert attempt.agent_uri == plain.uri and attempt.state == "pending"
    assert written.exists()
