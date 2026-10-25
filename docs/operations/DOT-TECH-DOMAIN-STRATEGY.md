# .TECH domain strategy

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and sponsor
goal 16 (`.TECH` domain). What a `.TECH` hostname changes and does not
change, the exact code-level contract it must satisfy, and the
[registered state](#registered-state-2026-10-25). The owner registered
`flakepilot.tech` on 2026-10-25 and the deployment serves
`https://app.flakepilot.tech`. The product still runs correctly with no domain
at all — `FLAKEPILOT_PUBLIC_ORIGIN` unset means sign-in answers 503 and every
other capability is unaffected.

## Registered state (2026-10-25)

| Item | Value | Status |
|---|---|---|
| Domain | `flakepilot.tech`, registered by the owner through get.tech (registrar of record Namify Domains Inc) on 2026-10-25, expiring 2027-10-25; nameservers `tech-domains.{venus,mars,earth,mercury}.orderbox-dns.com` | **VERIFIED** (RDAP; NS via DNS-over-HTTPS) |
| Record | `CNAME app → flakepilot-api.onrender.com` (TTL 14400) | **VERIFIED** (DNS-over-HTTPS) |
| Render custom domain | `app.flakepilot.tech` on `flakepilot-api`, verified 2026-10-25 | **VERIFIED** (Render API) |
| Certificate | issued by Google Trust Services (`WE1`), subject and SAN `app.flakepilot.tech`, valid 2026-10-25 to 2027-01-23; Render renews it | **VERIFIED** (TLS handshake) |
| Origin | `FLAKEPILOT_PUBLIC_ORIGIN=https://app.flakepilot.tech`; GitHub App callback `https://app.flakepilot.tech/auth/github/callback` and homepage `https://app.flakepilot.tech` | **VERIFIED**: the owner signed in through it on 2026-10-25 |

Sign-in starts at `https://app.flakepilot.tech/auth/github/login`. The
renewal price was not checked; renewal is an owner action.

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
| DNS | The chosen host gets a CNAME (subdomain) or ALIAS/ANAME/A record (apex) pointing at the hosting entry point — exact records under "DNS records for Render" below; **no AAAA** record (Render serves IPv4) | No record is created by this repository; TTLs and the registrar's NS setup are owner-managed |
| TLS | The public origin is `https://`, so secure cookies stay on and the `__Host-` prefix is valid | No certificate material lives in the code or repo; terminators issue/manage certs |
| Sign-in | The GitHub App's callback URL becomes `https://<domain>/auth/github/callback` — registered **exactly**, scheme/host/path (GitHub compares the redirect URI for exactness) | Tokens stay server-side (`contents:read`, one repository); the domain grants nothing |
| CSRF | The `Origin` users' browsers send now matches the configured origin | The comparison logic, and its fail-closed behavior on mismatch, are untouched |
| Everything else | Nothing | Deterministic classification, authorization, outbox, execution posture are origin-blind |

## Candidate availability (read-only RDAP, 2026-10-24)

Checked through the public RDAP bootstrap `https://rdap.org/domain/<name>`,
which redirects `.tech` to the registry's server `rdap.radix.host`. A 404 there
means "no registration exists right now", not a reservation or a price; it can
change at any moment until someone buys the name.

| Name | RDAP answer | Meaning |
|---|---|---|
| `flakepilot.tech` | 404, "Domain flakepilot.tech is available for registration" (Radix) | Unregistered at check time |
| `getflakepilot.tech` | 404 (Radix) | Unregistered at check time |
| `flakepilot.dev` | 404 (`pubapi.registry.google`) | Unregistered at check time (not a `.TECH` name; listed as a fallback) |

Price and renewal terms were **not** checked (registrar-specific; `.TECH`
renewals are typically far above first-year promotional prices — read the
renewal line before buying).

## DNS records for Render (the subdomain row is in use; the others are vendor documentation, NOT VERIFIED)

Add the custom domain on the API service first (Settings → Custom Domains);
Render then shows the target `<service>.onrender.com` and verifies the record.

| Hostname | Record | Value |
|---|---|---|
| `app.flakepilot.tech` (recommended: a subdomain) | `CNAME` | `flakepilot-api.onrender.com` (the service's actual `onrender.com` host) |
| `flakepilot.tech` (apex) | `ALIAS`/`ANAME` (or flattened CNAME) | `flakepilot-api.onrender.com` |
| `flakepilot.tech` (apex, provider without ALIAS) | `A` | `216.24.57.1` (Render's load balancer) |
| any | `AAAA` | **remove** — Render serves IPv4 only |

Render issues and renews the TLS certificate once the record verifies. Adding
an apex domain also adds a `www.` redirect (Render behavior) — the
**configured origin must be the one users land on**, because the CSRF check
compares the browser's `Origin` exactly. A subdomain avoids the apex/`www`
ambiguity entirely, so `https://app.flakepilot.tech` is the recommended
`FLAKEPILOT_PUBLIC_ORIGIN`. Hobby workspaces include two custom domains.

## Registration checklist (owner actions, in order; all done 2026-10-25)

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

Status: **registered and live 2026-10-25** (`app.flakepilot.tech`). Local-only
operation without any domain remains fully supported and unchanged.