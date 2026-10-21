# ADR-011: Authentication sessions, CI upload tokens, and access-check details

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-21. Decision B (session lifetimes) is **provisional**. Decision G is accepted in [IAM-DATA-MODEL](../architecture/IAM-DATA-MODEL.md) and the `iam` tables exist. IAM envelopes are written to `iam.outbox` ([ADR-012](ADR-012-event-envelope-and-outbox.md)). The worker copies them to `aud.events`. There is no audit query. Decision F's encrypted table exists and has no production caller |
| Related | [PRODUCT-SCOPE §3, §4, D2](../product/PRODUCT-SCOPE.md), [SYSTEM-ARCHITECTURE §7](../architecture/SYSTEM-ARCHITECTURE.md#7-tenancy-authorization-data-ownership-secrets), [ADR-005](ADR-005-system-of-record-database.md), [ADR-007](ADR-007-implementation-stack-and-licensing.md), [ADR-009](ADR-009-initial-provider-adapters.md), [ADR-010](ADR-010-engineering-toolchain-and-quality-gates.md) |

## Context

- ADR-009 fixes sign-in with the code host (the same GitHub App) for authentication only. No document said how a request is authenticated afterwards, how cross-site request forgery is prevented, or what a CI upload (ingestion) token looks like.
- SYSTEM-ARCHITECTURE §8 A already fixes that only a hash of an ingestion token is stored and the token is shown once.
- PRODUCT-SCOPE §3.1 makes effective access the intersection of FlakePilot grants and code-host permission, and Workflows A and J block a grant when the grantee lacks "the matching code-host access" without defining matching.
- Constraints: permissive-only dependencies (ADR-007); httpx is unavailable without a new licence exception (its certifi dependency is MPL-2.0); secrets only inside INT adapters (ADR-003, ADR-009).

## Decision

| # | Topic | Decision |
|---|---|---|
| A | Authenticating requests | **Server-side opaque sessions.** A random session ID from the standard library (`secrets`), stored only as a SHA-256 hash in an IAM-owned PostgreSQL table. Sent as a cookie with `HttpOnly; Secure; SameSite=Lax` |
| B | Session lifetime (**provisional**) | 12 hours absolute, 2 hours idle. The session ID is rotated on sign-in and on any privilege change. Values are calibrated later |
| C | CSRF for server-rendered pages | `SameSite` cookies plus an `Origin`/`Referer` check on unsafe methods (standard library only) |
| D | CI upload token | `fpi_` followed by 32 random bytes, stored only as a plain SHA-256 hash, sent as `Authorization: Bearer` |
| E | GitHub sign-in adapter (INT) | Standard-library `urllib`, run off the event loop. No new dependency |
| F | Secret store (ADR-009 encrypted table) | **As implemented:** `int_secrets.secrets` and `EncryptedSecretStore` (AES-256-GCM, `cryptography`) exist. The GitHub adapter does not use the store; it reads `FLAKEPILOT_GITHUB_CLIENT_SECRET` from the environment. `create_app` does not construct the store. `FLAKEPILOT_SECRET_KEY` is validated when set and otherwise unused. The store still has no production caller ([ADR-012](ADR-012-event-envelope-and-outbox.md)) |
| H | Denial responses | Not visible to the principal (another org, no grant, no code-host read) → 404 `not_found`, indistinguishable from a missing resource. Visible but the grant is missing → 403 `forbidden` |
| I | "Matching" code-host access for a grant | Granting `repo.approve` requires the grantee to have code-host **write**; every other grant requires code-host **read** |

**Unchanged:** D2 as accepted in PRODUCT-SCOPE. Approvers are holders of `repo.approve` with code-host write; only org admins grant `repo.approve`; no self-approval except in single-member orgs.

**Later implementation (not still pending review):**
- G was accepted in [IAM-DATA-MODEL](../architecture/IAM-DATA-MODEL.md). The `iam` tables, sessions, CSRF check, CI upload tokens, and GitHub adapter are implemented.
- Audit storage: IAM writes the envelope to `iam.outbox` in the same transaction as the change and fails closed ([ADR-012](ADR-012-event-envelope-and-outbox.md)). The worker copies the envelope to `aud.events`. There is no audit query. The SYSTEM-ARCHITECTURE §5 contract is unchanged.

## Alternatives considered

| Topic | Alternative | Why not |
|---|---|---|
| A | Signed stateless cookie (HMAC) | No immediate revocation; needs key management |
| A | Bearer tokens for the API plus sessions for pages | Two mechanisms before any API client exists |
| C | Synchronizer token in every form | Touches every form; the chosen checks suffice with `SameSite=Lax` |
| C | Double-submit cookie | Weaker if a sibling subdomain is compromised |
| D | Keyed hash with a server-side secret | Needs key management; the token is long and random, so a plain hash suffices |
| E | httpx | Needs a licence exception for certifi (MPL-2.0) |
| E | aiohttp | New dependency and licence review for one adapter |
| F | Build the secret store now | No consumer until the GitHub adapter exists |

## Consequences

- **Positive:** sessions and tokens can be revoked immediately and never stored in recoverable form. Denials never reveal resources in other orgs. The original decision added no dependency; `cryptography` arrived later with the unused secret store.
- **Negative:** one database lookup per authenticated request. A CSRF check is needed on every unsafe method. Synchronous `urllib` calls take a worker thread each.
- **Follow-ups:** calibrate B. Wire the secret store only together with `int_secrets.outbox`. Outbox pruning and an authorized audit query are still deferred.
