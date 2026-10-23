# Production-readiness roadmap

Stage 10J. Dependency-ordered plan for the remaining work, derived from the
assessment in [PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md).
Nothing here is provisioned, enabled, or approved. Decisions explicitly
reserved for the repository owner are marked **OWNER DECISION**. No cloud
provider, budget, runtime, vendor, or retention duration is assumed — each
appears as a decision to be made, recorded in a dated entry under
`docs/operations/isolation-validation/` (or an equivalent dated record), and
is only actionable after it is made.

Local work means work possible in this repository or the Mac with the tools
and credentials that exist. External work requires infrastructure, vendor
accounts, credentials, or human approvals that do not exist yet.

## Dependency graph

```
B1 dedicated host ──> B2 isolation runtime ──> B3 host plumbing & limits
                                   │                     │
                                   └──> C1 trust boundary maps ──> B4 evidence & review
B5  key manager (independent of B1)                 B4 --> B9 security review & authorization
B6  retention decision (independent)                B5 --> production encryption wiring
B7  GitHub App credentials (independent)            B6 --> deletion implementation
B8  operations & monitoring (needs B1 for reality)
```

## B1 — Dedicated Linux execution host: select and approve

**Why first.** Every host-side control (egress, mounts, limits, cleanup,
reboot) is BLOCKED until a host exists; all dependent items date from it.

- Prerequisites: this assessment accepted; a budget and an account identity.
- OWNER DECISIONS: provider and tenancy (any), host size, region, purchase
  model, who owns the account, spend ceiling. Nothing in the repository or
  this plan recommends one.
- Local deliverables: a decision record (provider, size, owner, ceiling,
  date) in the validation directory; a one-page provisioning checklist from
  `EXECUTION-HOST-RUNBOOK.md` §4–§5.
- External deliverables: the host, created and recorded.
- Acceptance criteria: the decision record exists; the host boots with the
  chosen OS and kernel; the record ties host identity to a future evidence
  fingerprint.
- Required evidence: date, operator name, access method, OS/kernel output.
- Risks: nothing yet — no cost without approval.
- Who can start: only the owner (external); the decision record is local
  work the owner must direct.

## B2 — Isolation runtime: select the per-run boundary

**Why second.** The mechanism cannot be measured until the host runs it; the
evidence target (`per_run_microvm`) depends on this choice.

- Prerequisites: B1 (or a concurrent decision, since the runtime may shape
  the host image).
- OWNER DECISIONS: the runtime and its governance (Firecracker, Cloud
  Hypervisor, QEMU, or another boundary reviewed independently). The
  repository requires a separate kernel per run, outside-the-guest limit
  enforcement, and no container-runtime socket in the guest. An
  independently reviewed container boundary could also qualify if a
  security review says so; that is an approval, not an invention.
- Local deliverables: a comparison note of the candidates against the
  runbook's requirements; an interface sketch of how the host adapter would
  expose the existing protocol-v1 operations and the reaper inventory.
- External deliverables: runtime installed on the host; a first-boot smoke
  that starts and destroys an empty guest.
- Acceptance criteria: a separate-kernel guest boots, is destroyable by the
  host, and receives no runtime socket; a dated record aligns the decision
  with `MECHANISM_LEVEL` in `ops/isolation/evidence_report.py` (a new
  mechanism entry may be added when a runtime is actually used, never
  before).
- Required evidence: first-boot log with the kernel identity of the guest.
- Risks: choosing a runtime that cannot enforce all six limits outside the
  guest blocks B3; recording a mechanism before measurement would fake a
  level (forbidden).
- Who: owner decides; later implementation stages are engineer work.

## B3 — Host plumbing, resource limits, worker integration

**Why third.** Limits, trusted-host worker integration, and cleanup are the
first code-bearing dependent stage, and can only be written after B1–B2.

- Prerequisites: B1, B2.
- OWNER DECISIONS: none initially; the design must be approved before
  wiring (see B9).
- Local deliverables: host-adapter implementation to represent — not to
  enable — the runtime: ledger-key placement on the host, `HostGate`
  integration, reaper against real inventory, limit enforcement outside the
  guest, tests against a container backend only where calibrated like the
  existing hosted-runner probe.
