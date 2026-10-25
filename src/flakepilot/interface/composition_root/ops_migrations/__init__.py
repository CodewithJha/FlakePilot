"""Migrations for the `ops` schema, owned by the composition root.

Not a bounded context: `ops.verification_runs` is operator tooling (execution
evidence for one-off jobs), not product data. Kept out of every context's
schema and of alembic autogenerate's scope on purpose.
"""
