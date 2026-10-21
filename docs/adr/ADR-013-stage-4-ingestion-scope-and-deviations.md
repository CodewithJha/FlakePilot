# ADR-013: Stage 4 ingestion scope and deviations

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-21 (choices 1a, 2b, 3a, 4a, 5a). Limits and thresholds are **provisional** |
| Related | [PRODUCT-SCOPE §5.1, Workflows B and C, D4, D5](../product/PRODUCT-SCOPE.md), [SYSTEM-ARCHITECTURE §4, §6, §8 B](../architecture/SYSTEM-ARCHITECTURE.md), [BOUNDED-CONTEXTS › TI](../architecture/BOUNDED-CONTEXTS.md), [ADR-005](ADR-005-system-of-record-database.md), [ADR-006](ADR-006-durable-jobs-in-the-database.md), [ADR-009](ADR-009-initial-provider-adapters.md), [ADR-012](ADR-012-event-envelope-and-outbox.md) |

## Context

Workflow B, as documented, depends on pieces that don't exist yet:
- an artifact store for the raw report;
- the ADR-006 job runtime for asynchronous normalization and the SHA check;
- REPO for repo state and SHA reachability;
- IAM quotas.

Workflow C classifies only inside user-opened investigations, which belong to ORCH. Stage 4 builds push ingestion of JUnit XML and the classifier anyway; this ADR records where it departs from the documented behaviour.

## Deviations

| # | Deviation | What restores the documented behaviour |
|---|---|---|
| 1 | **Artifact storage** is the INT artifact port with `put` and `get` only, plus a local-filesystem adapter (`FLAKEPILOT_ARTIFACT_DIR`, checked at startup). Not built: encryption at rest, deletion, retention, scoped read handles, listing, and the S3-compatible adapter. The name of TI's raw-report deletion event stays open (BC-Q4). Duplicates and idempotency conflicts are detected before the artifact is written. Orphans remain possible only when a transaction rolls back, or when a concurrent submission with the same key wins the claim; keys are content-addressed, so a retry reuses them. The root must be owned by the service user and not group- or world-writable; stale temporary files are swept at startup | Retention jobs (ADR-006 runtime), the S3 adapter, and encryption at rest |
| 2 | **Normalization is synchronous** in the accepting transaction. An upload goes straight from `received` to `processed` in one transaction, or is `rejected`. `processing` and `processing_failed` are never used | The ADR-006 job runtime: accept as `received`, then normalize in a worker |
| 3 | **No repo-state or quota gates.** The CI upload token's scope and revocation, plus fixed size limits, are the only gates | REPO connection state (`active` or `degraded`) and IAM ingestion-rate and storage quotas (`quota.denied`) |
| 4 | **Every run's revision is `unchecked`**, and the classifier doesn't penalize unchecked revisions. Revision order is approximated by when each SHA was first ingested | A REPO "SHA reachable" check in a worker (`verified` or `unverified_revision`), and code-host ancestry for ordering |
| 5 | **Classification is computed on demand** by a read-only TI query (needs `repo.view`). It isn't stored, emits no event, and has no HTTP endpoint (code-host access comes from the future `CodeHostAccessVerifier`) | Workflow C: ORCH's evidence stage seals evidence bundles and records classifications (`evidence.sealed`, `classification.recorded`) |

## Decisions within the documented design

**Endpoint:** `POST /ingest/junit`.
- **Auth:** `Authorization: Bearer fpi_…`. The token alone decides the org and repo; the client never names either.
- **Content:** type `application/xml` or `text/xml`, no content encoding.
- **Metadata:** strictly validated headers, each sent exactly once. Error messages name the header, never the value.

