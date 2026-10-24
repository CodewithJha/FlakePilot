# Tinker evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 5
and sponsor goal 13 (Tinker). Read-only evaluation: no account, no key, no
adapter. Verdict authority stays with the deterministic `ti-classification/1`
rule set.

## What Tinker is (vendor docs, read 2026-10-24)

Thinking Machines' Tinker is a managed fine-tuning API: LoRA training (not
full fine-tuning) and sampling on open-weight models, with downloadable
weights ([docs](https://tinker-docs.thinkingmachines.ai/),
[models and pricing](https://tinker-docs.thinkingmachines.ai/tinker/models/models_and_pricing/),
[pricing](https://thinkingmachines.ai/tinker/pricing/)).

- **Access:** self-serve sign-up via <https://thinkingmachines.ai/tinker/>;
  API key from the Tinker Console (`TINKER_API_KEY`); training and sampling
  are rejected until the organization has a prepaid balance or an enterprise
  agreement. Serverless inference is beta (Inkling models only).
- **Interface:** the Python SDK `tinker` (0.35.0 on PyPI, Apache-2.0). No
  plain-HTTP contract was found documented for training, so any adapter would
  need that dependency (ADR-017 decision 10 gate).
- **Models (sample):** Qwen3.5-4B, Qwen3-8B, Qwen3.5-9B, GPT-OSS-20B/120B,
  Nemotron-3.5-Lightning-30B-A3B, larger MoE models up to ~1T parameters.
- **Price (USD per 1M tokens, as of 2026-10-19):** e.g. Qwen3-8B train $0.44 /
  sample $0.60; Qwen3.5-4B train $0.737 / sample $1.005; GPT-OSS-20B train
  $0.396 / sample $0.45; checkpoint storage $0.10/GB-month. Cost scales with
  tokens × epochs; no estimate is possible without a dataset.

## The only objective that would make sense

**Failure-reason assistance:** given a failing test's failure text, suggest a
root-cause category (e.g. timeout, ordering dependence, network, assertion
drift) as *advisory* evidence beside — never instead of — the
`ti-classification/1` verdict.

Measurable form: on a held-out, human-labelled split, a tuned model must beat
(a) the same base model zero-shot through the existing `Assist` policy path
and (b) a trivial majority-class baseline, on macro-F1 and per-class
precision, with the dataset and model checkpoint recorded. Reproducing
`ti-classification/1` itself is not an objective: that rule set is exact over
attempt outcomes, so a model could only match or degrade it.

## Why it is not justified now

1. **No input data in the database.** `ti.test_results` stores outcomes
   only. Failure text exists solely inside the raw report artifacts (plaintext
   `LocalFsArtifactStore`); nothing extracts, labels, or indexes it.
2. **No labelled dataset.** No human-reviewed root-cause labels exist, and
   none may be derived from customer reports without consent.
3. **No authorization for the data flow.** Fine-tuning would send repository
   failure text (which can contain paths, hostnames, and secrets) to a third
   party. That needs owner approval, a field-level data-flow review, and a
   redaction step — none exist.
4. **Access is paid and owner-gated.** A prepaid balance and a key are owner
   actions; this phase has no spending.
5. **The advisory path already exists.** `Assist` + an operator-run endpoint
   (goal 1) can answer zero-shot today; fine-tuning only matters once (a) in
   the objective above is measured and found wanting.

## Revisit triggers (all required)

1. A stored, consented, redacted failure-text field with an owner-approved
   retention decision.
2. A human-labelled root-cause set large enough to hold out a test split,
   with per-class counts recorded.
3. A recorded zero-shot result through `Assist` that a tuned model must beat.
4. Owner approval of the `tinker` dependency, the data flow, and a budget.

If all hold, the shape that keeps the trust model intact is: train on Tinker,
download the LoRA weights, and serve them from the operator's own endpoint
through the existing local-endpoint adapter — the product would never call
Tinker at runtime.

Status: **not justified this phase**; no adapter exists by design.
