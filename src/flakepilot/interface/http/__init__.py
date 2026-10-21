"""HTTP interface: routes and their request/response models.

Conventions
- Pydantic models live here only; domain and application layers never import FastAPI or
  Pydantic (import-linter). Request models set `extra="forbid"`.
- JSON only. Each route declares its success status code. Errors use the stable
  contract in `request_context`.

Deferred until the first endpoint that needs them (not implemented):
- Idempotency (PRODUCT-SCOPE Workflows B and H; SYSTEM-ARCHITECTURE §5; ADR-005/006):
  keys are scoped to tenant + operation; a repeat with the same key and payload hash
  returns the existing result; the same key with a different hash is rejected with a
  stable conflict code; keys are stored in PostgreSQL under a unique constraint and
  written in the use case's own transaction. Deduplication is not exactly-once side
  effects; external writes use find-or-create. Retention is not decided.
- Pagination: no list endpoint exists and the docs set no convention. Decide with the
  first one; candidate: cursor (keyset) pagination over a stable order with a bounded
  page size.
- Request body size limits: added with the first endpoint that accepts a body
  (ingestion, PRODUCT-SCOPE Workflow B).
"""
