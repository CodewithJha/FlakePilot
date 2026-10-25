// The two outbound calls: one Auth0 client-credentials token, one FlakePilot
// investigation. Each has its own timeout and a response size cap, follows no
// redirects, and is never retried. Failures are short codes; no URL query,
// header, token, secret, or response text is ever put in an error.

export class CallFailed extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.code = code;
  }
}

export type Fetch = typeof fetch;

const MAX_BODY_BYTES = 1024 * 1024;

async function capped(response: Response): Promise<string> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new CallFailed("response_too_large");
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function exchange(
  fetchImpl: Fetch,
  url: string,
  init: RequestInit,
  timeoutMs: number,
  prefix: string,
): Promise<{ status: number; text: string }> {
  let response: Response;
  try {
    response = await fetchImpl(url, {
      ...init,
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    const name = error instanceof Error ? error.name : "";
    throw new CallFailed(
      name === "TimeoutError" || name === "AbortError" ? `${prefix}_timeout` : `${prefix}_unreachable`,
    );
  }
  try {
    return { status: response.status, text: await capped(response) };
  } catch (error) {
    if (error instanceof CallFailed) throw new CallFailed(`${prefix}_${error.code}`);
    const name = error instanceof Error ? error.name : "";
    throw new CallFailed(name === "TimeoutError" ? `${prefix}_timeout` : `${prefix}_unreachable`);
  }
}

export interface TokenRequest {
  tokenUrl: string;
  audience: string;
  clientId: string;
  clientSecret: string;
  timeoutMs: number;
}

export async function clientCredentialsToken(
  fetchImpl: Fetch,
  request: TokenRequest,
): Promise<{ token: string; expiresIn: number | null }> {
  const { status, text } = await exchange(
    fetchImpl,
    request.tokenUrl,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        grant_type: "client_credentials",
        client_id: request.clientId,
        client_secret: request.clientSecret,
        audience: request.audience,
      }),
    },
    request.timeoutMs,
    "token",
  );
  if (status !== 200) throw new CallFailed(`token_http_${status}`);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new CallFailed("token_bad_response");
  }
  const body = parsed as { access_token?: unknown; token_type?: unknown; expires_in?: unknown };
  if (typeof body.access_token !== "string" || !/^[\x21-\x7e]{1,8192}$/.test(body.access_token)) {
    throw new CallFailed("token_bad_response");
  }
  if (typeof body.token_type === "string" && body.token_type.toLowerCase() !== "bearer") {
    throw new CallFailed("token_bad_response");
  }
  return {
    token: body.access_token,
    expiresIn: typeof body.expires_in === "number" ? body.expires_in : null,
  };
}

export interface InvestigationRequest {
  baseUrl: string;
  orgId: string;
  runId: string;
  token: string;
  timeoutMs: number;
}

export async function requestInvestigation(
  fetchImpl: Fetch,
  request: InvestigationRequest,
): Promise<unknown> {
  const url = new URL(
    `/agent/orgs/${encodeURIComponent(request.orgId)}/runs/${encodeURIComponent(request.runId)}/investigation`,
    request.baseUrl,
  );
  const { status, text } = await exchange(
    fetchImpl,
    url.href,
    {
      method: "POST",
      headers: { authorization: `Bearer ${request.token}`, accept: "application/json" },
    },
    request.timeoutMs,
    "flakepilot",
  );
  if (status !== 200) throw new CallFailed(`flakepilot_http_${status}`);
  try {
    return JSON.parse(text);
  } catch {
    throw new CallFailed("flakepilot_bad_response");
  }
}
