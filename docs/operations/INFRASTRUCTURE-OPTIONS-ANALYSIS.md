# Infrastructure options analysis

Stage 10K. Options analysis for the decisions in
[INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md). Nothing is
provisioned, registered, or purchased, and no vendor is selected — each row
is information for an owner decision.

**Status of this comparison: preliminary.** Verified means checked against
the current repository. Research means an external source, with the source
and the date checked (2026-10-23 unless stated otherwise) cited inline. No
price, certification, performance number, security guarantee, or
availability claim is asserted beyond what a cited source states; where a
source is vendor-adjacent it is treated as marketing, not independent
security proof, and follows the standing rule: **a reviewer confirms,
marketing does not.** Options rejected for lack of usable evidence are
listed with their unknowns rather than guessed.

## 1. Dedicated Linux execution hosting

The requirement (verified in `execution/domain/capability.py` and the
runbook): a Linux host with KVM (`/dev/kvm`), single tenant, administratively
owned by one named person, rebuildable, with the host kernel itself part of
the evidence fingerprint.

| | Bare-metal cloud instance | Cloud VM with nested KVM | Self-managed dedicated server |
|---|---|---|---|
| KVM without nesting | Yes (per provider bare-metal SKUs) | No — nested | Yes |
| Boundary strength | Strongest; one hypervisor | Weaker: VMM inside VM | Strongest |
| Time to first evidence | Days to weeks (budget → contract) | Fastest | Depends on hardware in hand |
| Kernel control | Ours (chosen image) | Provider-managed host kernel | Ours |
| Recurring/month spend line | Medium–high on premium clouds; secondary providers list single-digit-to-low-double-digit USD/month class — Research anecdote, checked 2026-10-23, not priced here | Low | Lowest (if owned); colocation adds rack/power |
| Operational burden | Medium (provider real estate) | Low | High (hardware, power, network, retirement) |
| Data residency | Per provider/region choice | Inherits provider's residency | Whichever room it is in |
| Evidence-fingerprint stability | High (fixed host/kernel) | Medium (provider can change VM hosts, kernels) | High |
| Vendor lock-in | Medium | Medium-high (nested-virt quirks are per-cloud) | None |
| Integration effort | Medium | Low–Medium | Medium |

Sources: `blog.alexellis.io` — "KVM with nested virtualisation can be found
on Azure, Digital Ocean and Google Cloud", and a documented cost-anecdote
comparison of bare-metal on AWS (~3.5k USD/mo class) against secondary
providers (~100 USD/mo class), checked 2026-10-23; `blogs.oracle.com`
(Jan 15 2026) — Firecracker on OCI bare metal with measured benchmarks.
These sources inform the cost *category* only; exact pricing must be
verified at purchase time and is deliberately not asserted here.

Assessment. Compatibility: all three satisfy the architecture's host
contract; the bare-metal/self-managed columns require no design change,
while nested KVM adds a second trust boundary a reviewer must argue.
Trust-boundary implication: nested KVM means a kernel exploit path also
crosses *two* hypervisors; it is not prohibited but increases review
effort. Cost model: cloud VM the cheapest recurring; bare-metal premium
clouds the most expensive (Research anecdote). Lock-in: strongest for
nested-virt because host/kernel specifics are provider-chosen. Evidence
required before acceptance: for any option, a dated decision record plus
first-boot kernel identity (runbook §4–§5). Unknowns: owner's budget,
residency constraint (D-HOST-3), whether an existing machine is available
(D-HOST-1 recommendation assumes one fixed host).

## 2. Per-run microVM or equivalent isolation runtime

The requirement: separate kernel per untrusted run, limits enforced outside
the guest, no guest access to host mounts or the runtime socket, digest-
pinned immutable images, agent channel that does not depend on the guest
network, and actively maintained security patches.

