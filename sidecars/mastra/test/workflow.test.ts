// The workflow against a mocked Auth0 token endpoint and a mocked FlakePilot,
// both on one loopback HTTP server. No network beyond 127.0.0.1.

process.env.MASTRA_TELEMETRY_DISABLED = "1";

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, beforeEach, test } from "node:test";

const { loadConfig, ConfigError } = await import("../src/config.ts");
const { runInvestigation } = await import("../src/workflow.ts");

const ORG = "11111111-2222-4333-8444-555555555555";
const RUN = "cccccccc-dddd-4eee-8fff-000000000001";
const REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const SECRET = "s3cret-client-secret-value";
const TOKEN = "eyJ.test-access-token.sig";

const BODY = {
  org_id: ORG,
  repo_id: REPO,
  run_id: RUN,
  stopping: "complete",
  stages: [
    { name: "collect", status: "produced", reason: null },
    { name: "research", status: "produced", reason: null },
    { name: "browse", status: "skipped", reason: "provider_absent" },
    { name: "hypothesize", status: "produced", reason: null },
    { name: "prioritize", status: "produced", reason: null },
  ],
  sources: [
    { url: "https://docs.pytest.org/en/stable/flaky.html", title: "Flaky tests", retrieved_at: "2026-10-25T06:00:00+00:00" },
  ],
  pages: [],
  assist: { request_id: "r-1", outcome: "produced", reason: null, linked: 1, unverified: 0, contradicted: 0 },
  prioritized: [
    { rank: 1, test_id: "dddddddd-eeee-4fff-8aaa-000000000003", flaky_runs: 1, failed_attempts: 1 },
  ],
  brief: "FlakePilot investigation brief. The full report is linked.",
  report_url: `http://127.0.0.1/orgs/${ORG}/runs/${RUN}`,
};

type Handler = (req: IncomingMessage, body: string, res: ServerResponse) => void;

const calls: { path: string; authorization?: string; body: string }[] = [];
let tokenHandler: Handler;
let flakepilotHandler: Handler;
let base = "";

const server = createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", () => {
    calls.push({ path: req.url ?? "", authorization: req.headers.authorization, body });
    if (req.url === "/oauth/token") tokenHandler(req, body, res);
    else flakepilotHandler(req, body, res);
  });
});

function json(res: ServerResponse, status: number, value: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(value));
}

