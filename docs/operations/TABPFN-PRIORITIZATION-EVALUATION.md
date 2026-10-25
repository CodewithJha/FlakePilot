# TabPFN prioritization evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 5
and sponsor goal 12 (TabPFN). The deterministic baseline
(`test_intelligence/application/prioritization.py`) stays what the product
ships. This page records the evaluation harness, the hosted-API facts, and
the honest result: **no evaluation has run on real data**. The hosted
predictor was live-verified on synthetic rows only (2026-10-25); the
evaluation is BLOCKED on stored multi-window history. The
`backfill-ci-history` importer (below) brings that history in with its
original CI time, and since commit `4c1bf81` FlakePilot's own CI uploads its
pytest JUnit report as the `junit-gates` artifact; the first such run was
imported live on 2026-10-25. History before that commit cannot be recovered.
Classification authority stays with `ti-classification/1`; prioritization
only orders tests for attention.

## The harness (`test_intelligence/application/evaluation.py`)

- **Target `fails_within_horizon`:** does a test identity have a failing or
  errored attempt in a CI run whose CI time is in `(cutoff, cutoff + horizon]`?
  CI time is the code host's original completion time for a backfilled run,
  receipt time for every other run (`TiStore.repo_history`).
- **Features (`ti-eval-features/1`)**, from runs at or before the cutoff only:
  runs, attempts, failed attempts, failed runs, flaky runs, environment
  count, hours since last failure (missing if never failed), hours since last
  run. Counts only — no names, IDs, or text.
- **Temporal split:** train examples cut at `train_cutoff`, labelled from the
  next horizon; test examples cut at `train_cutoff + horizon`, labelled from
  the horizon after. Training never depends on a row after the test cutoff
  (asserted by a test that truncates history and compares).
- **Observed labels only:** an identity with no run in the label window is
  not an example (no invented "passed").
- **Baseline:** the negated rank from the shipped `prioritize()` over the
  same cutoff-bounded history — the comparison is against the real product.
- **Metrics (stdlib):** precision@k, ROC-AUC (Mann-Whitney with tie-averaged
  ranks; undefined with one class), Brier score for probability outputs. The
  baseline is a rank, so it has no Brier score; a constant training-prevalence
  predictor gives the Brier floor a candidate must beat.
- **Dataset card:** target, feature schema, cutoffs, horizon, row and
  positive counts, k, and a SHA-256 fingerprint of the exact examples; the
  candidate is named `tabpfn:<model_path>@<package_version>` as reported by
  the API.
- **Honest outcomes:** no predictor → `blocked/not_configured`; one class in
  training → `blocked/single_class_train` (no call made); provider failure →
  `blocked/<reason code>`; malformed probabilities → `failed/bad_prediction`.
  No number is ever filled in.

Known biases to state with any future result: the population is identities
with runs in both windows; `repo_history` reads the most recently active
`max_tests` identities; small repositories give few positives, so one window
is noise — compare across several consecutive cutoffs.

## Hosted TabPFN facts (verified from vendor docs, 2026-10-24)

