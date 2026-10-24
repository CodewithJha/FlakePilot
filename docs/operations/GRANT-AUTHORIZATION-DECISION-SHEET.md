# Owner decision sheet — grant-time authorization (Decisions A–E)

Stage 7 review aid for the repository owner. Like
[OWNER-DECISION-SHEET](OWNER-DECISION-SHEET.md) (stage 10K infrastructure, kept
separate), this sheet introduces no new policy and changes no behavior: it
derives the minimum decisions needed before the grant flow changes, from source
verified on 2026-10-24 at commit `0c1e3f8`. Each item distinguishes **existing
behavior** (tested, in `main`) from a **recommended decision** that has no
effect until the owner selects it.

## 1. What `main` does now (evidence)

**Grant time** — `grant` (`interface/composition_root/administration.py`,
`_change_grant` → `identity/application/administration.py:grant_repo_access`):

1. Input validation before any database access (canonical UUIDs, GitHub
   subject format, grant name).
2. One short REPO transaction reads the binding (`SqlRepoDirectory.get`);
   a foreign-org or unregistered repo is refused with the single message
   "the repository is not registered in this org" (existence never disclosed
   across orgs).
3. One IAM unit of work: org exists → user found by (issuer, GitHub subject) —
   internal IDs are never accepted as input → member check →
   `iam.repo_grants` row with `granted_by = OPERATOR_GRANTER_ID` → audit
   `iam.grant.granted` (payload: `{"grant": <name>}`; actor `operator_cli`)
   → the member's active sessions revoked with `privilege_change`.
4. The **`CodeHostAccessVerifier` is not consulted**, and neither the granter
   nor grantee side runs `may_grant()`. This is ADR-015 deviation 2; a repeat
   returns `already_held` and writes nothing.

**Read time** — the authenticated Run View
(`interface/composition_root/run_views.py`, `RunViews.classified`): resolves
the run's repo inside the org (TI store, one lookup), reads the session
principal's handle (`Iam.login`), has REPO's `BoundCodeAccessVerifier` verify
that handle against the run's canonical repository (provider call **outside**
any database transaction), then `decide()` applies the intersection rule
(PRODUCT-SCOPE §3.1). Unverified outcomes (`UNAVAILABLE`, `INDETERMINATE`,
`UNBOUND`) and verified `NONE` all deny with the same 404 `not_found`. Tests:
`tests/unit/interface/test_run_views_http.py` (unit, fakes),
`tests/integration/test_ingestion_postgres.py::test_run_view_authorizes_a_signed_in_member_over_real_tables`
(real SQL, provider answer canned), plus
`tests/unit/repository/application/test_access_verification.py` and
`tests/unit/identity/application/test_verification.py` (the fail-closed
contract, and the `may_grant()` composition).

**Operator display** — `show-access` renders through the same `decide()` rules
with the deployment's read standing, labelled "this is not verified access";
the operator channel never consults the verifier.

**Audit**: every mutation appends exactly one event after the change, in the
same transaction (ADR-012). No event claims a code-host fact; payloads carry
the grant name only, and session-revocation events name no session IDs.

---

## 2. Decision A — grant-time verification

