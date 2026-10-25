// CLI: run the workflow once for one org/run and print one JSON line.
//   node src/main.ts --org <uuid> --run <uuid>
// Exit 0 with {"ok":true,"report":{...}}; exit 1 with {"ok":false,"error":"<code>"}.
// Nothing else is printed: no token, secret, or response text.

import { parseArgs } from "node:util";

// Mastra's own product telemetry stays off: this process talks to Auth0 and
// FlakePilot only. Set before Mastra is loaded.
process.env.MASTRA_TELEMETRY_DISABLED = "1";

const { loadConfig, ConfigError } = await import("./config.ts");
const { runInvestigation } = await import("./workflow.ts");

function emit(outcome: object, code: number): never {
  process.stdout.write(`${JSON.stringify(outcome)}\n`);
  process.exit(code);
}

let values: { org?: string; run?: string };
try {
  ({ values } = parseArgs({ options: { org: { type: "string" }, run: { type: "string" } } }));
} catch {
  emit({ ok: false, error: "usage: --org <uuid> --run <uuid>" }, 2);
}
if (!values.org || !values.run) emit({ ok: false, error: "usage: --org <uuid> --run <uuid>" }, 2);

let config;
try {
  config = loadConfig(process.env);
} catch (error) {
  emit({ ok: false, error: error instanceof ConfigError ? error.message : "config_invalid" }, 2);
}

const outcome = await runInvestigation(config, { orgId: values.org, runId: values.run });
emit(outcome, outcome.ok ? 0 : 1);
