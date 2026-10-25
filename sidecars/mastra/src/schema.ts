// The 200 body of FlakePilot's investigation routes (interface/http/investigations.py
// `_body`). Everything in it is already advisory: verdicts come from FlakePilot's
// deterministic rule set, and model output arrives as counts only.

import { z } from "zod";

const uuid = z.uuid();
const timestamp = z.iso.datetime({ offset: true });
const code = z.string().regex(/^[a-z][a-z0-9_]{0,39}$/);

export const InvestigationSchema = z.object({
  org_id: uuid,
  repo_id: uuid,
  run_id: uuid,
  stopping: z.enum(["complete", "not_found", "unauthorized"]),
  stages: z
    .array(
      z.object({
        name: z.enum(["collect", "research", "browse", "hypothesize", "prioritize"]),
        status: z.enum(["produced", "skipped", "failed"]),
        reason: code.nullable(),
      }),
    )
    .max(10),
  sources: z
    .array(z.object({ url: z.url(), title: z.string().max(1000), retrieved_at: timestamp }))
    .max(20),
  pages: z
    .array(
      z.object({
        url: z.url(),
        sha256: z.string().regex(/^[0-9a-f]{64}$/),
        size: z.number().int().nonnegative(),
        retrieved_at: timestamp,
      }),
    )
    .max(20),
  assist: z
    .object({
      request_id: z.string().max(100),
      outcome: code,
      reason: code.nullable(),
      linked: z.number().int().nonnegative(),
      unverified: z.number().int().nonnegative(),
      contradicted: z.number().int().nonnegative(),
    })
    .nullable(),
  prioritized: z
    .array(
      z.object({
        rank: z.number().int().positive(),
        test_id: uuid,
        flaky_runs: z.number().int().nonnegative(),
        failed_attempts: z.number().int().nonnegative(),
      }),
    )
    .max(100),
  brief: z.string().max(10_000),
  report_url: z.url(),
});

export type Investigation = z.infer<typeof InvestigationSchema>;

export const ReportSchema = z.object({
  advisory: z.literal(true),
  authority: z.literal("flakepilot"),
  org_id: uuid,
  repo_id: uuid,
  run_id: uuid,
  stopping: z.string(),
  stages: z.array(z.object({ name: z.string(), status: z.string(), reason: z.string().nullable() })),
  hypothesis: z.object({
    stage: z.string(),
    reason: z.string().nullable(),
    linked: z.number(),
    unverified: z.number(),
    contradicted: z.number(),
  }),
  research: z.object({ sources: z.number(), hosts: z.array(z.string()), pages: z.number() }),
  top_tests: z.array(
    z.object({ rank: z.number(), test_id: z.string(), flaky_runs: z.number(), failed_attempts: z.number() }),
  ),
  headline: z.string(),
  report_url: z.string(),
});

export type Report = z.infer<typeof ReportSchema>;
