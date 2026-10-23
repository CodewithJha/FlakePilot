# Infrastructure implementation plan

Stage 10K. Translates the decisions in
[INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md) and the analysis in
[INFRASTRUCTURE-OPTIONS-ANALYSIS](INFRASTRUCTURE-OPTIONS-ANALYSIS.md) into
dependency-ordered work packages. **Work packages are not approved stages.**
No package here has started; the current repository posture — production
untrusted execution disabled, plaintext artifacts at rest, no retention, no
live GitHub mint, no monitoring deployed — is exactly the one verified in
Stage 10J's assessment and nothing in this plan changes it.

Sources of truth this plan depends on: `AGENTS.md` (workflow invariants),
[EXECUTION-HOST](EXECUTION-HOST.md) (the standing host contract),
[EXECUTION-HOST-RUNBOOK](EXECUTION-HOST-RUNBOOK.md) (measurement and
go/no-go), ADR-004/008 (planes/trust model), and the existing
`implemented` code — the ledger, the evidence collector, the sealing
envelope, the durable-jobs worker. Anything else is not yet built.

Every work package states whether it is local (possible now inside this
repository with nothing new to buy) or external (requires the owner's
infrastructure decision first). A package marked LOCAL-READY can start
whenever the owner directs a stage for it; nothing has been started during
Stage 10K.

```
WP-1 decision records (local) ────┬──> WP-2 provisioning (external)
                                  │          │
                                  └──> WP-5 KMS integration (local→external)
                                       WP-6 retention implementation (local→prod)
                                       WP-7 live GitHub proof (local→external)
                                       WP-8 monitoring & drills (local→external)
                                             │
                    WP-3 host+runtime integration (local code after WP-2)
                                             │
                    WP-4 isolation measurement & evidence (external)
                                             │
                    WP-9 independent security review ──> WP-10 go/no-go
                                                        authorization
```

---

## WP-1 · Owner decision record

