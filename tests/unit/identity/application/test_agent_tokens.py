"""A verified issuer token only ever narrows a delegation."""

from datetime import UTC, datetime, timedelta

import pytest

from flakepilot.identity.application.agent_tokens import narrow
from flakepilot.identity.application.agents import AgentAccess, AgentExpired
from flakepilot.identity.application.authorization import Grant, PermissionDenied

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
REPO = "repo_1"
AGENT = AgentAccess(
    principal_id="user_1",
    org_id="org_1",
    grants=frozenset({(REPO, Grant.VIEW), (REPO, Grant.INVESTIGATE)}),
    agent_id="triage_bot",
    repo_id=REPO,
    expires_at=NOW + timedelta(hours=1),
)


def test_scopes_remove_grants_and_the_earlier_expiry_wins() -> None:
    narrowed = narrow(
        AGENT,
        scopes=frozenset({"repo.view", "openid"}),
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    assert narrowed.grants == {(REPO, Grant.VIEW)}
    assert narrowed.expires_at == NOW + timedelta(minutes=5)
    assert (narrowed.principal_id, narrowed.agent_id) == ("user_1", "triage_bot")


def test_a_token_can_never_widen_scope_or_lifetime() -> None:
    view_only = narrow(
        AGENT, scopes=frozenset({"repo.view"}), expires_at=NOW + timedelta(1), now=NOW
    )
    widened = narrow(
        view_only,
        scopes=frozenset({"repo.view", "repo.investigate", "repo.approve"}),
        expires_at=NOW + timedelta(days=30),
        now=NOW,
    )
    assert widened.grants == {(REPO, Grant.VIEW)}
    assert widened.expires_at == AGENT.expires_at
    assert not widened.org_admin


def test_no_shared_scope_is_a_refusal() -> None:
    for scopes in (frozenset(), frozenset({"repo.approve", "repo.configure", "*"})):
        with pytest.raises(PermissionDenied):
            narrow(AGENT, scopes=scopes, expires_at=NOW + timedelta(1), now=NOW)


def test_an_expired_token_or_delegation_is_refused() -> None:
    with pytest.raises(AgentExpired):
        narrow(AGENT, scopes=frozenset({"repo.view"}), expires_at=NOW, now=NOW)
    with pytest.raises(AgentExpired):
        narrow(
            AGENT,
            scopes=frozenset({"repo.view"}),
            expires_at=NOW + timedelta(days=1),
            now=AGENT.expires_at,
        )
