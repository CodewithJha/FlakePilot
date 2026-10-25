# MongoDB Atlas evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 9
and sponsor goal 4 (MongoDB Atlas). PostgreSQL stays the single system of
record ([ADR-005](../adr/ADR-005-system-of-record-database.md)).

## Decision update (2026-10-25): implemented as an opt-in, write-only export

The owner approved Atlas in a bounded role: an opt-in, **write-only export**
of finished reports. Atlas is never read back for any decision, and
PostgreSQL stays authoritative. The driver first failed the runtime licence
gate (`dnspython`, a hard dependency of `pymongo`, reports `ISC`); the owner
then added `ISC` to the `allow-only` list (2026-10-25), and `pymongo` 4.19.0
was added. The Atlas Data API (HTTP) was retired by MongoDB, so the driver is
the only path.

What is implemented (commit `a9c4ff8`):

- **What is exported.** Investigation reports are not persisted in
  PostgreSQL (the route composes them per request), so the stored, finished
  document is the classified run report. `ReportExport`
  (`test_intelligence/application/report_export.py`) exports IDs, counts,
  enum values, the commit SHA, the environment label, each test's attempt
  outcomes and its `ti-classification/1` verdict with evidence. Test class
  and method names, upload metadata (CI run ID, job, ref), artifact keys,
  raw reports and logs are not exported; a unit test checks that.
- **Key and idempotency.** `_id` is `org_id:repo_id:run_id:report_revision`,
  where `report_revision` is the SHA-256 of the canonical exported JSON. The
  write is `update_one(..., {"$setOnInsert": doc}, upsert=True)`, so a
  re-export of an unchanged report writes nothing, and a verdict that changed
  as history grew becomes a new revision. A concurrent duplicate insert
  counts as unchanged.
- **Trigger.** The operator command `export-reports-atlas --org-id --repo-id
  [--limit 1-200]` (`composition_root/atlas_export.py`), not the outbox.
- **Audit.** Every outcome is one TI outbox event: `ti.report.exported`
  (`inserted` or `unchanged`) or `ti.report.export_failed` with a reason
  code (`atlas_unreachable`, `atlas_timeout`, `atlas_unauthorized`,
  `atlas_refused`, `atlas_misconfigured`, `atlas_error`). The first failure
  stops the batch; driver messages are never kept.
- **Connection.** `FLAKEPILOT_ATLAS_URI` and `FLAKEPILOT_ATLAS_DATABASE`
  together or not at all (half set refuses startup). The URI must be
  `mongodb+srv://` or `mongodb://`, and `tls=false`, `ssl=false`,
  `tlsInsecure` and `tlsAllowInvalid*` are refused. The client is built with
  `tls=True`, `serverSelectionTimeoutMS=10000`, `connectTimeoutMS=10000`,
  `socketTimeoutMS=20000`, one fixed collection (`run_reports`). Only the
  operator command builds it; the API process and the worker never import
  the driver (a unit test checks the loaded modules).

Evidence (2026-10-25):

- **VERIFIED:** unit tests over a hand-written fake collection and an
  in-memory sink (`tests/unit/integrations/infrastructure/test_atlas.py`,
  `tests/unit/test_intelligence/application/test_report_export.py`), and a
  real-PostgreSQL test that backfills six runs and exports them twice
  (6 inserted, then 6 unchanged, 12 audit events, no test names in any
  document).
- **VERIFIED, live:** `tests/live/test_live_atlas.py` against the owner's
  cluster from the owner's allow-listed IP: one synthetic report upserted
  (inserted), upserted again (unchanged, one document), read back by key
  (the test's check only), then that document deleted.
- **VERIFIED, live end to end:** `export-reports-atlas` through the operator
  CLI on a throwaway PostgreSQL 16 database holding one real backfilled run
  (FlakePilot's own CI, 1397 tests): first run `inserted`, second run
  `unchanged`; the document was found by key and then deleted (0 left for
  that org). The same canonical document, rebuilt from the stored run,
  contains none of the run's 1513 class and test names.

The analysis below was written before this decision. Its risks (a second
authorization boundary, retention) still bind the export: an Atlas document
is a copy, scoped by the same pair, and is never a source of truth.

## Earlier evaluation (2026-10-24, superseded as to the decision)

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

Condition 3 (owner-approved spend and credentials) now holds; see the
decision update at the top. Status: **implemented as an opt-in, write-only
export**, mock-tested, real-PG-tested, live-verified (adapter and the
operator command); not deployed.