- External deliverables: the measured proofs of all six limits on the real
  runtime (CPU, memory, pid, disk, output, wall) and the cleanup proofs
  (orphan after launcher death, reboot recovery, double-sweep emptiness).
- Acceptance criteria: every limit observed at its cap from inside the
  guest and enforced from outside; cleanup recovers at least from launcher
  kill, timeout, cancel, and host restart; composition root still refuses
  production wiring until review (posture must not change during B3).
- Required evidence: validation entries per proof, with digests.
- Risks: claimed cleanup without reboot tests — this is the first place
  production could silently weaken; review at B4 only after evidence.
- Who: engineer implements; an operator runs the external proofs.

## C1 — Trust-boundary verification (host↔guest, control↔host)

**Concurrent with B2–B3** because it defines what B4 must measure.

- Prerequisites: B1.
- OWNER DECISIONS: none beyond the standing ones; secrets-appropriateness
  is already decided in ADR-004/008.
- Local deliverables: a verification script or logged procedure showing the
  guest receives no database URL, session secret, GitHub key, host ledger
  key, or artifact keys (credential census is evidence-at-the-moment only —
  the runbook requires this be documented as necessary-not-sufficient);
  the control plane holds no host key.
- Acceptance criteria: check `credentials_absent` and `host_mounts_denied`
  PASS from guest observation with the control-plane side verified by
  audit/secret-store review.
- Required evidence: census output summary (counts, no names), mount flags.
- Risks: a launcher leak would fail here — failing is the correct outcome.
- Who: engineer + operator.

## B4 — Independently reviewable isolation evidence and review

**Why fourth.** Evidence and review require the controls from B1–C1; review
is the gate to anything production-shaped.

- Prerequisites: B1, B2, B3, C1.
- OWNER DECISIONS: the reviewer (a named person), acceptable re-collection
  cadence.
- Deliverables (local): collected reports via `ops/isolation/collect_report.py`
  for the runtime mechanism, hashes recorded with raw log excerpts, dated
  review entries naming the reviewer and the per-check judgments, and the
  reviewer's decision at the `production_approved` level (grant/declined).
- External deliverables: the measurements themselves on the host.
- Acceptance criteria: all seven `IsolationChecks` fields supported by
  unsimulated guest observations at `per_run_microvm` (or a reviewed
  equivalent); the domain stays untouched — attestation happens through a
  reviewed record, wiring remains forbidden until B9 grants it.
- Required evidence: report JSON + sha256 + identity fingerprint + review
  entry.
- Risks: review-by-convenience; the hash proves bytes, not truth — the
  reviewer must compare against raw logs.
- Who: engineer collects; named reviewer decides; owner ratifies.

## B5 — External key manager, then production artifact encryption

**Independent of B1–B4.** Can start whenever approved; must complete before
any production artifact path is called "encrypted".

- Prerequisites: this assessment; OWNER DECISIONS: vendor/approach (nothing
  pre-selected), key custody rules, rotation cadence.
- Local deliverables: key-provider interface is already defined
  (`ArtifactKeyProvider`); implement a concrete provider against the chosen
  manager, application wiring that stores ciphertext and retrieves it
  through the authorized read path, tests with a fake provider already
  present; use production with the real vendor only after the round-trip
  test.
- External deliverables: a real key manager account or equivalent.
- Acceptance criteria: deployed storage writes ciphertext and returns it
  through `read_execution_artifact`; unavailability fails closed
  (`encryption_unavailable`); no fallback to plaintext.
- Required evidence: round-trip logs with key ids, tenant access checks.
- Risks: wrong-owner keys or guest-side keys are worse than plaintext; the
  provider must live outside guest and worker.
- Who: vendor owner decision; wiring engineer work; access decision record.

## B6 — Retention: approval, then implementation

**Independent of B1–B4.**

- Prerequisites: none technical.
- OWNER DECISIONS (must come first): the duration or trigger for artifact
  deletion; the categories (raw reports, sealed artifacts) and tenants; the
  rule that audit and outbox rows are never artifact keys (already
  structural in `integrations/application/retention.py`).
