"""AUD: audit and observability.

Not implemented yet. Contract from SYSTEM-ARCHITECTURE §5, ADR-005/006 and ADR-012: every
state change writes an event envelope (`flakepilot.shared_kernel.events`) to its context's
outbox in the same transaction as the change, so a failed audit write aborts the change
(fail closed). IAM already does (`iam.outbox`). AUD will append envelopes from the outboxes
to the append-only audit log, deduplicated by event ID, through the relay that arrives with
the ADR-006 job runtime; until then audit is delayed, never lost. Request logs are
operational and are not audit.

Still open: the audit-log schema, outbox pruning, failure parking (RO-2), and calibrated
retention (provisional: 1 year).
"""
