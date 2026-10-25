// The Mastra workflow: obtain token -> investigate -> validate -> summarize.
//
// Mastra orchestrates; FlakePilot decides. The workflow holds no database
// access and no FlakePilot credential beyond one short-lived Auth0 token for
// its own client, which FlakePilot checks against an admin-made binding on
// every call. Budgets: one investigation per invocation (a fresh workflow per
// call, guarded), a timeout per outbound call, and no retries anywhere.
// The token never enters a step's input or output: it lives in a per-
// invocation holder and is dropped after its single use.

import { createStep, createWorkflow } from "@mastra/core/workflows";
import { z } from "zod";

import type { Config } from "./config.ts";
import { CallFailed, clientCredentialsToken, requestInvestigation, type Fetch } from "./http.ts";
import { InvestigationSchema, ReportSchema, type Investigation, type Report } from "./schema.ts";

const uuid = z.uuid();

export const InputSchema = z.object({ orgId: uuid, runId: uuid });
export type Input = z.infer<typeof InputSchema>;

export interface Dependencies {
  fetch?: Fetch;
  /** Overrides `https://<domain>/oauth/token`; tests point it at a local server. */
  tokenUrl?: string;
}

export function summarize(investigation: Investigation): Report {
  const hypothesize = investigation.stages.find((stage) => stage.name === "hypothesize");
  const hosts = [...new Set(investigation.sources.map((source) => new URL(source.url).hostname))].sort();
  const assist = investigation.assist;
  const top = investigation.prioritized.slice(0, 5);
  const produced = investigation.stages.filter((stage) => stage.status === "produced").length;
  const headline =
    `Investigation ${investigation.stopping}: ${produced}/${investigation.stages.length} stages produced; ` +
    `hypothesis ${hypothesize ? `${hypothesize.status}${hypothesize.reason ? ` (${hypothesize.reason})` : ""}` : "absent"}; ` +
    `${investigation.sources.length} research sources; ` +
    `${top.length ? `top test flaky in ${top[0]!.flaky_runs} run(s)` : "no prioritized tests"}.`;
  return ReportSchema.parse({
    advisory: true,
    authority: "flakepilot",
    org_id: investigation.org_id,
    repo_id: investigation.repo_id,
    run_id: investigation.run_id,
    stopping: investigation.stopping,
    stages: investigation.stages,
    hypothesis: {
      stage: hypothesize?.status ?? "absent",
      reason: hypothesize?.reason ?? null,
      linked: assist?.linked ?? 0,
      unverified: assist?.unverified ?? 0,
      contradicted: assist?.contradicted ?? 0,
    },
    research: { sources: investigation.sources.length, hosts, pages: investigation.pages.length },
    top_tests: top,
    headline,
    report_url: investigation.report_url,
  });
}

export function buildWorkflow(config: Config, deps: Dependencies = {}) {
  const fetchImpl = deps.fetch ?? fetch;
  const tokenUrl = deps.tokenUrl ?? `https://${config.auth0Domain}/oauth/token`;
  const held: { token: string | null; investigations: number } = { token: null, investigations: 0 };

  const obtainToken = createStep({
    id: "obtain-token",
    description: "One Auth0 client-credentials token for this agent's own client",
    inputSchema: InputSchema,
    outputSchema: InputSchema.extend({ tokenExpiresIn: z.number().nullable() }),
    retries: 0,
    execute: async ({ inputData }) => {
      const { token, expiresIn } = await clientCredentialsToken(fetchImpl, {
        tokenUrl,
        audience: config.audience,
        clientId: config.clientId,
        clientSecret: config.clientSecret,
        timeoutMs: config.tokenTimeoutMs,
      });
      held.token = token;
      return { ...inputData, tokenExpiresIn: expiresIn };
    },
  });

  const investigate = createStep({
    id: "investigate",
    description: "One call to FlakePilot's agent investigation route",
    inputSchema: InputSchema.extend({ tokenExpiresIn: z.number().nullable() }),
    outputSchema: z.object({ body: z.unknown() }),
    retries: 0,
    execute: async ({ inputData }) => {
      if (held.investigations >= 1) throw new CallFailed("budget_exhausted");
      const token = held.token;
      held.token = null;
      if (token === null) throw new CallFailed("no_token");
      held.investigations += 1;
      const body = await requestInvestigation(fetchImpl, {
        baseUrl: config.baseUrl,
        orgId: inputData.orgId,
        runId: inputData.runId,
        token,
        timeoutMs: config.investigationTimeoutMs,
      });
      return { body };
    },
  });

  const validate = createStep({
    id: "validate",
    description: "Check the answer against FlakePilot's response schema",
    inputSchema: z.object({ body: z.unknown() }),
    outputSchema: InvestigationSchema,
    retries: 0,
    execute: async ({ inputData }) => {
      const parsed = InvestigationSchema.safeParse(inputData.body);
      if (!parsed.success) throw new CallFailed("schema_invalid");
      return parsed.data;
    },
  });

  const report = createStep({
    id: "summarize",
    description: "Summarize the already-advisory result into a report object",
    inputSchema: InvestigationSchema,
    outputSchema: ReportSchema,
    retries: 0,
    execute: async ({ inputData }) => summarize(inputData),
  });

  return createWorkflow({
    id: "flakepilot-agent-investigation",
    description: "Ask FlakePilot to investigate one stored run, as a bound Auth0 agent",
    inputSchema: InputSchema,
    outputSchema: ReportSchema,
    retryConfig: { attempts: 0, delay: 0 },
  })
    .then(obtainToken)
    .then(investigate)
    .then(validate)
    .then(report)
    .commit();
}

export type Outcome = { ok: true; report: Report } | { ok: false; error: string };

/** One invocation: a fresh workflow, so the one-investigation budget is per call. */
export async function runInvestigation(config: Config, input: Input, deps: Dependencies = {}): Promise<Outcome> {
  const parsed = InputSchema.safeParse(input);
  if (!parsed.success) return { ok: false, error: "input_invalid" };
  const workflow = buildWorkflow(config, deps);
  const run = await workflow.createRun();
  const result = await run.start({ inputData: parsed.data });
  if (result.status === "success") return { ok: true, report: ReportSchema.parse(result.result) };
  if (result.status === "failed") {
    const error = result.error as unknown;
    const code =
      error instanceof CallFailed
        ? error.code
        : typeof (error as { message?: unknown })?.message === "string" &&
            /^[a-z][a-z0-9_]{0,59}$/.test((error as { message: string }).message)
          ? (error as { message: string }).message
          : "workflow_failed";
    return { ok: false, error: code };
  }
  return { ok: false, error: `workflow_${result.status}` };
}
