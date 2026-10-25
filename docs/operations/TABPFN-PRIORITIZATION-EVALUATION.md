# TabPFN prioritization evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 5
and sponsor goal 12 (TabPFN). The deterministic baseline
(`test_intelligence/application/prioritization.py`) stays what the product
ships. This page records the evaluation harness, the hosted-API facts, and
the honest result: **no evaluation has run on real data**. The hosted
predictor was live-verified on synthetic rows only (2026-10-25); the
evaluation is BLOCKED on stored multi-window history, which no environment
here has.
Classification authority stays with `ti-classification/1`; prioritization
only orders tests for attention.

## The harness (`test_intelligence/application/evaluation.py`)

- **Target `fails_within_horizon`:** does a test identity have a failing or
  errored attempt in a CI run received in `(cutoff, cutoff + horizon]`?
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

## Result

| Model | Status | precision@k | ROC-AUC | Brier |
|---|---|---|---|---|
| `ti-prioritization-baseline` | **NOT RUN on real data** — no stored history reachable here | — | — | n/a (rank) |
| `base_rate` | **NOT RUN on real data** | n/a | n/a | — |
| `tabpfn` | **BLOCKED** — no real dataset (the API answers; see above) | — | — | — |

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
test covers the blocked path only. No dependency is needed (the SDK
`tabpfn-client` 0.6.1, Apache-2.0, is not required).