- **Prerequisites** — this stage's documents accepted.
- **Local or external** — local (writing decisions; the decisions themselves
  are the owner's).
- **Scope** — record D-HOST-1..7, D-RUNTIME-1..5, D-KMS-1..3, D-RET-1..4,
  D-GH-1..4, D-OPS-1..6 from [INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md)
  as confirmations/amendments in dated records under
  `docs/operations/` (one page per block or one consolidated record).
- **Exclusions** — no infrastructure acquisition, no credentials, no
  monitoring setup, no code.
- **Deliverables** — a decision record with each decision id, choice,
  approver, date, assumption list, and assumptions that had to be
  confirm-or-reject (esp. D-HOST-3 residency, D-HOST-4 usage portrait,
  D-RET-2 duration, D-GH-3 key storage).
- **Acceptance criteria** — every decision id has a written answer or an
  explicit "deferred to WP-x" note with the owner's name on the deferral.
- **Tests and evidence** — consistency check against
  [INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md) table (no
  contradicted id); release-review documentation gate.
- **Security reviewer** — not required.
- **Owner decisions** — that is the package's whole content.
- **Rollback / cancellation** — any decision may be replaced by a later
  recorded decision; nothing irreversible exists yet.
- **Blocks released** — WP-2..WP-8's start lines.

## WP-2 · Infrastructure provisioning (after owner approval)

- **Prerequisites** — WP-1 (at minimum D-HOST-1/2/3/4/5, D-RUNTIME-1).
- **Local or external** — external (purchases/registrations must happen;
  this package requires money and an account identity, only the owner
  can approve).
- **Scope** — acquire and record exactly one execution host per
  D-HOST-1's choice; create the monitoring instance per D-OPS-1's choice
  (separate from the execution host); create the KMS-facing account per
  D-KMS-1 if a cloud manager is chosen; register one test GitHub App per
  D-GH-1 if that path is approved at the same time.
- **Exclusions** — no application code deployment; no untrusted-code
  execution; no production data; no GitHub App yet unless D-GH-1 chose to
  create one now.
- **Deliverables** — for each acquired asset: a dated record (what, why,
  cost line, owner, how to retire), the first-boot facts runbook §4 asks
  for (OS, kernel, runtime name, host identity for the future
  fingerprint), the monitoring instance's URL and credentials storage path,
  the KMS keys' ids if a cloud manager, the GitHub App id/policy if created.
- **Acceptance criteria** — each asset boots and is reachable per its
  intended access path (runbook §5 for the host); no asset holds control
  plane secrets beyond its scope; every credential generated here has a
  named storage location and no plaintext copy left in a shell history or
  transcript.
- **Tests and evidence** — manual verification records; no untrusted code
  runs on any asset.
- **Security reviewer** — skip (still design phase); noted for WP-9.
- **Owner decisions** — already covered by WP-1; only genuine surprises
  (like a residency constraint surfacing) re-escalate.
- **Rollback / cancellation** — every asset acquired here can be destroyed
  and recorded as retired; nothing is load-bearing until WP-3 completes.
- **Unblocks** — WP-3, WP-4, WP-8 external phases.

## WP-3 · Host and runtime integration (local code, external validation)

- **Prerequisites** — WP-2 (a host and a chosen runtime actually exist).
- **Local or external** — local implementation (repository code), needing
  the external host only to *verify*.
- **Scope** — the runtime adapter (a `mechanism` name in
  `ops/isolation/evidence_report.py` once the runtime is actually used —
  never before, per the verified rule), a host-side launcher that respects
  the runbook contract (per-run scratch only, no guest secrets, limits
  enforced outside the guest), a vsock agent channel (no TCP rendezvous for
  agent traffic), and the ledger/worker integration needed to speak the
  existing protocol-v1 operations to this runtime. The control plane's
  composition root continues to return `ClosedSandbox` / the disabled
  attestation while WP-9 has not authorized anything: untrusted execution
  stays disabled in the *product*, and this work is *host-side capability*,
  not a production enable.
- **Exclusions** — no change to `IsolationChecks`, no sealer wiring, no
  retention changes, no GitHub wiring.
- **Deliverables** — a runtime mechanism documented, tests for the
  launcher/adapter logic, a smoke path that starts an empty guest and
  destroys it (runbook B3 acceptance criterion).
- **Acceptance criteria** — a guest on the chosen host boots, is
  destroyable, and answers only over the agent channel; the runtime never
  exposes its socket into the guest; the composition root's failure-closed
  behavior is untouched (verified by the existing test suite staying green).
- **Tests and evidence** — the full pytest suite; a smoke transcript in
  `docs/operations/isolation-validation/`.
- **Security reviewer** — skip; noted for WP-9.
- **Owner decisions** — any unplanned-for deviation (e.g., the runtime
  needs a non-default kernel).
- **Rollback / cancellation** — the host-side code is additive; disabling
  is a no-op since production was never enabled.

## WP-4 · Isolation measurement and evidence collection

- **Prerequisites** — WP-2 and WP-3 (a host, a runtime, a smoke path).
- **Local or external** — external measurement on the real host, local
  tooling (`ops/isolation/collect_report.py` — verified to exist and to
  write schema-version-1 reports).
- **Scope** — run every probe section from
  [EXECUTION-HOST-RUNBOOK](EXECUTION-HOST-RUNBOOK.md) §6–§12 on the real
  runtime, collect reports via the CLI, record hashes, and assemble the
  bundle a named reviewer will need. Per the roadmap's B3 acceptance: every
  limit observed at its cap from inside the guest and enforced from
  outside; cleanup recovers from launcher kill, timeout, cancel, and host
  reboot; every check supported by a guest, unsimulated observation.
- **Exclusions** — no `IsolationChecks` promotion path — a report is
  evidence, not authorization; the composition root still refuses.
- **Deliverables** — dated validation entries, one per proof (disk, CPU,
  memory, pid, output, wall, cleanup variants, network/metadata, mounts/
  credentials, unprivileged); the report JSON files and their sha256s; a
  single consolidated evidence bundle.
- **Acceptance criteria** — report summary shows all seven
  `IsolationChecks` fields PASS on the runtime's mechanism at the
  `per_run_microvm` level, without any simulated entry; identity
  fingerprints recorded and handed to the reviewer (WP-9's input).
- **Tests and evidence** — collected reports + raw probe logs; the
  reviewer's checklist (runbook §12 verification bullet list).
- **Security reviewer** — pass to the reviewer named in D-RUNTIME-5/WP-1.
- **Owner decisions** — reviewer ratification (already the WP-1 item).
- **Rollback / cancellation** — evidence is inert; nothing to roll back.
  A failed control is a *find*, documented, not a rollback: the runbook's
  refusal path is the correct outcome.
- **Unblocks** — WP-9.

## WP-5 · External key management integration

- **Prerequisites** — WP-1 (D-KMS-1 vendor choice) and the account from
  WP-2 if a cloud KMS was chosen. Can begin *code-side* (local) once the
  decision exists even before WP-2 buys anything, using the vendor's API
  contract without provisioning keys; the round-trip against real keys is
  external.
- **Local or external** — local implementation; external validation.
- **Scope** — a concrete `ArtifactKeyProvider` implementation for the
  chosen manager; application wiring that seals on write and opens on the
  authorized read path (`read_execution_artifact`); unit tests like the
  existing `AesGcmSealer` tamper and rotation tests; a documented
  offline drill ("fail closed, never plaintext" — verified behavior
  of `UnavailableKeyProvider`/`sealed_store.py`).
- **Exclusions** — no fake or local-only provider counts for the
  production claim; no retention change; no guest-side key exposure
  (D-KMS-3 stays "never").
- **Deliverables** — the provider module, wiring, tests, a runbook entry
  for the round-trip record and the drill, and one dated round-trip
  transcript against the real manager (seal → store → read-back).
- **Acceptance criteria** — a deployed configuration writes ciphertext and
  reads it back through the authorized path; KMS unavailability produces
  `encryption_unavailable` and never plaintext; logs show no key material.
- **Rollback / cancellation** — the plaintext store remains the default
  until production-sealing is explicitly authorized; rollback is "do not
  wire the sealer", not "delete data".
- **Unblocks** — removes the encryption-at-rest blocker from the
  assessment's list (B5).

## WP-6 · Retention implementation (after formal approval)

- **Prerequisites** — WP-1 (D-RET-1..4: the **approval record from
  product/legal/security signed by the owner** is the prerequisite — the
  approval is not an engineering artifact).
- **Local or external** — local implementation; no production data at
  risk until a production deployment exists.
- **Scope** — tenant-scoped, idempotent, restart-safe deletion of *artifact
  keys only* per [INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md)
  D-RET-3 (dry-run first, no SQL against audit/outbox through this path,
  deletion events audit-logged). Category and duration come from the
  owner's approvals; the 90-day suggestion in D-RET-2 is a proposal only.
- **Exclusions** — no retention enabled by this plan; audit/outbox rows
  are never deleted by retention (verified structural separation in
  `integrations/application/retention.py`).
- **Deliverables** — retention policy plumbing behind an explicit approved
  policy; dry-run report format; tests covering restart-safety and
  idempotence.
- **Acceptance criteria** — with no approved policy, `select_for_deletion`
  keeps selecting nothing (current verified behavior); with an approved
  one, dry-run and execute are distinguishable; audit rows and outbox rows
  untouched.
- **Rollback / cancellation** — disable the policy; artifacts already
  deleted cannot be recovered (deletion is one-way; documented).
- **Unblocks** — the retention item in the assessment.

## WP-7 · Live GitHub App authentication

- **Prerequisites** — WP-1 (D-GH-1/2/3) and the App credentials from WP-2
  if the owner chose to create them there.
- **Local or external** — local test code and execution; the credential
  itself must exist (owner action).
- **Scope** — live mint of a `contents:read` installation token for
  exactly one authorized repository; expire/refresh behavior; assert no
  write scope appears; verify the token never lands in a log or guest
  path; record a dated proof; rotate per D-GH-3 policy when the owner
  asks; incident handling if the private key is ever suspect (D-GH-4).
- **Exclusions** — no application behavior change beyond the test;
  no admission of App credentials into a guest or a mount.
- **Deliverables** — a dated record: token metadata (never the token),
  the mint request/response shape, the single authorized read, expiry
  honored; a "no write scope" assertion in code reviewable by anyone.
- **Acceptance criteria** — one authenticated live read of the authorized
  repository, no other permission exercised, no token leakage in logs.
- **Rollback / cancellation** — uninstall the App or revoke the key from
  GitHub; the repository's code path stays `GitHubCodeHost(None)`-safe
  by default (verified).
- **Unblocks** — removes the "live minting NOT VERIFIED" blocker.

## WP-8 · Monitoring and operational validation

- **Prerequisites** — WP-1 (D-OPS-1..6) and, for the external half, the
  monitoring instance from WP-2.
- **Local or external** — local design and alert definitions; external
  deployment and drills.
- **Scope** — deploy the chosen monitoring stack (self-hosted Prometheus/
  Grafana/Alertmanager/Loki or the managed alternative D-OPS-1 picked) on
  infrastructure *separate* from the execution host; wire the alert list
  from the runbook §12; verify that logs exported anywhere are redacted
  per D-OPS-2 while audit rows are exported raw; run three drills: (i) a
  killed run, (ii) a host reboot mid-run, (iii) the key manager
  unavailable; confirm alerts fired and the response path works.
- **Exclusions** — no control-plane application features; no monitoring
  of production that does not yet exist.
- **Deliverables** — alert definitions, incident-response tabletop record,
  drill logs (three drills passed), control-plane recovery runbook
  (Postgres rebuild from backup, worker redeploy, monitoring redeploy),
  the retirement rehearsal noted in D-HOST-7.
- **Acceptance criteria** — every drill ended recoverable, alerts fired
  and were seen by the named on-call (D-OPS-3), nothing exposed secrets.
- **Rollback / cancellation** — monitoring can be torn down freely; drill
  outcomes are the record.
- **Unblocks** — the assessment's area 12 FAIL condition; WP-9's
  operational half.

## WP-9 · Independent security review

- **Prerequisites** — WP-4 (evidence complete), WP-5, WP-6, WP-7, WP-8
  as applicable to the production scope sought; the named reviewer from
  D-RUNTIME-5.
- **Local or external** — external (the reviewer must not be the
  implementer, and must not be inside this repository's toolchain).
- **Scope** — reviewer re-derives the control mapping, reads raw probe
  logs against collected reports, verifies the identity/hash chain, checks
  the runtime's threat model honestly (including "is this actually the
  mechanism it claims"), reviews every drill's transcript, and writes a
  decision at the `production_approved` level or a refusal.
- **Exclusions** — no re-implementation during review; no change of the
  composition root while review runs.
- **Deliverables** — a dated written review with findings and the
  explicit authorization decision recorded against a repository SHA.
- **Acceptance criteria** — an unambiguous written authorization or
  refusal; if refusal, the exact gaps named; no "approved with caveats"
  without listing each caveat as a blocker.
- **Rollback / cancellation** — a refusal is a valid outcome; the
  production plan pauses and the gaps become work packages.

## WP-10 · Explicit production go/no-go authorization

- **Prerequisites** — WP-9 (an authorization exists and names the exact
  scope); the owner's money/ownership posture unchanged since WP-1.
- **Local or external** — the owner's decision (external to engineering);
  the wiring after it is local work.
- **Scope** — the owner, with the reviewer's authorization, says go (and
  defines the scope: which mechanisms, which repositories, what scale);
  only then does the composition root change posture, wiring the runtime,
  the sealer, and the retention job as *authorized*.
- **Deliverables** — a dated authorization record; the wiring work
  packages as a subsequent stage.
- **Acceptance criteria** — the repository's own full gate suite stays
  green with the new posture; every assessment-area status that said
  BLOCKED/FAIL for a now-authorized item has moved with evidence.
- **Rollback / cancellation** — reverting authorization returns to today's
  posture; the fail-closed guards exist to make that possible.

---

### Explicit separation of authorization levels

| Work | May start now | Needs WP-1 decisions | Needs WP-2 infrastructure | Needs authorization |
|---|---|---|---|---|
| WP-1 decision records | Yes (owner's) | — | — | — |
| WP-2 provisioning | No | Yes | — | Owner money |
| WP-3 runtime integration | No | Yes | Yes | No (host-side, not product) |
| WP-4 measurement | No | Yes | Yes | No (evidence, not wiring) |
| WP-5 KMS integration | Code-side after WP-1 | Yes | For real-keys test | No (until production) |
| WP-6 retention | No | Yes | No | Yes (product/legal/security) |
| WP-7 live GitHub | No | Yes | Yes | Owner (credentials) |
| WP-8 monitoring | Design now | Yes | Yes | No |
| WP-9 security review | No | — | — | Reviewer assignment |
| WP-10 authorization | No | — | — | **Yes (owner)** |

Nothing in this table has begun in Stage 10K. The next step is entirely
WP-1: the owner's answers in
[INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md) (the highest-leverage
ones are D-HOST-1/3/5, D-RUNTIME-1, D-KMS-1, D-GH-1, D-OPS-1, D-RET-2/4).
