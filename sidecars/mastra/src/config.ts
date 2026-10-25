// Configuration from the environment. Values are validated and never echoed:
// every error names the variable, not its content.

export interface Config {
  baseUrl: string;
  auth0Domain: string;
  audience: string;
  clientId: string;
  clientSecret: string;
  tokenTimeoutMs: number;
  investigationTimeoutMs: number;
}

export class ConfigError extends Error {}

const HOSTNAME = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const PRINTABLE = /^[\x21-\x7e]{1,512}$/;
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name];
  if (!value) throw new ConfigError(`${name} is required`);
  return value;
}

function printable(env: NodeJS.ProcessEnv, name: string): string {
  const value = required(env, name);
  if (!PRINTABLE.test(value)) throw new ConfigError(`${name} is not a printable token`);
  return value;
}

function milliseconds(env: NodeJS.ProcessEnv, name: string, fallback: number): number {
  const raw = env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1_000 || value > 600_000) {
    throw new ConfigError(`${name} must be an integer from 1000 to 600000`);
  }
  return value;
}

/** https anywhere; plain http only to a loopback host (local development). */
export function baseUrlOk(raw: string): boolean {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (url.username || url.password || url.search || url.hash) return false;
  if (url.pathname !== "/" && url.pathname !== "") return false;
  return url.protocol === "https:" || (url.protocol === "http:" && LOOPBACK.has(url.hostname));
}

export function loadConfig(env: NodeJS.ProcessEnv): Config {
  const baseUrl = required(env, "FLAKEPILOT_BASE_URL");
  if (!baseUrlOk(baseUrl)) {
    throw new ConfigError(
      "FLAKEPILOT_BASE_URL must be an https origin (http only to a loopback host)",
    );
  }
  const auth0Domain = required(env, "FLAKEPILOT_AUTH0_DOMAIN");
  if (!HOSTNAME.test(auth0Domain)) {
    throw new ConfigError("FLAKEPILOT_AUTH0_DOMAIN must be a lowercase host name");
  }
  return {
    baseUrl: new URL(baseUrl).origin,
    auth0Domain,
    audience: printable(env, "FLAKEPILOT_AUTH0_AUDIENCE"),
    clientId: printable(env, "FLAKEPILOT_AGENT_AUTH0_CLIENT_ID"),
    clientSecret: printable(env, "FLAKEPILOT_AGENT_AUTH0_CLIENT_SECRET"),
    tokenTimeoutMs: milliseconds(env, "FLAKEPILOT_AGENT_TOKEN_TIMEOUT_MS", 10_000),
    investigationTimeoutMs: milliseconds(
      env,
      "FLAKEPILOT_AGENT_INVESTIGATION_TIMEOUT_MS",
      180_000,
    ),
  };
}
