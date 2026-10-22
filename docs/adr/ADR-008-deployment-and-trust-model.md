# ADR-008: Initial deployment model, execution trust model, and sandbox class

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20: deployment model (OQ-2), trust model (OQ-3), sandbox class (FQ-2), single-node placement (FQ-3), and more than one org per deployment (FQ-4). No deployment files are created by this ADR |
| Related | [ADR-001](ADR-001-modular-monolith.md), [ADR-004](ADR-004-control-plane-and-execution-plane.md), [ADR-005](ADR-005-system-of-record-database.md), [ADR-009](ADR-009-initial-provider-adapters.md), [SYSTEM-ARCHITECTURE §3, §4, §7](../architecture/SYSTEM-ARCHITECTURE.md#3-control-plane-vs-execution-plane), [PRODUCT-SCOPE §4, A5](../product/PRODUCT-SCOPE.md) |

## Context

Constraints already in place:
- ADR-004 separates the trusted control plane from the untrusted execution plane. Repo code, test code, and AI-written patches always run in ephemeral sandboxes that meet the sandbox-port guarantees (SYSTEM-ARCHITECTURE §4).
- Authorization and tenant scoping are enforced in the application layer and the data-access guard (SYSTEM-ARCHITECTURE §7).
- The product must be self-hostable and multi-org capable (PRODUCT-SCOPE A5).
- The sandbox technology was deferred until the hosting and trust questions were answered (OQ-5).

Owner answer to OQ-2 (verbatim):

> "Initial deployment: self-hosted, using documented Docker-based deployment and configuration. Architecture: preserve a clean separation between control-plane services and isolated execution workers, regardless of deployment mode. Tenant isolation: enforce authorization and tenant-scoped data access even in self-hosted deployments. Self-hosting does not eliminate security requirements. Hosted service: a future deployment option, not a requirement for the initial release. Portability: avoid hard-coded assumptions about a single cloud provider, managed database, or hosting vendor."

Owner answer to OQ-3: **one trusted org per deployment**.

## Decision

**Binding (owner-confirmed):**
1. **v1 is self-hosted**, deployed with documented Docker-based deployment and configuration. Writing those deployment artifacts belongs to a later stage. A hosted service is a future option, not a v1 requirement.
2. **Control plane and execution workers are separate deployable units in every deployment mode.** The control-plane processes (request-serving and workers, ADR-001) never run sandbox workloads. Sandboxes never get control-plane credentials or a network route to the control plane (ADR-004). Execution workers get no database or secret-store access.
3. **Tenant isolation and authorization are fully enforced in self-hosted deployments too**: IAM decisions, grants, D2, the tenant-scoped data-access guard (SYSTEM-ARCHITECTURE §7; ADR-005 decision 5).
4. **Portability.**
   - No hard-coded assumption about a single cloud provider, managed database, or hosting vendor.
   - Only core PostgreSQL features are used (ADR-005).
   - Storage uses the S3-compatible API or the local filesystem, and telemetry uses a vendor-neutral protocol ([ADR-009](ADR-009-initial-provider-adapters.md)).
   - Every endpoint and credential reference comes from configuration.
5. **Trust model for v1: one trusted org per deployment.** Precisely:
   - **Unchanged:**
     - Repo and test code, dependencies, and AI-written patches remain **untrusted**. All of ADR-004 applies: per-run ephemeral sandboxes, no credentials in sandboxes, egress denied by default and enforced outside the sandbox, no route to the control plane or metadata endpoints, artifacts treated as untrusted, outcomes recomputed in the control plane.
     - Authorization and tenant-scoped data access are enforced as in item 3.
     - The sandbox-escape security metric stays at 0 (PRODUCT-SCOPE §10).
   - **Assumed for v1:** the threat model doesn't include *mutually hostile tenants sharing sandbox hosts*. Sandbox hosts in one deployment run only that deployment's work.
   - **More than one org per deployment is allowed** (FQ-4). This is consistent with PRODUCT-SCOPE A5 and the org and role model (§3). Orgs in one deployment are assumed mutually trusted **for sandbox-host co-tenancy only**. Tenant-scoped authorization and data isolation between them are fully enforced (item 3). Each org keeps its own admins and grants, and the platform operator's metadata-only and break-glass rules still apply (PRODUCT-SCOPE §3.4). Deployment documentation must state this co-tenancy assumption to operators.
6. **If a hosted multi-tenant service is added**, a new ADR must supersede item 5 before launch. It must at least decide:
   - **microVM-class sandbox isolation**, a required upgrade (owner-confirmed, FQ-2), or dedicated execution hosts per tenant on top of it.
   - Per-tenant network segmentation of execution.
   - Physical tenant isolation for data (deferred in SYSTEM-ARCHITECTURE §10).
   - The platform operator as a separate party, which makes break-glass and the metadata-only rule central (PRODUCT-SCOPE §3.4).
   - Noisy-neighbour quotas.
   - Whether the default secret store and its key management still fit ([ADR-009](ADR-009-initial-provider-adapters.md)).

**Sandbox class (binding for v1, FQ-2):**
- **Hardened, ephemeral containers** on a container runtime dedicated to execution workers. The INT sandbox adapter drives it through a narrowly scoped, authenticated control interface. The control plane starts every interaction (ADR-004 decision 2).
- **Hardening baseline:**
  - Runs as non-root, never privileged, with capabilities dropped and the default syscall filter on.
  - Read-only root filesystem where the test allows.
  - No host mounts, and no container-runtime socket inside a sandbox.
  - CPU, memory, disk, and process limits.
  - A per-run network with egress denied by default, enforced by the host (an allowlist through an egress proxy or firewall), and metadata endpoints blocked.
  - A TTL-based reaper for self-expiry.
  - A fresh container per run, never reused.
- **Placement (FQ-3):**
  - **Production installs require a dedicated execution host.**
  - Running sandboxes on the control-plane host is allowed **only in evaluation installs**. Evaluation mode is an **explicit, documented opt-in and never the default**; otherwise a production install could silently break the production rule.
  - Even in evaluation mode, sandboxes are still hardened containers driven through the sandbox adapter. They are separate units from the control-plane processes (item 2) and have no control-plane credentials or network route. Only the host is shared.
- **Residual risk (stated, not dismissed):** containers share the host kernel, so a kernel or runtime escape by malicious repo code would compromise the execution host. In production the dedicated execution host limits that blast radius to the execution plane, and that host holds no control-plane credentials. In evaluation installs, an escape could reach control-plane processes and data on the shared host. That is why co-location is evaluation-only.
- The adapter must pass the sandbox-port conformance suite (ADR-003). If it can't meet a guarantee, this choice is reopened.

**Implementation details for later design** (these don't need an owner decision): the exact syscall profile, the egress-proxy or firewall mechanism, TTL values, authentication for the execution runtime's control interface, and how evaluation mode is configured and shown to operators.

## Rationale

- The owner's answers fix the deployment and trust context. Hardened containers are the simplest class that runs on a self-hosted Docker-based deployment and can meet the port guarantees for one trusted org's untrusted code.
- Keeping the control plane and execution as separate units in every mode keeps ADR-004's boundary real, even in single-node installs.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| A stronger isolation class with its own kernel per sandbox (microVM-class) | A stronger kernel boundary, but more operational burden and host requirements for self-hosters. The owner made it the **required upgrade before a hosted multi-tenant service** (item 6) |
| Managed sandbox service | Contradicts the self-hosted-first and portability answers (OQ-2) |
| Running tests in control-plane containers | Violates ADR-004 |
| Assuming mutually hostile tenants in v1 | Not the owner's v1 threat model (OQ-3). Deferred to item 6 |

## Consequences

- **Positive:** a deployable, portable v1 with the full ADR-004 boundary. The isolation level is honest about what it protects against.
- **Negative:** production self-hosters must provide a dedicated execution host. Container isolation carries kernel-sharing risk. Evaluation installs give up host separation.
- **Follow-ups:** the implementation details above. The Docker-based deployment artifacts and docs come in a later stage.

## Stage 10 feasibility

No dedicated execution host was provisioned. There is no cloud account, no second machine, and no authority to create one. Paid infrastructure is out of scope. The control-plane process was not given a container runtime or a microVM monitor. `/dev/kvm` is not usable by this user, and using it here would still place the hypervisor next to control-plane secrets. The worker no longer selects the container client when execution mode is `evaluation`. The handoff type carries no database URL or GitHub key, and its only adapter refuses the call. The worker's runtime reaper reports `runtime_reaper_unavailable` and removes nothing but expired `fp-` directories. `LabelledContainerReaper` can remove an expired labelled container, and the worker does not call it. A developer laptop recorded container probes on 21 October 2026; that Docker Desktop VM is not a dedicated host and not a microVM. A GitHub-hosted runner recorded a second set. On that runner, SIGKILL of the client left the container running. The disk probe issued one `write` of 2 MB, and that call can return a short count without an error, so the run did not prove the size option was ignored. A later run on the developer laptop, still not this deployment, stored exactly the tmpfs cap for `size=1m`, `size=1024k`, `size=1048576`, and `size=64m`, and failed the next write. The runner has not repeated that probe. Stage 10B leaves `LabelledContainerReaper` off the worker. Stage 10D still does not provision a host. The reaper's deadline is a host ledger written before start. A container label cannot extend or shorten it. The worker still does not call the reaper, and no runtime was started for that rule. Stage 10E serializes ledger updates with a sibling flock and treats a missing or empty ledger as untrusted. A Mac probe is in the tree for a developer laptop. Two 22 October 2026 logs from that laptop, Docker Desktop's shared LinuxKit VM, are recorded. The later log measured the container limits. Neither sets isolation checks. The probe was not executed on this control plane. Stage 10F leaves the worker on a refusing reaper and a refusing host handoff. Artifact encryption and retention stay unconfigured. Live GitHub App authentication stays unverified. Stage 10G documents the host contract and a simulated reference adapter. A lower isolation observation does not satisfy a higher level. An AES-GCM sealer can persist ciphertext in tests; the app still uses the plaintext store, and there is no key manager. Retention still selects nothing. No host was provisioned. The manual harness is what runs it, and only after the wall deadline recorded before start. A worker lease is not a reason to remove a container. Neither record sets isolation checks. Local artifacts remain plaintext. Untrusted execution stays disabled. See [EXECUTION](../operations/EXECUTION.md).

## Stage 9 feasibility

Checked again on the control-plane host used for this stage: no `docker`, `podman`, `firecracker`, or `qemu-system-x86_64`, and no runtime socket. `/dev/kvm` is not usable by this user. No runtime was installed, and no isolation suite was executed. The production placement rule in this ADR is unmet. Untrusted execution stays disabled. The operator route records `execution_closed` or, in evaluation, `source_unverified`, and starts nothing. Job leases are renewed while a handler runs. A committed cancel is read again before artifacts are stored. Artifact bytes require the owning org and a matching hash. Local artifact files are plaintext and require an encrypted volume in production. There is no container reaper and no patch proposal. See [EXECUTION](../operations/EXECUTION.md).

## Stage 8 feasibility

No dedicated execution host, container runtime, or runtime socket was available where Stage 8 started, and none was added. The production placement rule in this ADR is unmet. Untrusted execution stays disabled. The operator route records `execution_closed` without starting a runtime. Installation tokens are `contents:read` and stay in the control plane. There is no container reaper and no test that launches a container. Patch proposals are not implemented, because this deployment cannot produce evidence from a verified sandbox. See [EXECUTION](../operations/EXECUTION.md).

## Stage 7 implementation note

Evaluation mode is the explicit opt-in this ADR requires, and `closed` is the default. The deviations are concrete. This repository does not provision a dedicated execution host, does not install a container runtime, and does not run a TTL reaper. The job worker is a control-plane process with database access, and in evaluation mode that same process execs the runtime client. The container is not given the database URL, a token, or the control-plane environment. The argument list does not set a seccomp profile. Kernel enforcement of `--network=none` and the other flags is documented only: CI did not start a container. A hosted multi-tenant service still needs the microVM-class decision in item 6. See [EXECUTION](../operations/EXECUTION.md).
