"""Issuer tokens narrow a delegation; they never widen it (ADR-017 decision 6; goal 9).

A verified Auth0 agent token (integrations' `Auth0TokenVerifier`) is evidence
about the agent software — which client, which scopes the tenant granted it —
never about a FlakePilot member. The only authority stays the delegation
`Delegate.create` minted from a verified member's own grants. A token can
remove grants from that delegation and shorten its life; nothing else. The
decision point stays `authorize_agent`.

Token scopes are matched against grant values (`repo.view`,
`repo.investigate`) of the API the token names in `aud`; unknown scopes are
ignored, and no scope can add a grant the delegation does not already hold.
"""

from dataclasses import replace
from datetime import datetime

from flakepilot.identity.application.agents import AgentAccess, AgentExpired
from flakepilot.identity.application.authorization import PermissionDenied


def narrow(
    agent: AgentAccess,
    *,
    scopes: frozenset[str],
    expires_at: datetime,
    now: datetime,
) -> AgentAccess:
    """The delegation intersected with the token: fewer or equal grants, an
    earlier or equal expiry. Nothing left in common is a refusal."""
    if now >= expires_at or now >= agent.expires_at:
        raise AgentExpired
    grants = frozenset(
        (repo_id, grant) for repo_id, grant in agent.grants if grant.value in scopes
    )
    if not grants:
        raise PermissionDenied
    return replace(agent, grants=grants, expires_at=min(agent.expires_at, expires_at))
