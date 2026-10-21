"""HTTP interface: routes and their request/response models.

Conventions
- Pydantic models live here only; domain and application layers never import FastAPI or
  Pydantic (import-linter). Request models set `extra="forbid"`.
- JSON only. Each route declares its success status code. Errors use the stable
  contract in `request_context`.

- Idempotency (PRODUCT-SCOPE Workflows B and H; SYSTEM-ARCHITECTURE §5): keys are scoped
  to tenant + operation; a repeat with the same key and payload hash returns the existing
  result (200); the same key with a different hash is 409 `idempotency_conflict`; keys
  are unique in PostgreSQL and written in the use case's own transaction. First user:
  `POST /ingest/junit` (ADR-013). Deduplication is not exactly-once side effects.
- Request body size: limited per route and enforced while reading, not from
  Content-Length alone (ingestion: 10 MiB, ADR-013).

Deferred until the first endpoint that needs it: pagination. No list endpoint exists and
the docs set no convention; candidate: cursor (keyset) pagination over a stable order with
a bounded page size.
"""