| | Firecracker | Cloud Hypervisor | QEMU (`microvm` type) | Kata Containers | gVisor |
|---|---|---|---|---|---|
| Language / license | Rust, Apache-2.0 | Rust, Apache-2.0 | C, GPL | Rust/Go containers runtime | Go userspace kernel |
| Isolation model | KVM microVM, ~5 virtio devices, jailer (chroot/namespaces/cgroups/seccomp) | KVM or MSHV microVM, broader device surface | Full system emulator with a minimal machine type | Container-runtime wrapper over a VMM; Kubernetes RuntimeClass | Userspace kernel intercepting syscalls |
| Separate kernel per run | Yes | Yes | Yes | Yes | User-space kernel, host kernel still behind it |
| Boot/density claims (vendor/community, checked 2026-10-23) | <125 ms boot, ~5 MiB overhead, up to 150 VMs/s per host | ~75 ms slower than Firecracker; richer devices, no density figure cited | 0.4–1.2 s boot with legacy disabled (`qemu-system-x86-microvm` ships with Ubuntu) | Inherited from the chosen VMM | No VM boot; syscall overhead instead |
| Needs Kubernetes/orchestrator | No (API + jailer enough) | No | No | **Yes — primary target** | Works inside Kubernetes or standalone |
| Needs `/dev/kvm` | **Yes** | Yes | Yes | Yes (VMM-dependent) | No (runs without KVM) |
| Notable limitations | No GPU/PCI passthrough, no CPU/memory hotplug, no live migration, no virtio-fs, Linux/OSv guests only | Broader attack surface than Firecracker; fewer production references | ~2M lines C; the largest attack surface of the four VM options | Adds an orchestration layer atop a VMM; still one VMM underneath | Weaker isolation than a VM boundary (userspace kernel design); Google-run in Cloud Run/GKE |
| Security-patch activity 2026 | Release notes list the virtio PCI init-sequence hardening fixing **CVE-2026-5747** (checked 2026-10-23) — active | Maintained by Microsoft/Cyberus/Tencent et al.; v53 referenced in Kata 2026 releases | Mature, universally deployed | Active upstream community | Google-run in production (Cloud Run) |
| Production users for untrusted code | AWS Lambda/Fargate, Fly.io, Vercel Sandbox, E2B (per cited write-ups) | Fly.io (GPU), Kata, Ubicloud, Northflank | Default Kata/KubeVirt backend | Baidu and other containerized deployments | Google Cloud Run, GKE Sandbox |
| Vendor lock-in (self-hosted) | None (Apache-2.0, self-managed) | None | None | None | None |
| Integration effort for FlakePilot | Medium: adapter, jailer config, vsock agent | Medium: similar, with extra device choices we do not need | Medium–high: the device model must be argued to a reviewer | High: we do not run Kubernetes; the ledger is the orchestrator | Medium: but would need the level ladder to define a non-`per_run_microvm` target for it |

Sources, checked 2026-10-23: `firecracker-microvm.github.io` (device
count/boots, licensing); `safeguard.sh` runtime comparison (jailer/security
model, cgroup/namespace restrictions); `emirb.github.io` microVM-isolation
2026 survey (line counts, Cloud Hypervisor maintainer set, QEMU microvm
details); `kata-containers` `docs/design/virtualization.md` (VMM versions in
current releases: Firecracker v1.12.1, QEMU v10.2.1, Cloud Hypervisor v53.0,
Dragonball default); `fly.io` learn pages and Northflank/Depot/PandaStack
comparisons (feature matrices); Kata blog (Kubernetes RuntimeClass
integration). Rate limiters/jailer/vsock/MMDS/snapshots: security-training
video transcript collated from Firecracker documentation direction.

Assessment. Compatibility: Firecracker's *limitations* match FlakePilot's
requirements (no shared filesystem is a feature here; no hotplug matches
one-shot runs; rate limiters map onto output/wall caps). Trust-boundary
implication: QEMU's larger surface (~2M lines vs. Firecracker's ~100k) is
the review-effort driver; Cloud Hypervisor is the middle. Operational
burden: Firecracker and Cloud Hypervisor similar; Kata adds Kubernetes; all
self-hosted options leave the vendor lock-in at zero. Integration effort:
lowest-to-medium for Firecracker given the existing ledger/reaper/HostGate
design; a new `MECHANISM_LEVEL` entry for the runtime mechanism is added
only after a real measurement exists (verified rule in
`evidence_report.py`). Evidence required before acceptance: runbook §12's
identity fingerprint + hash chain, all six limits observed at their caps
from inside the guest, cleanup proofs, and independent review of the whole
bundle. Unknowns: no performance, certification, or availability figure in
this comparison has been reproduced locally; a host kernel fully compatible
with the chosen runtime must be pinned (D-RUNTIME-1 stays an owner
decision).

## 3. External key management

The requirement (verified in `integrations/application/sealing.py`,
`sealed_store.py`, `aes_gcm.py`): an envelope design — wrapped keys carry
key ids and a versioned blob format, the provider returns wrapped-DEK bytes
on `open_key`, `UnavailableKeyProvider` and `UnconfiguredSealer` fail
closed, and the guest receives no artifact keys ever.

