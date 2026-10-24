# MongoDB Atlas evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 9
and sponsor goal 4 (MongoDB Atlas). Read-only evaluation; no Atlas account is
created, nothing is provisioned, and the live posture is unchanged:
PostgreSQL is the single system of record
([ADR-005](../adr/ADR-005-system-of-record-database.md)).

## What the goal asked

Sponsor goal 4: "MongoDB Atlas for storing investigation documents: store
investigation results as documents, keyed by investigation ID, together with
audit provenance."

## What the product already does with that need

The composed "investigation document" is not a stored document — it is a
composition of normalized, integrity-governed rows, reassembled at read time:

- Run reports and classifications compose from `ti.runs`, `ti.test_results`,
  and `ti.test_identities` through `RunViewer` (one unit of work, tenant
  scoped by construction). The
  [SPONSOR-IMPLEMENTATION-MAP](SPONSOR-IMPLEMENTATION-MAP.md)
  goals 6 and 12 add reliability statistics, day/week trends, and
  prioritization over the same rows, and goal 2's investigation composes
  them — the same data, one copy, one authorization path (re-checked at
  `3b93cb6`; see [TIGER-DATA-EVALUATION](TIGER-DATA-EVALUATION.md)).
- Investigation provenance is **event rows** (`diag.investigation.recorded`,
  `iam.agent.delegated`, `diag.diagnosis.recorded`) in the same transaction
  discipline as the state they describe (ADR-012). Events are append-only;
  retention of audit/outbox rows is not an artifact decision and never runs
  automatically ([EXECUTION-HOST](EXECUTION-HOST.md) "fail-closed inventory";
  DURABLE-JOBS "Migrations and retention").

## Why Atlas is not selected now

1. **No concrete advantage.** The requested capability (structured
   investigation data + provenance) is already provided by the relational
   store with integrity guarantees that bind state and provenance
   transactionally. A document store would duplicate the same values into a
   second schema that must be kept consistent with the first — inverse-ETL
   maintenance with no feature that needs document-native shape today.
2. **A second store is a second authorization boundary.** Every existing
   check — org/repo scoping in the SQL layer, code-host standing, IAM-only
   access types — would need an independently implemented, independently
   tested equivalent against a different engine, or Atlas rows would hold
   tenant data without the scoping spine. Cross-store leakage is the
   specifically named risk of this phase; not adopting a second store is the
   control, not an omission.
3. **Retention rules do not transfer.** The repository never deletes audit or
   outbox rows automatically (a documented human/legal decision). Any store
   introduced later inherits that rule explicitly; until then, the fewer the
   stores carrying provenance-shaped values, the stronger the guarantee.
4. **Operator and cost posture.** Hosting a managed cluster requires
   credentials and an owner-authorized spend (constraints D-HOST-5; every
   external account is owner-gated), plus a new dependency decision under
   ADR-017 Decision 10's vendor gate (terms, data flow, licence purity,
   transmitted-field verification).

## Conditions to revisit

1. A written product requirement whose natural representation is
   document-native (e.g. deeply nested, heterogeneous evidence bundles
   queried by content) with an owner-approved spec naming the queries.
2. A measured capacity or access-pattern limit in PostgreSQL for these read
   models that no index or composition change fixes (real load evidence, not
   projections).
3. Owner-approved spend and credentials, and a data-flow review making the
   field list explicit — the same review the research/speech/live-provider
   providers require before any external system ever receives content.

None hold today. Status: **not selected**; the goal is retained and this
evaluation is the recorded reason it stays that way.
