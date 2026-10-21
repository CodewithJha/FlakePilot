"""AUD stores envelopes copied from context outboxes.

The relay in the composition root is the only writer. A row is unique on event id, and
this package does not update or delete records. Outbox pruning, retention deletion, and
authorized audit queries are still deferred.
"""