| | Cloud KMS (AWS KMS / GCP Cloud KMS / Azure Key Vault) | HashiCorp Vault (self-hosted) | External/HYOK KMS (Fortanix/Thales/Futurex class) |
|---|---|---|---|
| Operator burden | Low (managed; keys non-exportable, calls audited) | High (unseal, storage backend, upgrade path) | Customer-run; highest |
| Key custody | Cloud-held KEK never returns; key material region-pinned (per AWS/GCP docs, checked 2026-10-23) | Ours, in Vault storage | Ours, outside the cloud entirely |
| Failure semantics | Central service outage = no new key operations; documented store behavior: refuse, never fall back to plaintext | Vault outage = same fail-closed behavior; unseal process adds human dependency | Every operation is a remote call — latency and availability dependency (per cited comparison) |
| Audit of key use | Provider-accounted (CloudTrail/Cloud Audit Logs class) | Vault operation logs | External HSM logs |
| Cost model (category) | Usage-metered per key and per API call; HSM tiers cost more | Infrastructure + effort cost; license tier for enterprise features | Hardware or service contract |
| Lock-in | Highest (provider-specific APIs and regional behavior; multi-region keys explicit) | Low (self-managed, portable) | Strength comes with vendor diversity; EKM partners are provider-curated per Google docs |
| Fits `ArtifactKeyProvider` (envelope) | Yes, ideally — cloud KMS wraps data keys and never returns the KEK (Research, checked 2026-10-23) | Yes, though Vault is broader than KMS (dynamic secrets/PKI also possible) | Yes — wrap/unwrap semantics; Azure external-key mode supports wrap/unwrap only |

Sources, checked 2026-10-23: `qcecuring.com` AWS-vs-Azure-vs-GCP KMS
comparison (key types, EKM semantics, Vault positioning); Google Cloud
security docs (EKM partner list, 24-hour destruction delay); GitGuardian
and Infisical write-ups on KMS vs secrets-management division of labor.

Assessment. Compatibility: any of the three satisfies the envelope design;
the repository code requires only `seal_key`/`open_key` semantics, so the
choice is operationally rather than architecturally driven. Trust boundary:
keys never reach guests or workers — cloud KMS calls happen from the
control plane/worker only (Guest receives nothing regardless of choice —
verified `D-KMS-3`). Operational burden matters most for a single-operator
project; this is why the recommendation steers to managed-cloud KMS as the
default *if* the artifact store sits on the same cloud, and defers vendor
selection to D-HOST-1's outcome. Evidence before acceptance: a real
end-to-end round-trip (seal → store → read-back against the real manager in a deployed
configuration), a documented unavailability drill (key manager offline), and the current
plaintext-at-rest limitation remains **explicitly stated in every
readiness document until that round-trip exists**. Unknowns: pricing exact
numbers were deliberately not cited; certification needs are unknown
(D-HOST-3 dependency).

## 4. Monitoring and alerting

The requirement (verified in the runbook alert list and the assessment):
alert on authority unavailability, runtime unavailability, unreadable
ledger, repeated rejected macs, and cleanup unable to run — monitored
*outside* the host it watches, with logs leaving the machine only after
redaction.

| | Self-hosted OSS stack: Prometheus + Grafana + Alertmanager + Loki | Managed observability (Grafana Cloud class) | Cron/heartbeat minimum |
|---|---|---|---|
| Operational burden | Medium (another service to upgrade/secure) | Low | Low |
| Cost category (unverified numbers) | Infra only | Usage-metered recurring | Negligible |
| Maturity / community | CNCF-hosted, Apache-2.0; production-standard (Research, checked 2026-10-23) | Managed product from the Grafana ecosystem (vendor marketing — cannot be treated as independent security proof) | No |
| Alert fidelity | PromQL rules; dedup/routing via Alertmanager | Vendor-managed alerting | Latching only; misses alert richness |
| Data residency | Ours | Vendor's region policies | Ours |
| Lock-in | Low | Medium | None |
| Integration effort | Medium (a scrape config, runbooks, an alert file); must run on separate infrastructure from the watched host | Low | Low |
| Audit-integrity fit | Exports audit rows raw (redaction decision D-OPS-2) | Same, with vendor redaction defaults we must review | n/a |

Sources, checked 2026-10-23: `prometheus.io` (Apache-2.0, governance,
PromQL/Alertmanager model); Grafana docs on getting started; community
write-ups on the Prometheus/Loki/Tempo stack and self-hosting caveats
(need to pair storage backends; alert routing complexity at scale — we are
far below that scale on a single host).

Assessment. Compatibility: all three can read the alerts the runbook
already specifies; the honest differentiators are burden and cost.
Recommendation is self-hosted (single-operator fit, no recurring vendor
commitment, no residency question raised) with the explicit rule that
monitoring infrastructure is separate from the execution host — otherwise
the watcher shares the fate of the watched. Managed observability stays a
cost-and-work trade decision, not a security claim. Unknowns: no vendor
pricing was verified; whichever option is chosen, "drill passed" (WP-8)
remains the acceptance criterion, not product maturity.

---

### Cross-cutting caveats

- Nothing in this document establishes that any infrastructure exists.
  FlakePilot's deployment posture is unchanged (assessment go/no-go is
  NO-GO for production untrusted execution).
- Every external claim was gathered on 2026-10-23; pricing, release
  versions, and CVE status must be re-checked at the time of an actual
  purchase or implementation, because the comparison above is a snapshot
  and hardware/runtime vendors move.
- Where a source is published by the vendor of the product it describes it
  is labelled vendor marketing and cannot substitute for the independent
  security review that Stage 10L+ work packages call for.