| | A1 live-verify at grant | A2 intent + live enforcement at read (**recommended**) | A3 staged pending/active grants |
|---|---|---|---|
| What the command does | Before writing, verify the grantee's handle against the binding; refuse (nothing written) unless verified ≥ required level (Decision I: write for `repo.approve`, read otherwise) | Record the grant as today; ADR-011 I is enforced at every use site by `decide()` over a live verification | Record with a pending state; promote to active through a follow-up (auto re-check or member/owner confirmation) |
| Authorization consequence | Grants can only exist for verified access — the strictest reading of Decision I | Today's fail-closed behavior: an unverified grantee's grant is inert everywhere it matters | Same as A2 until promotion; after promotion identical |
| Code-host outage | The operator cannot grant at all while GitHub is unavailable — a new administrative failure mode, needing a policy for exceptions (which one? who records them?) | Granting proceeds; access stays off until the host is reachable | The outage defers promotion, not recording; same exception question as A1 in miniature |
| Audit semantics | New event payload must distinguish "refused: unverified" from existing refusals; adds a code-host-derived fact (guarded in ADR-016 §Events spirit) | Unchanged — no grant event claims a code-host fact | Pending→active transitions need two events per grant and a motivating reason; the largest audit-semantics change |
| `decide()` compatibility | Unchanged | Unchanged | Unchanged, but `effective_grants` gains a state dimension (schema change) |
| Implementation scope | Composition flow + refusal messages + tests; no schema change | None for the flow; only the Decision-E runbook makes the deployment able to say "verified" | New column/state machine, migration, events, session semantics, UI for pending state |
| Records | Would *change* ADR-015's deviation 2 — requires the owner decision this sheet asks for | *Is* ADR-015's recorded deviation, unchanged | Exceeds ADR-015's scope; would supersede it |

**Recommendation: A2.** It preserves the shipped fail-closed invariant (the
intersection denies at use time, so a wrongly issued grant is inert),
exercises the verifier where evidence matters (a human reading a run), keeps
operator authority independent of code-host availability, and requires no
schema or audit-semantics change. A1/A3 remain open: if the owner prefers
A1 or A3, ADR-015 deviation 2 and the `grant` command's edge behavior must be
revised by a new ADR first — this recommendation is not owner approval.

## 3. Decision B — verifier outcomes per flow

Consequences if each outcome occurs, per affected flow (**existing** =
implemented and tested; none changes without an owner decision):

| Outcome | Session Run View (existing) | Operator `grant` under A2 (existing) | Operator `show-access` (existing) | Future session-admin route (proposed uses of `may_grant()`) |
|---|---|---|---|---|
| `UNAVAILABLE` (provider/network/credentials) | 404 `not_found`, no run-store read | Refusal "not registered in this org" only if the binding check fails (it never consults the verifier); otherwise granted, still subject to read-time denial | Deployment-read display, muted as not verified | No grant created |
| `INDETERMINATE` (ambiguous 404, refused lookup, unusable handle) | 404 `not_found` | as above | as above | No grant created |
| `UNBOUND` (foreign-org/unregistered) | 404 `not_found` | The grant is refused at the binding check before the verifier would run | The repo is simply not listed | No grant created |
| verified `NONE` | 404 `not_found` (a fact, not an error) | as under A2 | as above (display cannot see it) | No grant created |
| verified `READ` | Run served if the grant allows; `repo.approve` still needs write | as above | as above | Grant except `repo.approve` |
| verified `WRITE` | Run served | as above | as above | All grants, granter rules still apply (D2: admins only for `repo.approve`) |

Non-disclosure is preserved in every row: the denied Run View is the same 404
for all unverified outcomes and verified `NONE`, and `UNBOUND` is
indistinguishable from "unknown repo" to the caller.

## 4. Decision C — minimum member-consent mechanism

ADR-015 deviation 1 requires, before any authenticated session-admin route
exists: (a) the flow checks `may_grant()` with the verifier's verified
standing (the composition is now pinned by tests
`tests/unit/identity/application/test_verification.py`), and (b) a member
consent model — *nothing in the codebase defines it today* (no owner decision
exists; no route exists). The minimum mechanism that would satisfy Workflows
A/J without new trust assumptions is a consent record inside IAM's data model
(who consents to being administered by whom; audited, revoke-able by the
member), created by a new ADR that also defines how an admin obtains consent
and what happens when consent is withdrawn mid-flight. **Nothing is
implemented here; implementing a route before that ADR would violate ADR-015
deviation 1 as written.**

## 5. Decision D — verification delay vs cache

