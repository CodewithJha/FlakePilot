# .TECH domain strategy

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and sponsor
goal 16 (`.TECH` domain). Read-only strategy: what buying a `.TECH` hostname
would and would not change, and the exact code-level contract the hostname
must satisfy. **No domain is registered**; purchase and DNS ownership are
owner actions, and the zero-budget constraint (D-HOST-5) stands. The product
today runs correctly with no domain at all — `FLAKEPILOT_PUBLIC_ORIGIN` unset
means sign-in answers 503 and every other capability is unaffected.

## The one rule: no hostname lives in code

There is no default host anywhere in the repository. The public origin is a
validated, normalized environment value:

- `FLAKEPILOT_PUBLIC_ORIGIN` (`Settings.from_environ` → `_origin`): must be
  only `scheme://host[:port]` — no path, query, fragment, or userinfo —
  normalized to lowercase host with the default port omitted. It feeds
  exactly two things: the OAuth redirect URI
  (`{origin}/auth/github/callback`) and the CSRF origin check.
- **Origin exactness is the security property.** The CSRF check compares
  the browser's `Origin` header against this string as browsers send it, so
  the configured value must match what users actually type — a `www.` a
  config forgot, a port the DNS proxy strips, and the check fails closed.
- **TLS is not optional.** `http` origins are refused unless
  `FLAKEPILOT_ALLOW_INSECURE_COOKIES=true` (the documented local-HTTP
  exception, never for a public host). Cookies are `Secure` with the
  `__Host-` prefix while secure — a prefix that additionally pins the cookie
  to path `/`, so any future path-based hosting quirk fails loudly rather
  than silently downgrading.

## What a `.TECH` domain changes, concretely

| Layer | What the domain changes | What it does not change |
|---|---|---|
| DNS | `flakepilot.<yourname>.tech` (or similar) gets an A/AAAA/CNAME record pointing at the hosting entry point ([RENDER-DEPLOYMENT-ARCHITECTURE](RENDER-DEPLOYMENT-ARCHITECTURE.md)) | No record is created by this repository; TTLs and the registrar's NS setup are owner-managed |
| TLS | The public origin is `https://`, so secure cookies stay on and the `__Host-` prefix is valid | No certificate material lives in the code or repo; terminators issue/manage certs |
| Sign-in | The GitHub App's callback URL becomes `https://<domain>/auth/github/callback` — registered **exactly**, scheme/host/path (GitHub compares the redirect URI for exactness) | Tokens stay server-side (`contents:read`, one repository); the domain grants nothing |
| CSRF | The `Origin` users' browsers send now matches the configured origin | The comparison logic, and its fail-closed behavior on mismatch, are untouched |
| Everything else | Nothing | Deterministic classification, authorization, outbox, execution posture are origin-blind |

## Registration checklist (owner actions, in order)

1. **Decide the domain and buy it** (registrar of choice; `.TECH` pricing and
   renewal terms are the buyer's — recorded here as a decision, not a
   recommendation).
2. **Point DNS at the host** before first sign-in use, so the origin is
   stable from the first cookie issued. Renaming an origin later invalidates
   every issued session cookie (different `__Host-` name binding and CSRF
   origin) — users re-authenticate; nothing dangerous, but plan the name
   once.
3. **Update the GitHub App** callback to the exact new origin's
   `/auth/github/callback` path (add the new one alongside any old entry;
   never replace-and-hope mid-deploy).
4. **Set `FLAKEPILOT_PUBLIC_ORIGIN=https://<domain>`** on the API service
   (see [RENDER-DEPLOYMENT-ARCHITECTURE](RENDER-DEPLOYMENT-ARCHITECTURE.md)'s
   secrets table). The settings validator is the last gate: a typo, a stray
   path, or a plain-HTTP origin is a startup refusal, not a misbehaving
   deploy.
5. **Smoke**: sign-in round-trip, cookie carries `__Host-` + `Secure`, the
   CSRF check accepts same-origin requests and rejects cross-origin ones.

## What the domain never does

- **It is not a security boundary.** TLS and origin pinning protect browser
  sessions; every API authorization still flows through the IAM gates, and
  HMAC possession alone is never permission.
- **It does not bake a hostname into config files.** The only place the
  origin exists is the environment of the process that serves browsers —
  local development without a domain keeps working exactly as the README
  records.
- **It does not change the evidence posture.** No host, domain, or
  certificate advances any isolation or production-readiness claim.

Status: **strategy documented, purchase owner-gated**. Local-only operation
without any domain remains fully supported and unchanged.