- **API:** documented plain JSON over HTTPS at `https://api.priorlabs.ai`
  ([REST quickstart](https://docs.priorlabs.ai/api/rest-quickstart),
  [predict reference](https://docs.priorlabs.ai/api-reference/prediction/predict-tabpfn-json-api)):
  `POST /tabpfn/prepare_train_set_upload` → PUT CSV/Parquet to signed URLs →
  `POST /tabpfn/fit` → `POST /tabpfn/prepare_test_set_upload` → PUT →
  `POST /tabpfn/predict` (`output_type: probas`). Bearer token. Legacy `/v1/*`
  multipart routes are being retired. Model `v3.5_default` (TabPFN-3.5-Plus).
- **Limits and cost** ([metering](https://docs.priorlabs.ai/api/metering),
  [rate limits](https://docs.priorlabs.ai/api/rate-limits),
  [pricing](https://priorlabs.ai/pricing)): a free API tier; default budgets
  of 5,000,000 tokens/day and 20,000,000 tokens/month; uploads and standard
  fits are free, predictions are metered with a 10,000-token minimum per
  operation; 60 predictions/minute. Whether one evaluation fits the free
  budget is NOT VERIFIED until `POST /tabpfn/estimate_cost` is called with a
  real key and the real row counts.
- **Terms — owner must act:** the General Terms §7.4
  ([terms](https://priorlabs.ai/general-terms-and-conditions)) grant Prior
  Labs a licence to use uploaded content for model improvement **unless the
  customer objects** (account toggle or written notice), while the
  [security page](https://docs.priorlabs.ai/api/security) says foundation
  models are never trained on customer data. Treat the terms as binding: opt
  out before the first upload (the owner reports having opted out before the
  2026-10-25 live check). The Acceptable Use Policy forbids personal
  data; the harness sends only numeric counts and 0/1 labels.

## The adapter (`test_intelligence/infrastructure/tabpfn.py`)

`TabPfnPredictor` implements the harness's `Predictor` port over the
documented flow. Fail-safe: every failure is an unavailable prediction with a
reason code; the bearer token goes only to the API origin, never to signed
storage URLs; only numeric CSV leaves the process; responses are size-capped;
`random_state` is fixed at 0. The HTTPS transport is **injected** — outbound
HTTP may live only in integration adapters (ADR-003, enforced by
`tests/architecture/test_authentication.py`), so the module imports no HTTP
client; the composition root supplies `hosted_http.call` (https only, no
redirects, size-capped).
Tested with a fake transport (**tested with mocks**) and **live-verified**
2026-10-25 from a developer machine (`tests/live/test_live_tabpfn.py`, key
in `FLAKEPILOT_LIVE_TABPFN_API_KEY`): one fit on 20 synthetic rows and one
predict on 5 returned a probability in [0, 1] per row, and an invalid key
gave HTTP 401 → `provider_unauthorized`. That proves the plumbing, not
prioritization quality.

## Real history: the CI backfill

Ingestion stamps a run with its receipt time, so a fresh deployment has one
window of history at most. `backfill-ci-history` (operator only) imports the
JUnit XML a repository's GitHub Actions runs uploaded as artifacts, keeping
the original time:

- **Provenance.** The upload has `ci_source` and `provenance`
  `github_actions_backfill`, no CI token, the original run ID and attempt,
  and `origin_run_url`; the run has `ci_completed_at` (the API's `updated_at`
  on the completed run). `received_at` stays the import time. Migration
  `ti_0002`; schema checks keep a CI upload tokened and a backfill tokenless.
- **Time.** `repo_history` and `failure_trend` read CI time
  (`coalesce(ci_completed_at, received_at)`), so the evaluation's windows,
  history cutoffs, and day trends use the original time. Classification
  evidence (`observations`) is unchanged and keeps receipt order.
- **Scope.** The repository is the (org, repo) pair's registered binding,
  never a flag. The GitHub App token is actions:read on that one repository.
  At most 50 runs, 10 artifacts per run, and 100 artifacts per command.
- **Archive safety** (`integrations/infrastructure/github_actions.py`). The
  download follows exactly one redirect, only to an https GitHub storage
  host, and sends no bearer there. Zips are read in memory only: at most
  25 MiB compressed, 1000 entries, 10 MiB per XML and 50 MiB in total.
  Encrypted entries and codecs other than stored or deflate are refused, and
  any name that could traverse a path refuses the whole archive. JUnit
  parsing stays with `JUnitParser`, with its limits; XML that is not JUnit is
  ignored.
- **Idempotency and audit.** The key is the upload key: (org, repo,
  `github_actions_backfill`, run ID, attempt, artifact name). The same
  archive again is a duplicate and writes nothing. Another archive under the
  same key is refused (`idempotency_conflict`). Every outcome is one
  `ti.backfill.imported`, `.duplicate` or `.rejected` event, with actor
  `operator_cli`.

Tests: unit (zip safety, redirect host, bearer never sent to storage,
idempotency, provenance, refusals), real-PG integration
(`tests/integration/test_backfill_postgres.py`: six runs a day apart; re-import
no-op; tenant-exact binding; history cutoff, day trend and the evaluation's
split on CI time; schema refuses an untokened CI upload).

Live, 2026-10-25 (`tests/live/test_live_backfill.py`, the App installed only
on `CodewithJha/FlakePilot` with actions, contents and metadata read):
actions:read minting, the run listing (50 completed runs, 2026-10-21 to
2026-10-25), and 50 artifact listings succeeded; **0 artifacts** exist, because
FlakePilot's CI does not upload JUnit XML. Nothing was imported, and TabPFN was
not called. The live run first exposed two minter defects, both fixed:
GitHub answers token creation with 201, not 200, and installation tokens
are now about 390 characters (the shape check allowed 255). The download
and zip path has not run against real artifacts.

Live, 2026-10-25, after FlakePilot's CI began uploading `junit-gates`
(commit `4c1bf81`, CI run 40170825392): `backfill-ci-history --artifact-pattern
'junit-*'` through the operator CLI against a throwaway PostgreSQL 16 database
and the registered `CodewithJha/FlakePilot` pair read 10 completed runs and
imported **1** artifact: 1397 tests, `ci_completed_at` 2026-10-25T06:57:52Z
(the run's own completion time; `received_at` is the import a minute later),
provenance `github_actions_backfill`, tokenless upload, origin URL kept, one
`ti.backfill.imported` event. The download, redirect and zip path ran against
a real artifact. `tests/live/test_live_backfill.py` then passed: 1 imported,
and the re-import was 1 duplicate.

The evaluation needs at least three windows of at least one day each, so it
becomes possible after about three days of CI runs on `main` (artifacts
expire after 90 days by default). Until then it is not run, and nothing is
inferred from one run.

## Result

| Model | Status | precision@k | ROC-AUC | Brier |
|---|---|---|---|---|
| `ti-prioritization-baseline` | **NOT RUN on real data**: one imported run (one day) so far; three windows need about three days of CI | — | — | n/a (rank) |
| `base_rate` | **NOT RUN on real data** | n/a | n/a | — |
| `tabpfn` | **BLOCKED**: no real dataset (the API answers; see above) | — | — | — |

Unit tests compute these metrics on hand-built fixtures only to verify the
arithmetic; those values are not evaluation results.

## Selection rule (proposed for owner approval)

TabPFN may be considered for an *advisory* ordering only if, on real stored
history, with recorded dataset cards: it beats the baseline on precision@k
and ROC-AUC and beats the base-rate Brier score, on each of several
consecutive non-overlapping test windows — not on one lucky window. Even then
it never replaces `ti-classification/1` verdicts and never gates anything.

## Running it for real

`evaluate-prioritization --org-id … --repo-id … --train-cutoff <ISO> --horizon-days N [--k K] [--with-tabpfn]`
builds `Evaluation(uow)` and prints the `EvaluationReport` as JSON. The TabPFN
candidate is constructed only from `FLAKEPILOT_TABPFN_API_KEY` with the
`hosted_http.call` transport, and contacted only with `--with-tabpfn`;
otherwise it is reported `blocked: not_configured`. The command's PostgreSQL
tests cover the blocked candidate path, including over backfilled history. No dependency is needed (the SDK
`tabpfn-client` 0.6.1, Apache-2.0, is not required).