Cost measured in code: one verification = one GitHub REST call per classified
run view (plus the installation-token mint, already cached per repo by
`InstallationMinter` until expiry). GitHub's documented REST rate limit for
App installations is 5,000 requests/hour (documented, not live-verified).
Revocation responsiveness equals cache age: a TTL of T bounds how long a
revoked member keeps read ability at the *FlakePilot* surface (the code host
itself still decides nothing here).

**Recommendation: stay live-per-view** until view traffic makes the rate limit
real in a staging measurement; if a cache is later chosen (owner decision),
the candidate shape is: verified-*positive* results only, short TTL
(minutes), invalidated by the store's own session-revocation call and by any
`iam.grant.revoked` for the repo, never caching unverified outcomes or
denials, and ADR-016 updated first. Caching denials would freeze outage
states and is out of the question; caching nothing until the semantics are
owner-approved is the fail-closed default.

## 6. Decision E — staging-only live-verification runbook

Nothing in this section has been executed: no App exists, no credentials are
configured anywhere, and the repository has no spending authority. All steps
are staging-only and none touches production data or external state without
the owner performing them.

1. **Create the App (GitHub settings, owner-operated; free):**
   GitHub App registration with callback-less use (the URL is used for
   sign-in only if configured); permissions: **Contents: read** only
   (Metadata: read is implied). No Webhook, no org-wide install. Scope the
   installation to **one dedicated staging repository** ("only select
   repositories"), owned by a staging account/organization whose access loss
   cannot affect anything else.
2. **Credentials into the staging environment only:**
   `FLAKEPILOT_GITHUB_APP_ID` (numeric) and
   `FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` (the unencrypted PEM; `Settings`
   requires both or neither, `private_key_ok` refuses anything that does not
   load, and the value is a `Secret` — no repr, no logs). Command substitution
   `export FLAKEPILOT_GITHUB_APP_PRIVATE_KEY="$(cat key.pem)"` preserves PEM
   newlines; never write the key into a committed file, a test, or the shell
   history.
3. **Validate redacted, in order:**
   - The settings nonce check first: start the app against the staging
     database — misconfiguration must fail fast with a `SettingsError` that
     names the variable and never the value.
   - The worker pins a revision on the staging repository
     (existing `contents:read` path — same credentials, so one validation
     exercises both consumers).
   - One collaborator (read access) with a session loads the authenticated
     run view → 200; a member with the grant but **without** code-host access
     (remove from the repo) → 404 `not_found`; token lookup requests must
     never appear in logs (URLs redact the token; assert with the staged
     logs).
   - Stop the App credentials (unset the variables) and re-run step 3 — the
     view must go to 404 (fail closed without the App), proving
     `UNAVAILABLE` handling live.
4. **Record the result** against ADR-016 deviation 3: what was observed, the
   commit SHA, and the redacted request counts; live verification remains
   **NOT VERIFIED** until that run.

**External prerequisites (complete list):** a GitHub staging account or
organization the owner controls; one disposable repository with test runs
ingested; the App credentials above; one collaborator account to observe
denied/allowed outcomes. No paid services, no domains, no production data.

---

## 7. Decisions requiring owner approval

- **A**: grant-time model (A1 / A2 / A3) — A2 is recommended, not assumed.
- **B**: confirm the outcome table (it is implemented behavior; a change
  requires an owner decision).
- **C**: the consent mechanism and an ADR before any session-admin route.
- **D**: live-per-view now? if later cached: TTL, invalidation triggers,
  positive-only rule — before any caching code exists.
- **E**: authorization to create the staging App and the repository choice;
  the runbook is ready for the owner to execute.

## 8. Independent work shipped with this sheet

Documentation drift was corrected — grant-path docstrings and the
`show-access` operator output still said "no code-host verifier exists / does
not exist yet", written before ADR-016; they now state the actual policy: the
verifier exists, the operator channel does not consult it (ADR-015 deviation
2, owner decision pending). No code object or behavior changed; new tests pin
the verifier-outcome → `may_grant()`/`decide()` composition that any future
session-admin flow must replicate. Gates and the commit SHA are in the sprint
report.