| Header | Format |
|---|---|
| `X-FlakePilot-CI-Source` | `[a-z][a-z0-9_-]{0,31}` |
| `X-FlakePilot-CI-Run-Id` | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` |
| `X-FlakePilot-CI-Attempt` | integer from 1 to 9999 |
| `X-FlakePilot-Job-Name` | printable ASCII, up to 200 characters, no surrounding spaces |
| `X-FlakePilot-Commit-Sha` | 40 or 64 lowercase hex characters |
| `X-FlakePilot-Ref` | `[A-Za-z0-9._/+-]{1,255}`, without `..` |
| `X-FlakePilot-Environment` | `[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,99}` |
| `X-FlakePilot-Started-At`, `X-FlakePilot-Finished-At` | ISO 8601 with an offset; finished is not before started. Recorded only, never used for ordering |

**Limits (provisional):**

| Limit | Value |
|---|---|
| Body size | 10 MiB. A declared `Content-Length` over the limit is refused unread; otherwise the body is read in chunks and dropped once the limit is crossed |
| Element depth | 32 |
| Elements | 200,000 |
| Test cases | 20,000 |
| Name or classname length | 1,000 characters |
| Attempts per test case | 50 in one `<testcase>`, 100 per test identity after merging |

**Hardened parsing:** the stdlib expat parser, in the INT adapter. Any DOCTYPE, entity, unparsed-entity or notation declaration, and any external entity reference, is refused before anything is expanded or fetched. Text content is never read.

**Idempotency key:** (org, repo, CI source, CI run ID, attempt, job name). It is unique among non-rejected uploads in PostgreSQL, and concurrent submissions resolve through `INSERT … ON CONFLICT`.

| Submission | Result |
|---|---|
| New key | 201 |
| Same key, same SHA-256 | 200 `duplicate: true`, returning the original upload and run |
| Same key, different hash | 409 `idempotency_conflict` |

**Other responses:**

| Status | Code | When |
|---|---|---|
| 401 | — | Missing, unknown or revoked token |
| 413 | `payload_too_large` | Body over the limit |
| 415 | `unsupported_media_type` | Wrong content type, or a content encoding |
| 422 | `validation_error` | Invalid metadata; the header is named in `fields` |
| 422 | `invalid_report` | The report was refused, with a `reason` such as `forbidden_dtd`, `malformed_xml`, `too_deep` or `not_junit` |
| 503 | `ingestion_not_configured` | No artifact directory is configured |

Every authenticated attempt is recorded as an upload row. Rejections get their own transaction.

**Test identity:** (classname, name) within an (org, repo), with surrounding whitespace removed and case kept. The file and suite are not part of it, so a renamed or moved test is a new identity (Q5).
- **Repeated cases:** a test case that appears twice in one report contributes successive attempts, in document order.
- **Surefire rerun elements** express retries within a run:
  - `flakyFailure` and `flakyError` are failed attempts before a final pass;
  - `rerunFailure` and `rerunError` are further failed attempts after the first failure.
- **Outcomes:** `<error>` maps to `errored`, `<failure>` to `failed`, and `<skipped>` to `skipped`.

**Classifier `ti-classification/1`** (pure; rules are in `test_intelligence/domain/classification.py`):
- **Evidence:** only CI-uploaded runs count (D5); sandbox-sourced runs are listed as excluded.
- **`flaky`:** a pass and a failure at the same SHA in the same environment.
- **`deterministic_regression`:** every attempt at the target SHA fails, at least 2 of them, all of one kind. Failed versus errored stands in for a failure signature in v1. There must also be a pass at an earlier-seen SHA in the same environment.
- **D4:** without that earlier pass, the result is `inconclusive` / `no_passing_baseline`.
- **Other `inconclusive` reasons:** `insufficient_history` for too little evidence; `conflicting_signals` for mixed kinds, passes in another environment, or flakiness at another SHA.
- **`infrastructure_failure` is never produced from CI uploads.** It needs explicit environment signals, and neither JUnit nor the Workflow B metadata carries them. A JUnit `<error>` is a test error.
- **Determinism:** the same observations always give the same result.

**Events (ADR-012):** TI publishes `ti.upload.received`, `ti.upload.processed`, `ti.upload.duplicate` and `ti.upload.rejected` (with a reason), all with actor `ci_token`.
- **Payloads:** the content SHA-256 and size, or the run ID and counts. Test names, CI-supplied strings and report content are never included; the upload row holds the key fields.
- **Outbox:** `ti.outbox`, written in the same transaction as the change.

## Consequences

- **Positive:**
  - Push ingestion works end to end with idempotency, hardened parsing, tenant-scoped storage and fail-closed audit.
  - The classifier is ready for Workflow C.
- **Negative:**
  - Large reports are parsed inside the request.
  - Orphan artifacts can accumulate until retention exists.
  - Classifications aren't stored yet.
