# Entire and external provenance

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 7
and sponsor goal 15 (Entire). Read-only evaluation. **No adapter was built**,
because Entire offers no runtime API for publishing an external attestation of
a FlakePilot investigation. Nothing is installed in this repository.

## What Entire is (verified 2026-10-24)

- A developer platform from Entire (founded by former GitHub CEO Thomas
  Dohmke; launched February 2026), built for human–agent software development.
- Its core primitive is the **checkpoint**. The open-source Entire CLI
  installs agent hooks (Claude Code, Codex, Cursor, Copilot CLI, and others)
  and Git hooks. On a commit, it links the captured agent session
  (prompts, transcript, tool calls, changed files, token usage, line
  attribution) to the commit by adding an **`Entire-Checkpoint:` trailer to the
  commit message**. It stores the checkpoint data in the repository under its
  own ref or the `entire/checkpoints/v1` branch, and pushes it with the code.
- entire.io adds Git mirroring/hosting, session browsing, search, and review.
  Its HTTP API (`https://aws-us-east-2.api.entire.io/api/v1`, bearer token) is
  reached through `entire api`. Entire's own CLI reference warns that these
  **internal endpoints may change without notice**. The sessions/checkpoints
  section of the published OpenAPI is read-oriented: listing checkpoints,
  sessions, and transcripts. Checkpoints are written by the CLI through Git,
  not posted by third-party services.

Sources: <https://entire.io/news/former-github-ceo-thomas-dohmke-raises-60-million-seed-round>,
<https://entire.io/blog/hello-entire-world/>,
<https://docs.entire.io/guides/sessions/overview>,
<https://docs.entire.io/guides/checkpoints/capture-checkpoints.md>,
<https://docs.entire.io/guides/configuration/privacy-and-redaction.md>,
<https://docs.entire.io/cli-reference/api.md>,
<https://aws-us-east-2.api.entire.io/api/v1/openapi.json?section=sessions>.

## Does it add runtime provenance beyond `diag.investigation.recorded`?

No. FlakePilot's runtime provenance is one `diag.investigation.recorded` event
per investigation, appended to `diag.outbox` in the same transaction
discipline as every diagnosis event. The event carries IDs, enums, and counts
only, and is integration-tested over real tables (goal 15). Entire records
something else: how *code* was produced by coding agents. It has no documented,
stable endpoint where a service could append "investigation X ran with these
stage counts". Building an adapter against the internal API would mean:

- depending on endpoints the vendor says may change without notice;
- inventing a record shape Entire does not define (there is no attestation
  object for third-party runtime events);
- adding an outbound data flow and a credential for no verifiable gain over
  the append-only outbox.

So the runtime side stays as it is. Goal 15's "external attestation" step
remains **Planned** until Entire (or another attestation service) publishes a
stable write API with a defined record shape.

## Where Entire does apply: the development workflow

Entire fits how this repository is *built* (AI-agent-assisted commits), not
what the product does at runtime. Adopting it would record which agent session
produced each commit. But it conflicts with standing conventions, so it
**needs an explicit owner decision before anyone runs `entire enable` here**:

| Entire default | AGENTS.md convention | Conflict |
|---|---|---|
| Adds an `Entire-Checkpoint:` trailer to linked commits | "no co-author or tool-attribution trailers" | Direct |
| Signs checkpoint commits with the user's Git key (`sign_checkpoint_commits: true`) | "Unsigned commits" | Direct, for the checkpoint ref; can be set `false` |
| Pushes transcripts, prompts, and tool output with the repository | Secrets and credentials must never enter commits | Risk: redaction is pattern-based and best-effort (Betterleaks by default; PII redaction off by default; images are not scanned) |
| Installs Git and agent hooks | Gates and commit identity are checked by hand before every commit | Adds hook behaviour to every commit |

If the owner wants it anyway, the least invasive shape is:

1. Amend AGENTS.md first to allow the `Entire-Checkpoint` trailer, or decline
   linking (`commit_linking` prompt answered "no") so no trailer is written.
2. Keep checkpoints local
   (<https://docs.entire.io/guides/checkpoints/keep-checkpoints-local.md>) or
   store them in a separate private repository
   (<https://docs.entire.io/guides/checkpoints/store-checkpoints-in-another-repo.md>),
   so transcripts never reach the public `origin`.
3. Set `sign_checkpoint_commits: false`, enable PII redaction, and set
   `telemetry: false` in `.entire/settings.json`.

None of this is done. It is recorded so the decision can be made with the
conflicts in view.

## What the owner must supply (only if adopting for development)

- A decision amending AGENTS.md's trailer and signing rules, or confirmation
  that linking stays off.
- An entire.io account and GitHub App authorization (only for mirroring or the
  web UI; local checkpoints need neither).
- Cost: the CLI is open source; entire.io platform pricing was not verified
  here.

## Status

Goal 15: **Integration-tested** (local `diag.investigation.recorded` event
flow, unchanged). External Entire attestation: **BLOCKED**, because no stable
third-party write API exists. Development-workflow adoption: **owner decision
pending**, because it conflicts with AGENTS.md.