before(async () => {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => {
  server.closeAllConnections();
  server.close();
});
beforeEach(() => {
  calls.length = 0;
  tokenHandler = (_req, body, res) => {
    const sent = JSON.parse(body);
    assert.equal(sent.grant_type, "client_credentials");
    assert.equal(sent.audience, "https://api.flakepilot.example");
    json(res, 200, { access_token: TOKEN, token_type: "Bearer", expires_in: 600 });
  };
  flakepilotHandler = (req, _body, res) => {
    assert.equal(req.method, "POST");
    assert.equal(req.url, `/agent/orgs/${ORG}/runs/${RUN}/investigation`);
    if (req.headers.authorization !== `Bearer ${TOKEN}`) return json(res, 401, {});
    json(res, 200, BODY);
  };
});

function config(overrides: Record<string, string> = {}) {
  return loadConfig({
    FLAKEPILOT_BASE_URL: base,
    FLAKEPILOT_AUTH0_DOMAIN: "tenant.us.auth0.com",
    FLAKEPILOT_AUTH0_AUDIENCE: "https://api.flakepilot.example",
    FLAKEPILOT_AGENT_AUTH0_CLIENT_ID: "client-id-123",
    FLAKEPILOT_AGENT_AUTH0_CLIENT_SECRET: SECRET,
    ...overrides,
  });
}

const run = (overrides: Record<string, string> = {}) =>
  runInvestigation(config(overrides), { orgId: ORG, runId: RUN }, { tokenUrl: `${base}/oauth/token` });

test("one token, one investigation, a validated advisory report", async () => {
  const outcome = await run();
  assert.equal(outcome.ok, true);
  if (!outcome.ok) return;
  const { report } = outcome;
  assert.equal(report.advisory, true);
  assert.equal(report.authority, "flakepilot");
  assert.equal(report.run_id, RUN);
  assert.equal(report.hypothesis.stage, "produced");
  assert.equal(report.hypothesis.linked, 1);
  assert.deepEqual(report.research, { sources: 1, hosts: ["docs.pytest.org"], pages: 0 });
  assert.equal(report.top_tests.length, 1);
  assert.match(report.headline, /^Investigation complete: 4\/5 stages produced/);
  assert.deepEqual(
    calls.map((call) => call.path),
    ["/oauth/token", `/agent/orgs/${ORG}/runs/${RUN}/investigation`],
  );
  const printed = JSON.stringify(outcome);
  assert.ok(!printed.includes(TOKEN) && !printed.includes(SECRET));
});

test("a FlakePilot refusal is final: one call, no retry, a short code", async () => {
  for (const status of [401, 403, 404, 503]) {
    calls.length = 0;
    flakepilotHandler = (_req, _body, res) => json(res, status, { error: { code: "x", message: "detail" } });
    const outcome = await run();
    assert.deepEqual(outcome, { ok: false, error: `flakepilot_http_${status}` });
    assert.equal(calls.filter((call) => call.path.startsWith("/agent/")).length, 1);
  }
});

test("a token refusal stops before FlakePilot is called", async () => {
  tokenHandler = (_req, _body, res) => json(res, 401, { error: "access_denied", error_description: SECRET });
  const outcome = await run();
  assert.deepEqual(outcome, { ok: false, error: "token_http_401" });
  assert.deepEqual(calls.map((call) => call.path), ["/oauth/token"]);
});

test("an answer outside the schema is rejected", async () => {
  flakepilotHandler = (_req, _body, res) => json(res, 200, { ...BODY, run_id: "not-a-uuid" });
  assert.deepEqual(await run(), { ok: false, error: "schema_invalid" });
});

test("a hanging FlakePilot hits the timeout, once", async () => {
  flakepilotHandler = () => undefined; // never answers
  const outcome = await run({ FLAKEPILOT_AGENT_INVESTIGATION_TIMEOUT_MS: "1000" });
  assert.deepEqual(outcome, { ok: false, error: "flakepilot_timeout" });
  assert.equal(calls.filter((call) => call.path.startsWith("/agent/")).length, 1);
});

test("invalid input is refused before any call", async () => {
  const outcome = await runInvestigation(config(), { orgId: "x", runId: RUN }, { tokenUrl: `${base}/oauth/token` });
  assert.deepEqual(outcome, { ok: false, error: "input_invalid" });
  assert.equal(calls.length, 0);
});

test("configuration refuses plain http off loopback and never echoes values", () => {
  assert.throws(() => config({ FLAKEPILOT_BASE_URL: "http://flakepilot.example" }), ConfigError);
  assert.throws(() => config({ FLAKEPILOT_BASE_URL: "https://u:p@flakepilot.example" }), ConfigError);
  try {
    config({ FLAKEPILOT_AGENT_AUTH0_CLIENT_SECRET: "has space" });
    assert.fail("expected a refusal");
  } catch (error) {
    assert.ok(error instanceof ConfigError);
    assert.ok(!error.message.includes("has space"));
  }
  assert.equal(config({ FLAKEPILOT_BASE_URL: "https://fp.example" }).baseUrl, "https://fp.example");
});

test("only Apache-2.0 Mastra code is imported (no ee/ paths)", () => {
  const dir = new URL("../src/", import.meta.url);
  for (const file of readdirSync(dir)) {
    const text = readFileSync(new URL(file, dir), "utf8");
    for (const match of text.matchAll(/from\s+"(@mastra\/[^"]+)"/g)) {
      assert.equal(match[1], "@mastra/core/workflows", `${file} imports ${match[1]}`);
    }
  }
});
