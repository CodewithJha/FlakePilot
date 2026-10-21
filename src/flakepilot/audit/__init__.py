"""AUD: audit and observability.

Not implemented yet. Contract from SYSTEM-ARCHITECTURE §5 and ADR-005/006: every state
change writes an event envelope (event ID, org ID, repo ID, actor, action, target, before
and after, reason, correlation ID, timestamp; never secret values or raw untrusted
content) to the outbox in the same transaction as the change, so a failed audit write
aborts the change (fail closed). AUD appends envelopes to the append-only audit log from
the outbox, deduplicated by event ID. Request logs are operational and are not audit.
Undecided: table schemas, event names (BC-Q3, BC-Q4), retention, failure parking (RO-2).

Security-relevant changes that already happen without durable audit (to add when AUD
lands): sign-in (user created or login refreshed, session created), session rotation,
sign-out, CI upload token creation and revocation, secret writes, and operator org
bootstrap (`bootstrap-org`: org, admin user and admin membership created). Grant, membership
and privilege-change revocations will need it when their use cases exist.
"""