- Local deliverables: an approval record; only then a tenant-scoped,
  idempotent, restart-safe, auditable deletion implementation and its
  tests; retention policy configuration behind explicit approval.
- Acceptance criteria: deletion selects nothing without the approved policy
  (current behavior preserved); with a policy, deletion is retryable and
  leaves audit and outbox intact; a partial pass recovers on restart.
- Required evidence: tests plus a dry-run report on real (non-production)
  data.
- Risks: deleting audit evidence is irreversible — the approval record
  before any code is the control.
- Who: owner/legal/security decide; engineer implements after.

## B7 — GitHub App provisioning and live least-privilege proof

**Independent.**

- Prerequisites: none technical. OWNER DECISIONS: create the app (or use an
  existing identity), which account owns it.
- Local deliverables once credentials exist: live minting of
  `contents:read` for one repository; expiry and refresh behavior; proof
  that no write scope appears; secret handling through the secret store;
  token never logged or placed in guest paths.
- External deliverables: the App ID and private key in the appropriate
  store; the GitHub-side grant.
- Acceptance criteria: a dated record shows a live minted token used only
  for a read of the authorized repository, with expiry honored.
- Required evidence: minted token metadata (never the token itself),
  request/response log.
- Risks: private-key leakage — key handling review before use.
- Who: owner provisions; engineer writes the live test.

## B8 — Monitoring, incident response, ownership, rollback

**Depends on B1** for anything real, on nothing for design.

- OWNER DECISIONS: the sink (metrics/logs/alerts), the on-call operator,
  the patch cadence, rollback expectations.
- Local deliverables (design now): alert list from the runbook (authority
  unavailable, runtime unavailable, unreadable ledger, repeated rejected
  macs, cleanup unable to run) as an implementable job description.
- External deliverables (after B1): an actual monitoring stack, dashboards,
  drills, a recorded rollback exercise.
- Acceptance criteria: alerts fire on a fault injection drill; recovery is
  executed, not written about.
- Required evidence: drill logs.
- Risks: without this, the platform's first production-class fault is
  handled ad hoc.
- Who: owner decides; engineer installs; operator drills.

## B9 — Independent security review and production authorization

**Last.** Explicit, named, outside any automated component.

- Prerequisites: B1–B4 (execution evidence), B5, B6, B7, B8 as applicable
  to the requested production surface.
- OWNER DECISIONS: who reviews; what production scope gets authorized.
- Deliverables: a review report covering the trust boundaries, evidence,
  secrets, network, and the drill records; the authorization decision
  itself, recorded.
- Acceptance criteria: an unambiguous written authorization (or refusal)
  tied to a repository SHA; execution wiring may change only afterwards.
- Required evidence: the full chain above.
- Risks: a review conducted by the implementer is not an independent
  review.
- Who: the owner chooses the reviewer; the review belongs outside this
  repository's toolchain.

## Proposed sequence

The assessment found no requirement inventing a fixed "Stage 10K" onward:

| Step | Content | Entry criteria | Exit criteria |
|---|---|---|---|
| Pre-work | OWNER decisions for B1 and B5 vendor; optional B2 runtime candidate review (all local) | this document accepted | dated decision records |
| Host + runtime | B1, B2 external provisions; B3 local adapter design begins in parallel | approved records | host exists, runtime boots, first-boot record |
| Controls + evidence | B3 proofs, C1, B4 collection and review | host + runtime | all seven checks at `per_run_microvm`; reviewer sign-off |
| Independent workstreams | B6 retention approval (or release of it), B7 live GitHub proof, B8 operations design | any time | respective acceptance criteria |
| Production authorization | B9 with full chain, then any wiring | everything above | written authorization or refusal |
| Production wiring | only after B9 (worker integration, sealer use, retention job) | B9 authorization | production proof continues |

If the owner would rather hold everything until the dedicated-host decision,
then B5, B6, B7, and B8 design are the only remaining useful local stages,
and this document plus the assessment is the last required planning stage
for now. That is the explicit statement requested.
