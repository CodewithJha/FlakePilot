# DigitalOcean isolation assessment

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and sponsor
goal 11 (DigitalOcean). Read-only assessment of a real, trackable claim: what
evidence level a DigitalOcean-hosted execution host could honestly reach, and
what it still could not. Nothing is provisioned or purchased; the live
posture is unchanged — **no untrusted code executes in this product today**
([EXECUTION](EXECUTION.md), `ClosedSandbox`, `UnavailableIsolation`).

## The measure: the evidence ladder

Isolation claims rank exactly as
[PRODUCTION-READINESS](PRODUCTION-READINESS-ASSESSMENT.md) records:

```
simulated < shared_kernel_container < dedicated_host_container
           < per_run_microvm < production_approved
```

A lower level never satisfies a higher requirement, and every level is claimed
only after real evidence on real infrastructure is reviewed by a named
security reviewer. A green test suite is never evidence of isolation.

## What a DigitalOcean droplet could be

A droplet (the provider's Linux VM) offers: exclusive tenancy at the VM level,
operator-controlled firewall (default-deny egress is enforceable), and no
teammate-facing shared environment. That is exactly the shape of the
**dedicated execution host** contract in
[EXECUTION-HOST](EXECUTION-HOST.md): a different trust boundary holding
repository checkout and runtime, with — and this is the invariant, not a
feature list — no database URL, no session secrets, no GitHub App private
key, no artifact keys, no host-ledger key, and no broad mounts or privileged
capabilities from the control plane.

Honest reachable level for a droplet deployment: **`dedicated_host_container`
at most**, and only after all of:

1. The droplet runs the host role verbatim per
   [EXECUTION-HOST-RUNBOOK](EXECUTION-HOST-RUNBOOK.md) — separate OS user,
   egress limited to the allow-listed control-plane endpoint, local disk only.
2. Host-enforced CPU, memory, PID, disk, output, and wall limits (the checks
   `IsolationChecks` enumerate) — enforced on the droplet, not merely coded
   in the manager.
3. A named security reviewer reviews the collected evidence on that real
   infrastructure. Evidence without review does not advance the ladder.

## What a droplet could not be

**`per_run_microvm` is out of reach on the droplet path.** Per-run microVM
isolation (one Firecracker microVM per run, per the
[FIRECRACKER-FEASIBILITY-ASSESSMENT](FIRECRACKER-FEASIBILITY-ASSESSMENT.md)
candidate assessment) requires kernel-level capabilities Firecracker depends
on (bare-metal or enabled `/dev/kvm` with the Firecracker threat-model
posture), plus per-run root disks and attestable boot chains a standard
shared-CPU droplet does not attestably provide. Any claim of microVM-grade
isolation on a plain droplet would therefore be fabricated evidence — the
one thing this repository's rules forbid.

DigitalOcean does offer bare-metal-adjacent products and other runtimes;
whether such an offering actually satisfies the Firecracker preconditions is
**Unknown** from public documentation alone and is out of scope here: this
page only (a) fixes the droplet's ceiling and (b) records the honest
alternative below.

## Re-check, 2026-10-24 (public sources only, NOT VERIFIED on infrastructure)

The conclusions above still hold. Two facts were added from public sources:

- **Nested virtualization on droplets is unsupported, not absent.**
  DigitalOcean publishes no support statement for nested virtualization; its
  live-migration documentation says nested VMs must be restarted after a live
  migration (as quoted by a third-party provider comparison dated
  2026-10-15). Community reports show `/dev/kvm` present on some droplets.
  An unsupported capability that a routine host migration can interrupt is not
  a basis for `per_run_microvm` evidence, so the droplet ceiling stays
  `dedicated_host_container`.
- **DigitalOcean MicroVMs (public preview, announced/updated 2026-10-09)** are
  a *managed* Firecracker-based product: one microVM per session, created from
  a container image, reached over an authenticated HTTPS endpoint, optionally
  inside a VPC. That is a new candidate for a later per-run isolation stage,
  and it is **unassessed**: the host is operated by the vendor, so this
  repository's host contract (host-owned runtime inventory, host ledger, HMAC
  frames, host-enforced limits, default-deny egress) does not map onto it as
  written, its egress controls and limit enforcement are unknown, and preview
  products carry no stability commitment. Using it would need its own
  assessment and threat model, the same named-reviewer evidence chain, and an
  ADR — never a configuration change. Until then nothing about it changes the
  fail-closed inventory.

## Recommendation for the goal

Instead of force-fitting DigitalOcean into the isolation role, the phase
records a replacement path consistent with the 10J ladder:

1. **Dedicated execution host, provider-agnostic.** Any compliant VM host
   (DigitalOcean droplet included) is evaluated against the same
   EXECUTION-HOST contract; the provider is a procurement detail, and the
   gate is the evidence chain (B1→B4) reviewed on real infrastructure.
2. **Per-run microVM stays a separate, later stage** with Firecracker as the
   assessed candidate and its own threat model document — never implied from
   VM hosting.
3. **Nothing changes upstream of evidence.** The control plane stays
   fail-closed (`UnavailableIsolation`) until the reviewed ladder move
   exists. No environment variable can conjure the missing checks
   ([EXECUTION-HOST](EXECUTION-HOST.md); AGENTS invariants).

Status: **assessment recorded, owner-gated** — provisioning needs an
owner-authorized spend (D-HOST-5), region/residency answers (D-HOST-3), and a
named security reviewer (D-RUNTIME-1/5). Until then no host exists, and this
document's only action was honest analysis.
