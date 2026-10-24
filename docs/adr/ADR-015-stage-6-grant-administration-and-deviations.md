# ADR-015: Stage 6 grant administration scope and deviations

| | |
|---|---|
| Status | **Accepted** with the operator CLI implementation, 2026-10-23. Membership and grant mutations run through the operator CLI only; no HTTP route. Cross-context binding checks live in composition flow, never in IAM |
| Related | [PRODUCT-SCOPE §3.3, Workflows A and D](../product/PRODUCT-SCOPE.md), [BOUNDED-CONTEXTS › IAM, REPO](../architecture/BOUNDED-CONTEXTS.md), [DEPENDENCY-RULES](../architecture/DEPENDENCY-RULES.md), [IAM-DATA-MODEL](../architecture/IAM-DATA-MODEL.md), [ADR-011](ADR-011-authentication-sessions-and-tokens.md), [ADR-012](ADR-012-event-envelope-and-outbox.md) |

## Context

Stage 6 adds the smallest complete, auditable grant-administration capability:
registering repositories in an org, adding members, and granting or revoking the
four explicit `Grant` values (`repo.view`, `repo.investigate`, `repo.approve`,
`repo.configure`). Two constraints shaped it:

- **Session-admin granting is blocked.** ADR-011 Decision I requires the
  grantee to hold matching code-host access, supplied by REPO's
  `CodeHostAccessVerifier`. That verifier does not exist yet, and the
  dependency matrix forbids IAM → REPO. Building a fake verifier, or letting
  the grants themselves supply a code-host standing, would fabricate evidence.
  Until the verifier exists, no mutation that completes the ADR-011 Decision I
  chain can be authorized from a session.
- **The operator channel already exists.** `bootstrap-org` (ADR-011) and
  `issue-ci-upload-token` (ADR-013) already create orgs, admins, and scoped
  tokens without any HTTP or code-host permission. A narrowly scoped operator
  CLI channel is consistent with that trust.

## Behaviour

- The operator CLI has five subcommands: `register-repo`, `add-member`,
  `grant`, `revoke`, and `show-access` (one unit of work per invocation).
- `register-repo` stores a repository binding in REPO's `repo.repositories`
  (host, owner, name, org). Coordinates are validated and canonical
  (`host = 'github'`; owner and name regexes in the database). Registering a
  duplicate ID or a duplicate (host, owner, name) is reported as
  `RegistrationConflict`; the outcomes are `registered` or
  `already_registered`. The registration appends `repo.repository.registered`
  in REPO's own outbox.
- `add-member` adds a GitHub identity to an org. The caller may not supply a
  user ID as proof of identity: the command names a GitHub user ID and login,
  and the stored `iam.users` row must already exist at that subject. `added`
  or `already_member`.
- `grant` / `revoke` operate on a member's explicit grants over REPO-bound
  repositories. The cross-check — "the repository is not registered in this
  org" — is a flow check in the composition root: IAM does not read REPO's
  records (the dependency matrix forbids IAM → REPO), and the same single
  message covers a foreign-org repo and an unregistered one, so existence is
  never disclosed across orgs. Outcomes: `granted` / `already_held`,
  `revoked` / `not_held`.
- `show-access` renders a member's standing per repo through the same domain
  rules `decide()` uses, listing explicit grants, effective grants (including
  org-admin implication), and per-grant decisions. The code-host standing
  applied is the deployment's read standing, chosen in the identity
  application layer, and is never presented as verified access.
- Every mutation appends exactly one audit event **after** the change, in the
  same transaction and unit of work (ADR-012), with the actor
  (`operator_cli`), channel (`cli`), correlation ID, scope (org, repo, user),
  grant, and outcome. Membership and grant changes first revoke the user's
  live sessions with `privilege_change` (event `iam.session.revoked`,
  org-independent — filtered by subject), unless there are no active sessions
  or the operation would be a no-op.
- Authorization failures, non-canonical IDs, unknown orgs, unknown subjects,
  unsupported grants, and cross-org references are refused with
  `command: message` on stderr and exit code 1; malformed arguments are
  refused before the database is touched. Nothing in stdout or stderr names a
  foreign org's identifiers or a foreign member's identity.

## Deviations

| # | Deviation | What restores the documented behaviour |
|---|---|---|
| 1 | **Membership and grant mutations run only through the operator CLI.** HAR's sign-in Run View and the HTTP route have no administration surface | REPO's `CodeHostAccessVerifier` (ADR-011 I, now implemented — ADR-016), plus an owner decision on member consent, then an authenticated session-admin route |
| 2 | **Grantee code-host access is not checked at grant time.** The verifier exists (ADR-016); the operator channel does not call it yet | An owner decision ([GRANT-AUTHORIZATION-DECISION-SHEET](../operations/GRANT-AUTHORIZATION-DECISION-SHEET.md) Decision A), then the wiring |
| 3 | **`repo.repositories` has no foreign key to IAM and no code-host verification.** "Registered" means the operator bound it; nothing proves the code host's installation | The verifier, and an owner decision on what a binding means (PRODUCT-SCOPE §3.3) |
| 4 | **The binding cross-check lives in the composition root as a flow check** because the dependency matrix forbids IAM → REPO | A shared policy object, if the matrix is ever changed by a new ADR |

## Events

REPO emits `repo.repository.registered` in its own `repo.outbox`, relayed by
the existing worker relay to `aud.events` (the audit source list now admits
`repo` via migration `aud_0003`). IAM mutations keep ADR-012 names:
`iam.membership.granted`, `iam.grant.granted`, `iam.grant.revoked`, and
`iam.session.revoked` on the privilege change.
