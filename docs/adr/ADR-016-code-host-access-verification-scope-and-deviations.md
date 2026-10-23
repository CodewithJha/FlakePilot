# ADR-016: Code-host access verification (installation model) scope and deviations

| | |
|---|---|
| Status | **Accepted** with the implementation, 2026-10-23. `CodeHostAccessVerifier` is real and fail-closed; permission lookups run as FlakePilot's GitHub App installation. Live verification is **NOT VERIFIED** (no App credentials in any deployment this repository controls) |
| Related | [PRODUCT-SCOPE §3.1, Q6](../product/PRODUCT-SCOPE.md), [SYSTEM-ARCHITECTURE §7](../architecture/SYSTEM-ARCHITECTURE.md#7-tenancy-authorization-data-ownership-secrets), [DEPENDENCY-RULES §4](../architecture/DEPENDENCY-RULES.md#4-inverted-ports-cycle-breaking), [ADR-009](ADR-009-initial-provider-adapters.md), [ADR-011](ADR-011-authentication-sessions-and-tokens.md), [ADR-013](ADR-013-stage-4-ingestion-scope-and-deviations.md), [ADR-015](ADR-015-stage-6-grant-administration-and-deviations.md) |

## Context

ADR-011 Decision I makes effective access the intersection of FlakePilot grants and
code-host permission, and DEPENDENCY-RULES §4 fixes the shape: IAM defines the
`CodeHostAccessVerifier` port, REPO implements it, the composition root wires it. The
port did not exist; every consumer supplied a standing by hand (the authenticated Run
View used the deployment's READ standing, labelled not verified; ADR-015 deviation 2
skipped the grantee check at grant time).

Two identity facts bounded the design: sign-in exchanges the OAuth code for identity
only and never retains the user's OAuth token (ADR-009 E), and a repository binding is
a routing fact recorded by the operator, never evidence of anyone's access.

## Decision

- **The port** (`identity/application/verification.py`, IAM-defined) returns one
  `AccessVerification`: verified `NONE` / `READ` / `WRITE`, or unverified
  `UNAVAILABLE` (provider, network, credentials), `INDETERMINATE` (ambiguous or
  refused lookup, unusable handle), `UNBOUND` (the repo is not this org's in REPO's
  records). Only a verified standing is evidence. `standing()` maps every unverified
  outcome to `CodeHostAccess.NONE`, so "cannot say" denies under the intersection rule
  through the unchanged pure policy functions (`decide`, `effective_grants`,
  `may_grant`).
- **The implementation** (`repository/application/verification.py`): resolve the
  binding in `repo.repositories` (a foreign-org or unregistered repo is `UNBOUND` —
  the same non-disclosure the operator CLI uses), then ask the provider for the
  grantee's handle's repository permission on that canonical identity. Pure permission
  lookup; no IAM authorization call; no IAM record reads.
- **The provider check** (`GitHubCodeHost.repository_permission`): `GET
  /repos/{owner}/{repo}/collaborators/{login}/permission` with the installation token
  the App already mints for source pinning (a metadata read). `200` maps
  `admin`/`write`/`maintain` → write standing, `read`/`triage` → read, `none` → no
  access. `404` is ambiguous by the provider's own contract (not a collaborator, or
  the installation cannot see the repo) and is **never** a permission. `401`, `403`
  (including rate limiting), `5xx`, redirects, malformed bodies, and unusable handles
  are refused; nothing falls back to success.
- **Wiring**: the composition root builds the verifier from `FLAKEPILOT_GITHUB_APP_ID`
  and `FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` (already required for the worker's source
  pinning; ADR-009). A deployment without those credentials constructs the same
  verifier around a provider that can mint no token: every lookup answers
  `unavailable` and the view denies, without contacting the host. `create_app` accepts
  a `access_verifier` factory for tests; production passes nothing and gets the real
  one.
- **First consumer, the authenticated Run View**: the composition resolves the run's
  repo inside its org (TI store), reads the principal's handle from IAM's records, has
  REPO's verifier verify that handle against the run's canonical repository (provider
  call outside any database transaction), and passes the resulting standing to
  `RunViewer.classified_report_for`, which authorizes `repo.view` under the
  intersection rule. Unverified outcomes and a verified lack of code-host read deny
  with the same non-disclosing 404 as "no grant". Q6's short cache for reads is not
  built: every view verifies live until an owner decision fixes a TTL.

## Deviations

| # | Deviation | What restores the documented behaviour |
|---|---|---|
| 1 | **Grant-time grantee verification is still not wired.** The operator CLI's `grant` does not call the verifier; ADR-015 deviation 2 stands | An owner decision: whether the operator CLI grant becomes a live-verified command (and what an operator does when GitHub is down), and the member-consent design. The port and implementation exist; wiring is a flow change, not a code gap |
| 2 | **No session-admin HTTP administration route.** The verifier existing does not by itself authorize grant mutations (ADR-015 deviation 1) | The verifier plus the member-consent owner decision, then that route |
| 3 | **Live verification is NOT VERIFIED.** No GitHub App credentials exist in any environment this repository runs in; the endpoint's behaviour under an installation token (metadata read sufficing, `none` for non-collaborators) is documented but not demonstrated against the real host | A deployment configures a real App installation and an operator verifies one granted and one ungranted account end to end |
| 4 | **Freshness (Q6) is live-per-view with no cache** for the Run View; approve/apply, when they arrive, must verify live anyway | An owner decision on a read cache TTL |

## Not changed

- Revision pinning is untouched: `pin_revision` keeps its own mechanism and
  `source_unverified`; the Run View's revision status stays `unchecked`. Access
  verification says nothing about which commit produced a run.
- The operator CLI (`show-access` included) keeps its documented privileged-channel
  behaviour; `show-access` still shows the deployment's read standing, labelled as
  such.
- `ClosedSandbox`, `UnavailableExecutionPlane`, `control_plane_reaper`,
  `RefusedHostHandoff`, and all fail-closed invariants are untouched.

## Events

Verification emits nothing. It is a permission lookup; ADR-012's event names are
unchanged, and no audit record claims a code-host fact that was not verified.