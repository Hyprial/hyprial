"""A selected model's address must come from the same explicitly granted Key."""

from dataclasses import replace

import pytest

from hyprial.identity import SecretResolutionError, SecretSource
from hyprial.identity.impl.agents.runtime.secrets import ResolvedSecret, SecretGrant
from hyprial.kernel import HarnessLaunchSpec
from hyprial.identity.impl.agents.runtime.model_records import ModelServiceRoute


def resolved(*, names=("DEEPSEEK_API_KEY",), grant_id="model", route=True):
    grant = SecretGrant("agent", "incarnation", grant_id, SecretSource.USER_PROVIDER,
                        "arbitrary-name", "TOKEN", names, 1, route_required=route)
    return ResolvedSecret(grant, "synthetic-key", ModelServiceRoute(
        "https://model.example/v1", "openai-completions", "deepseek",
    ) if route else None)


def spec(**kwargs):
    return HarnessLaunchSpec(harness="pi", name="agent", model_provider="deepseek",
                             model="deepseek-flash", headless=True, **kwargs)


def test_route_is_selected_by_model_and_grant_not_entry_name():
    from hyprial.daemon.impl.harnesses.model_routes import route_environment

    assert route_environment(spec(), (resolved(),)) == {
        "HYPRIAL_OPENAI_BASE_URL": "https://model.example/v1",
    }


@pytest.mark.parametrize("changed", [
    {"harness": "codex"}, {"model_provider": "openai"},
    {"model_provider": None}, {"model": None}, {"model": "unknown-model"},
])
def test_route_refuses_wrong_or_implicit_launch(changed):
    from hyprial.daemon.impl.harnesses.model_routes import route_environment

    with pytest.raises(SecretResolutionError):
        route_environment(replace(spec(), **changed), (resolved(),))


@pytest.mark.parametrize("secrets", [
    (resolved(names=("OPENAI_API_KEY",)),),
    (resolved(names=("DEEPSEEK_API_KEY", "OPENAI_API_KEY")),),
    (resolved(), resolved(grant_id="other", route=False)),
])
def test_route_cannot_borrow_another_grants_key(secrets):
    from hyprial.daemon.impl.harnesses.model_routes import route_environment

    with pytest.raises(SecretResolutionError):
        route_environment(spec(), secrets)


def test_ordinary_grants_do_not_override_deployment_address():
    from hyprial.daemon.impl.harnesses.model_routes import route_environment

    assert route_environment(spec(), (resolved(route=False),)) == {}
