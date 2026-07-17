/**
 * eval-proxy worker.ts
 *
 * A live-demo evaluation backend for the content_quality study. It holds the
 * privileged POE_API_KEY, RESEND_API_KEY, and TURNSTILE_SECRET_KEY server-side
 * so the static frontend can stay fully static, and it bounds spend with
 * layered guards backed by a D1 database.
 *
 * This is SEPARATE infrastructure from the Python study pipeline. It shares no
 * state with it. All study knowledge (rubric, per-model prompts, weights,
 * threshold, prices, caps) lives in rubric-config.ts.
 *
 * Endpoints:
 *   POST /request-access   mint and email an access token (Turnstile gated)
 *   POST /evaluate         judge a passage with one chosen model (Turnstile gated)
 *   GET  /health           liveness check
 *
 * Checks on /evaluate. The cheap, non-consuming checks run first, then the
 * per-(model, content) cache is consulted, and ONLY a cache miss goes on to the
 * consuming guards and the Poe call. This means a repeat of the exact same
 * passage with the same model is always returned for free and never counts
 * against any limit.
 *
 * Before the cache lookup (order preserved):
 *   1. valid Turnstile token
 *   2. access token exists and is not revoked
 *   3. input validation: known model, non-empty content within MAX_INPUT_CHARS
 *
 * Cache lookup by (model, content_hash):
 *   - HIT: return the stored result (cached: true) with zero cost and NO Poe
 *     call. No per-token quota is spent, no per-IP slot, no monthly cost, and no
 *     global slot. The request is still logged (cached = 1, cost 0).
 *
 * Only on a cache MISS, the consuming guards run in order, then Poe is called:
 *   4. per-token daily quota not exceeded
 *   5. per-IP daily call cap not exceeded (deters token or email cycling)
 *   6. per-user monthly cost cap not reached (friendly "we" response if it is)
 *   7. hard global daily call cap not reached (reserved before the call)
 * The fresh result is then logged (cached = 0, real cost).
 *
 * Secrets and config are read from the Worker env (see Env). Nothing secret is
 * hardcoded. See README.md for how to set each one.
 */

import {
  MODELS,
  type ModelName,
  DIMENSIONS,
  ALL_FIELDS,
  SCALE_MIN,
  SCALE_MAX,
  compositeScore,
  GOOD_THRESHOLD,
  TEMPERATURE,
  MAX_OUTPUT_TOKENS,
  REPAIR_MODEL,
  buildSystemPrompt,
  buildUserPrompt,
  jsonInstruction,
  REPAIR_SYSTEM,
  costUsd,
  estimateTokens,
  LIMIT_DEFAULTS,
  SUPPORT_URL,
  CAPPED_MESSAGE,
} from "./rubric-config.ts";

export interface Env {
  // Secrets (set with `wrangler secret put`).
  POE_API_KEY: string;
  RESEND_API_KEY: string;
  TURNSTILE_SECRET_KEY: string;

  // Vars (set in wrangler.toml [vars] or with --var).
  FROM_ADDR: string; // e.g. "Content Quality Demo <demo@yourdomain.com>"
  SITE_NAME: string;
  ALLOWED_ORIGINS?: string; // comma separated, empty means allow any origin
  SUPPORT_URL?: string; // overrides rubric-config SUPPORT_URL
  TURNSTILE_SITE_KEY?: string; // public, used by the frontend only

  // Optional numeric limit overrides (strings from env, parsed at read time).
  MAX_INPUT_CHARS?: string;
  PER_TOKEN_DAILY_QUOTA?: string;
  GLOBAL_DAILY_CALL_CAP?: string;
  EVAL_IP_DAILY_CAP?: string;
  MONTHLY_COST_CAP_USD?: string;
  IP_DAILY_REQUEST_CAP?: string;
  EMAIL_COOLDOWN_HOURS?: string;
  DEFAULT_TOKEN_QUOTA_PER_DAY?: string;

  // Optional endpoint base overrides. Defaults point at the real services.
  // Overridden in local validation to point at a stub so no network is hit.
  TURNSTILE_VERIFY_URL?: string;
  RESEND_API_BASE?: string;
  POE_BASE_URL?: string;

  // Bindings.
  EVAL_DB: D1Database;

  [key: string]: unknown;
}

const DEFAULT_TURNSTILE_VERIFY_URL =
  "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const DEFAULT_RESEND_API_BASE = "https://api.resend.com";
const DEFAULT_POE_BASE_URL = "https://api.poe.com/v1";

const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;

// A short list of obviously disposable domains. Optional guard, best effort.
const DISPOSABLE_DOMAINS = new Set([
  "mailinator.com",
  "guerrillamail.com",
  "10minutemail.com",
  "tempmail.com",
  "trashmail.com",
  "yopmail.com",
  "throwawaymail.com",
  "getnada.com",
  "sharklasers.com",
  "maildrop.cc",
]);

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function readLimits(env: Env) {
  const num = (v: string | undefined, d: number): number => {
    if (v === undefined || v === null || v === "") return d;
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
  };
  return {
    MAX_INPUT_CHARS: num(env.MAX_INPUT_CHARS, LIMIT_DEFAULTS.MAX_INPUT_CHARS),
    PER_TOKEN_DAILY_QUOTA: num(
      env.PER_TOKEN_DAILY_QUOTA,
      LIMIT_DEFAULTS.PER_TOKEN_DAILY_QUOTA,
    ),
    GLOBAL_DAILY_CALL_CAP: num(
      env.GLOBAL_DAILY_CALL_CAP,
      LIMIT_DEFAULTS.GLOBAL_DAILY_CALL_CAP,
    ),
    EVAL_IP_DAILY_CAP: num(
      env.EVAL_IP_DAILY_CAP,
      LIMIT_DEFAULTS.EVAL_IP_DAILY_CAP,
    ),
    MONTHLY_COST_CAP_USD: num(
      env.MONTHLY_COST_CAP_USD,
      LIMIT_DEFAULTS.MONTHLY_COST_CAP_USD,
    ),
    IP_DAILY_REQUEST_CAP: num(
      env.IP_DAILY_REQUEST_CAP,
      LIMIT_DEFAULTS.IP_DAILY_REQUEST_CAP,
    ),
    EMAIL_COOLDOWN_HOURS: num(
      env.EMAIL_COOLDOWN_HOURS,
      LIMIT_DEFAULTS.EMAIL_COOLDOWN_HOURS,
    ),
    DEFAULT_TOKEN_QUOTA_PER_DAY: num(
      env.DEFAULT_TOKEN_QUOTA_PER_DAY,
      LIMIT_DEFAULTS.DEFAULT_TOKEN_QUOTA_PER_DAY,
    ),
  };
}

function supportUrl(env: Env): string {
  return env.SUPPORT_URL && env.SUPPORT_URL.length > 0 ? env.SUPPORT_URL : SUPPORT_URL;
}

function allowedOrigin(req: Request, env: Env): string {
  const allowed = (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const origin = req.headers.get("origin") || "";
  if (allowed.length === 0) return origin || "*";
  if (origin && allowed.includes(origin)) return origin;
  // Not allowed. Return the first configured origin so the browser blocks it.
  return allowed[0];
}

function corsHeaders(req: Request, env: Env): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": allowedOrigin(req, env),
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function json(
  req: Request,
  env: Env,
  body: unknown,
  status = 200,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders(req, env) },
  });
}

function originAllowed(req: Request, env: Env): boolean {
  const allowed = (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (allowed.length === 0) return true; // not configured, do not block
  const origin = req.headers.get("origin") || "";
  const referer = req.headers.get("referer") || "";
  return allowed.some((a) => origin === a || referer.startsWith(a));
}

function generateToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function tokenPrefix(t: string): string {
  return t.slice(0, 8);
}

function clientIp(req: Request): string {
  return req.headers.get("cf-connecting-ip") || "0.0.0.0";
}

// UTC calendar helpers.
function todayUtc(d = new Date()): string {
  return d.toISOString().slice(0, 10); // YYYY-MM-DD
}
function monthUtc(d = new Date()): string {
  return d.toISOString().slice(0, 7); // YYYY-MM
}

// SHA-256 hex of a string, using Web Crypto (available in the Workers runtime).
// Used as the content hash that keys the private submission cache.
async function sha256Hex(s: string): Promise<string> {
  const data = new TextEncoder().encode(s);
  const buf = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function randomId(): string {
  // crypto.randomUUID is available in the Workers runtime.
  try {
    return crypto.randomUUID();
  } catch {
    return generateToken();
  }
}

// ---------------------------------------------------------------------------
// External services. All go through fetch so a stubbed global fetch (local
// validation) exercises the real worker code without any network or cost.
// ---------------------------------------------------------------------------

async function verifyTurnstile(
  env: Env,
  token: string,
  ip: string,
): Promise<boolean> {
  if (!token) return false;
  if (!env.TURNSTILE_SECRET_KEY) return false;
  const url = env.TURNSTILE_VERIFY_URL || DEFAULT_TURNSTILE_VERIFY_URL;
  const form = new URLSearchParams();
  form.set("secret", env.TURNSTILE_SECRET_KEY);
  form.set("response", token);
  if (ip && ip !== "0.0.0.0") form.set("remoteip", ip);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    const data = (await resp.json()) as { success?: boolean };
    return data.success === true;
  } catch {
    return false;
  }
}

async function sendTokenEmail(
  env: Env,
  to: string,
  token: string,
): Promise<boolean> {
  const base = env.RESEND_API_BASE || DEFAULT_RESEND_API_BASE;
  const site = env.SITE_NAME || "Content Quality Demo";
  const subject = `Your ${site} access token`;
  const html =
    `<p>Here is your access token for <strong>${escapeHtml(site)}</strong>.</p>` +
    `<p style="font-size:15px;">Paste this into the evaluator to try it:</p>` +
    `<p style="font-family:monospace;font-size:16px;background:#f2f2f2;padding:10px 14px;border-radius:6px;word-break:break-all;">${escapeHtml(
      token,
    )}</p>` +
    `<p style="font-size:13px;color:#666;">Keep it to yourself. It carries a small daily quota and a monthly cost cap. If you did not request this, you can ignore this email.</p>`;
  const text =
    `Here is your access token for ${site}.\n\n` +
    `${token}\n\n` +
    `Paste it into the evaluator to try it. It carries a small daily quota and a monthly cost cap.\n` +
    `If you did not request this, you can ignore this email.\n`;
  try {
    const resp = await fetch(`${base}/emails`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.RESEND_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ from: env.FROM_ADDR, to, subject, html, text }),
    });
    return resp.status >= 200 && resp.status < 300;
  } catch {
    return false;
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

interface PoeResult {
  text: string;
  inputTokens: number;
  outputTokens: number;
  tokenSource: "api" | "estimated";
  ok: boolean;
}

async function callPoe(
  env: Env,
  model: string,
  system: string,
  user: string,
): Promise<PoeResult> {
  const base = env.POE_BASE_URL || DEFAULT_POE_BASE_URL;
  const payload = {
    model,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
    temperature: TEMPERATURE,
    max_tokens: MAX_OUTPUT_TOKENS,
  };
  try {
    const resp = await fetch(`${base}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.POE_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
    if (!(resp.status >= 200 && resp.status < 300)) {
      const errText = await resp.text();
      return {
        text: `__ERROR__: ${resp.status} ${errText.slice(0, 200)}`,
        inputTokens: estimateTokens(system + "\n" + user),
        outputTokens: 0,
        tokenSource: "estimated",
        ok: false,
      };
    }
    const data = (await resp.json()) as {
      choices?: { message?: { content?: string } }[];
      usage?: { prompt_tokens?: number; completion_tokens?: number };
    };
    const text = data.choices?.[0]?.message?.content ?? "";
    const pt = data.usage?.prompt_tokens;
    const ct = data.usage?.completion_tokens;
    if (typeof pt === "number" && typeof ct === "number") {
      return { text, inputTokens: pt, outputTokens: ct, tokenSource: "api", ok: true };
    }
    return {
      text,
      inputTokens: estimateTokens(system + "\n" + user),
      outputTokens: estimateTokens(text),
      tokenSource: "estimated",
      ok: true,
    };
  } catch (e) {
    return {
      text: `__ERROR__: ${String(e)}`,
      inputTokens: estimateTokens(system + "\n" + user),
      outputTokens: 0,
      tokenSource: "estimated",
      ok: false,
    };
  }
}

// ---------------------------------------------------------------------------
// Rubric parsing and scoring. Ported from src/rubric.py parse_rubric so the
// live demo accepts exactly the JSON shape the study defines.
// ---------------------------------------------------------------------------

interface ParsedRubric {
  scores: Record<string, number>; // includes each dimension and "overall"
  reasons: Record<string, string>;
  rationale: string;
}

function extractScore(value: unknown): { score: number | null; reason: string } {
  if (value !== null && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if (!("score" in obj)) return { score: null, reason: "" };
    const raw = obj.score;
    const reason = obj.reason === undefined ? "" : String(obj.reason);
    const n = Number(raw);
    return { score: Number.isFinite(n) ? n : null, reason };
  }
  const n = Number(value);
  return { score: Number.isFinite(n) ? n : null, reason: "" };
}

function parseRubric(text: string): ParsedRubric | null {
  if (!text || text.startsWith("__ERROR__")) return null;
  const match = text.trim().match(/\{[\s\S]*\}/);
  if (!match) return null;
  let obj: unknown;
  try {
    obj = JSON.parse(match[0]);
  } catch {
    return null;
  }
  if (obj === null || typeof obj !== "object") return null;
  const src = obj as Record<string, unknown>;
  const scores: Record<string, number> = {};
  const reasons: Record<string, string> = {};
  let rationale = "";
  for (const f of ALL_FIELDS) {
    if (!(f in src)) return null;
    const { score, reason } = extractScore(src[f]);
    if (score === null) return null;
    scores[f] = Math.max(SCALE_MIN, Math.min(SCALE_MAX, score));
    reasons[f] = reason;
    if (f === "overall") rationale = reason.slice(0, 300);
  }
  return { scores, reasons, rationale };
}

// The refined fitted composite (study's refined_rubric) is computed in
// rubric-config.ts compositeScore, so all study math lives in one module.

// ---------------------------------------------------------------------------
// D1 access. Daily and monthly rollovers are applied on read.
// ---------------------------------------------------------------------------

interface TokenRow {
  token: string;
  email: string;
  quota_per_day: number;
  used_today: number;
  used_total: number;
  day: string;
  cost_this_month: number;
  month_start: string;
  revoked: number;
  created_at: string;
  last_used: string | null;
}

async function getTokenRow(env: Env, token: string): Promise<TokenRow | null> {
  const row = await env.EVAL_DB.prepare(
    `SELECT token, email, quota_per_day, used_today, used_total, day,
            cost_this_month, month_start, revoked, created_at, last_used
       FROM access_tokens WHERE token = ?`,
  )
    .bind(token)
    .first<TokenRow>();
  return row ?? null;
}

// Apply daily and monthly rollovers in memory and persist if anything reset.
async function rolloverToken(env: Env, row: TokenRow): Promise<TokenRow> {
  const today = todayUtc();
  const month = monthUtc();
  let changed = false;
  if (row.day !== today) {
    row.used_today = 0;
    row.day = today;
    changed = true;
  }
  if (row.month_start !== month) {
    row.cost_this_month = 0;
    row.month_start = month;
    changed = true;
  }
  if (changed) {
    await env.EVAL_DB.prepare(
      `UPDATE access_tokens
          SET used_today = ?, day = ?, cost_this_month = ?, month_start = ?
        WHERE token = ?`,
    )
      .bind(row.used_today, row.day, row.cost_this_month, row.month_start, row.token)
      .run();
  }
  return row;
}

// Reserve one slot against the hard global daily cap. Returns true if reserved,
// false if the cap is already reached. Reserving BEFORE the Poe call keeps the
// global spend bounded even under concurrency.
async function reserveGlobalSlot(env: Env, cap: number): Promise<boolean> {
  const today = todayUtc();
  const row = await env.EVAL_DB.prepare(
    `SELECT count FROM counters WHERE name = 'global_calls' AND day = ?`,
  )
    .bind(today)
    .first<{ count: number }>();
  const current = row?.count ?? 0;
  if (current >= cap) return false;
  await env.EVAL_DB.prepare(
    `INSERT INTO counters (name, day, count) VALUES ('global_calls', ?, 1)
       ON CONFLICT(name) DO UPDATE SET
         count = CASE WHEN counters.day = excluded.day THEN counters.count + 1 ELSE 1 END,
         day = excluded.day`,
  )
    .bind(today)
    .run();
  return true;
}

async function releaseGlobalSlot(env: Env): Promise<void> {
  const today = todayUtc();
  await env.EVAL_DB.prepare(
    `UPDATE counters SET count = MAX(0, count - 1)
       WHERE name = 'global_calls' AND day = ?`,
  )
    .bind(today)
    .run();
}

// ---------------------------------------------------------------------------
// Private submission cache and log (submissions table). Lives only in the
// operator's own D1. lookupSubmission returns a prior result for the SAME model
// and content hash so a repeat costs no Poe call. insertSubmission records each
// fresh evaluation (email, passage, scores, decision).
// ---------------------------------------------------------------------------
interface SubmissionRow {
  clarity: number | null;
  neutrality: number | null;
  verifiability: number | null;
  coverage: number | null;
  structure: number | null;
  readability: number | null;
  informativeness: number | null;
  overall: number;
  composite: number;
  decision: string;
}

async function lookupSubmission(
  env: Env,
  model: string,
  contentHash: string,
): Promise<SubmissionRow | null> {
  const row = await env.EVAL_DB.prepare(
    `SELECT clarity, neutrality, verifiability, coverage, structure, readability,
            informativeness, overall, composite, decision
       FROM submissions
      WHERE model = ? AND content_hash = ?
      ORDER BY created_at DESC
      LIMIT 1`,
  )
    .bind(model, contentHash)
    .first<SubmissionRow>();
  return row ?? null;
}

async function insertSubmission(
  env: Env,
  opts: {
    email: string | null;
    content: string;
    contentHash: string;
    model: string;
    scores: Record<string, number>;
    overall: number;
    composite: number;
    decision: string;
    cached: boolean;
    cost: number;
  },
): Promise<void> {
  await env.EVAL_DB.prepare(
    `INSERT INTO submissions
       (id, created_at, email, content, content_hash, model,
        clarity, neutrality, verifiability, coverage, structure, readability,
        informativeness, overall, composite, decision, cached, cost_usd)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(
      randomId(),
      new Date().toISOString(),
      opts.email,
      opts.content,
      opts.contentHash,
      opts.model,
      opts.scores.clarity,
      opts.scores.neutrality,
      opts.scores.verifiability,
      opts.scores.coverage,
      opts.scores.structure,
      opts.scores.readability,
      opts.scores.informativeness,
      opts.overall,
      opts.composite,
      opts.decision,
      opts.cached ? 1 : 0,
      opts.cost,
    )
    .run();
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async function handleRequestAccess(req: Request, env: Env): Promise<Response> {
  const limits = readLimits(env);
  const neutral = () =>
    json(req, env, {
      ok: true,
      message: "If that email is valid, a token is on its way.",
    });

  if (!originAllowed(req, env)) {
    return json(req, env, { ok: false, error: "forbidden_origin" }, 403);
  }

  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return neutral();
  }
  const email = String(body.email ?? "").trim().toLowerCase();
  const turnstileToken = String(body.turnstileToken ?? body.turnstile ?? "");
  const ip = clientIp(req);

  // Verify Turnstile first. A neutral success on failure would let an attacker
  // probe without solving the challenge, so here we return an explicit error
  // because the human is expected to have solved a visible widget.
  const okTs = await verifyTurnstile(env, turnstileToken, ip);
  if (!okTs) {
    return json(req, env, { ok: false, error: "turnstile_failed" }, 400);
  }

  // From here on, always return the same neutral success so we never leak which
  // emails exist or are rate limited.
  if (!EMAIL_RE.test(email)) return neutral();
  const domain = email.split("@")[1] || "";
  if (DISPOSABLE_DOMAINS.has(domain)) return neutral();

  // Per-IP daily cap on access requests.
  const today = todayUtc();
  const ipKey = `ip:${ip}:${today}`;
  const ipRow = await env.EVAL_DB.prepare(
    `SELECT count FROM rate_limits WHERE key = ?`,
  )
    .bind(ipKey)
    .first<{ count: number }>();
  if ((ipRow?.count ?? 0) >= limits.IP_DAILY_REQUEST_CAP) {
    return neutral();
  }
  await env.EVAL_DB.prepare(
    `INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)
       ON CONFLICT(key) DO UPDATE SET count = rate_limits.count + 1`,
  )
    .bind(ipKey, new Date().toISOString())
    .run();

  // Per-email cooldown. If a token was minted for this email within the
  // cooldown window, silently succeed without minting or sending another.
  const cutoff = new Date(
    Date.now() - limits.EMAIL_COOLDOWN_HOURS * 3600 * 1000,
  ).toISOString();
  const recent = await env.EVAL_DB.prepare(
    `SELECT token FROM access_tokens WHERE email = ? AND created_at > ? LIMIT 1`,
  )
    .bind(email, cutoff)
    .first<{ token: string }>();
  if (recent) return neutral();

  // Mint and store a new token.
  const token = generateToken();
  const now = new Date().toISOString();
  await env.EVAL_DB.prepare(
    `INSERT INTO access_tokens
       (token, email, quota_per_day, used_today, used_total, day,
        cost_this_month, month_start, revoked, created_at, last_used)
     VALUES (?, ?, ?, 0, 0, ?, 0, ?, 0, ?, NULL)`,
  )
    .bind(
      token,
      email,
      limits.DEFAULT_TOKEN_QUOTA_PER_DAY,
      todayUtc(),
      monthUtc(),
      now,
    )
    .run();

  // Email the token. If the send fails, the token still exists so the user can
  // retry after the cooldown. We do not leak the failure.
  await sendTokenEmail(env, email, token);

  return neutral();
}

async function handleEvaluate(req: Request, env: Env): Promise<Response> {
  const limits = readLimits(env);

  if (!originAllowed(req, env)) {
    return json(req, env, { ok: false, error: "forbidden_origin" }, 403);
  }

  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return json(req, env, { ok: false, error: "bad_request", message: "Invalid JSON body." }, 400);
  }

  const token = String(body.token ?? "").trim();
  const turnstileToken = String(body.turnstileToken ?? body.turnstile ?? "");
  const content = String(body.content ?? "");
  const model = String(body.model ?? "") as ModelName;
  const ip = clientIp(req);

  // Guard 1: Turnstile. Cheap, non-consuming.
  const okTs = await verifyTurnstile(env, turnstileToken, ip);
  if (!okTs) {
    return json(req, env, { ok: false, error: "turnstile_failed", message: "Human check failed. Please retry the challenge." }, 400);
  }

  // Guard 2: access token exists and is not revoked. Non-consuming.
  if (!/^[a-f0-9]{64}$/.test(token)) {
    return json(req, env, { ok: false, error: "bad_token", message: "That access token is not valid." }, 401);
  }
  let row = await getTokenRow(env, token);
  if (!row) {
    return json(req, env, { ok: false, error: "bad_token", message: "That access token is not valid." }, 401);
  }
  if (row.revoked) {
    return json(req, env, { ok: false, error: "revoked_token", message: "That access token has been revoked." }, 403);
  }
  // Apply daily and monthly rollovers so the guards below (on a cache miss) read
  // the correct current-day and current-month counters.
  row = await rolloverToken(env, row);

  // Guard 3: input validation. Known model, non-empty content, size cap. All
  // non-consuming.
  if (!(MODELS as readonly string[]).includes(model)) {
    return json(
      req,
      env,
      { ok: false, error: "bad_model", message: `Choose one model of: ${MODELS.join(", ")}.` },
      400,
    );
  }
  const trimmed = content.trim();
  if (trimmed.length === 0) {
    return json(req, env, { ok: false, error: "empty_content", message: "Paste some content to evaluate." }, 400);
  }
  if (content.length > limits.MAX_INPUT_CHARS) {
    return json(
      req,
      env,
      {
        ok: false,
        error: "content_too_large",
        message: `Content is ${content.length} characters. The limit is ${limits.MAX_INPUT_CHARS}.`,
      },
      413,
    );
  }

  // Cache lookup, BEFORE any consuming guard. If this exact (model, content) was
  // evaluated before, return the stored result WITHOUT a new Poe call. A cache
  // hit costs nothing, so it does NOT consume the per-token daily quota, the
  // per-IP daily cap, the monthly cost cap, or a global slot. The request is
  // still logged (cached = 1, zero cost). To get a hit you must send the exact
  // passage (the hash is computed here), so this reveals nothing new. The model
  // is part of the key, so the same passage judged by a different model misses.
  const contentHash = await sha256Hex(trimmed);
  const cachedRow = await lookupSubmission(env, model, contentHash);
  if (cachedRow) {
    const cachedScores: Record<string, number> = {};
    for (const d of DIMENSIONS) cachedScores[d] = cachedRow[d] as number;
    const dims = DIMENSIONS.map((d) => ({
      name: d,
      score: cachedRow[d] as number,
      reason: "",
    }));
    // Log the cache hit as its own request row (cached = 1, zero cost). Best
    // effort: a logging failure must not fail the user's evaluation.
    try {
      await insertSubmission(env, {
        email: row.email ?? null,
        content: trimmed,
        contentHash,
        model,
        scores: cachedScores,
        overall: cachedRow.overall,
        composite: cachedRow.composite,
        decision: cachedRow.decision,
        cached: true,
        cost: 0,
      });
    } catch {
      // ignore
    }
    return json(req, env, {
      ok: true,
      cached: true,
      model,
      reasoning: "",
      overall_reason:
        "This exact passage and model were evaluated before. Showing the stored scores. No new judge call was made.",
      dimensions: dims,
      overall: cachedRow.overall,
      composite: Number(cachedRow.composite.toFixed(2)),
      good_threshold: GOOD_THRESHOLD,
      decision: cachedRow.decision,
      repaired: false,
      usage: {
        input_tokens: 0,
        output_tokens: 0,
        token_source: "cache",
        call_cost_usd: 0,
      },
    });
  }

  // --- Cache MISS. Only now do the consuming guards run, in order. ---

  // Guard 4: per-token daily quota.
  const dailyQuota = row.quota_per_day || limits.PER_TOKEN_DAILY_QUOTA;
  if (row.used_today >= dailyQuota) {
    return json(
      req,
      env,
      {
        ok: false,
        error: "daily_quota_exceeded",
        message: `This token has used its ${dailyQuota} evaluations for today. Try again tomorrow.`,
      },
      429,
    );
  }

  // Guard 5: per-IP daily call cap. Counts every fresh (non-cached) evaluation
  // from this IP, so cycling through tokens or emails from one address is
  // deterred. Cache hits above never reach here, so they do not consume it.
  const today = todayUtc();
  const evalIpKey = `eval_ip:${ip}:${today}`;
  const evalIpRow = await env.EVAL_DB.prepare(
    `SELECT count FROM rate_limits WHERE key = ?`,
  )
    .bind(evalIpKey)
    .first<{ count: number }>();
  if ((evalIpRow?.count ?? 0) >= limits.EVAL_IP_DAILY_CAP) {
    return json(
      req,
      env,
      {
        ok: false,
        error: "ip_daily_limit_reached",
        message: `The daily limit for this address has been reached. Please try again tomorrow.`,
      },
      429,
    );
  }
  await env.EVAL_DB.prepare(
    `INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)
       ON CONFLICT(key) DO UPDATE SET count = rate_limits.count + 1`,
  )
    .bind(evalIpKey, new Date().toISOString())
    .run();

  // Guard 6: per-user monthly cost cap. Warm collective message if reached.
  if (row.cost_this_month >= limits.MONTHLY_COST_CAP_USD) {
    return json(req, env, {
      ok: false,
      error: "monthly_cost_cap_reached",
      capped: true,
      message: CAPPED_MESSAGE,
      support_url: supportUrl(env),
    });
  }

  // Guard 7: reserve a slot against the hard global daily cap BEFORE calling.
  const reserved = await reserveGlobalSlot(env, limits.GLOBAL_DAILY_CALL_CAP);
  if (!reserved) {
    return json(
      req,
      env,
      {
        ok: false,
        error: "global_cap_reached",
        message: "The demo has reached its global limit for today. Please try again tomorrow.",
      },
      503,
    );
  }

  // All guards passed. Call Poe.
  const system = buildSystemPrompt(model);
  const user = buildUserPrompt(trimmed);
  let poe = await callPoe(env, model, system, user);

  let totalInput = poe.inputTokens;
  let totalOutput = poe.outputTokens;
  let callCost = costUsd(model, poe.inputTokens, poe.outputTokens);

  if (!poe.ok) {
    // Roll back the reserved global slot so a provider error does not consume
    // the daily budget.
    await releaseGlobalSlot(env);
    return json(
      req,
      env,
      { ok: false, error: "judge_unavailable", message: "The judge model could not be reached. Please try again." },
      502,
    );
  }

  // Parse, with a JSON-repair fallback (a cheap second call).
  let parsed = parseRubric(poe.text);
  let repaired = false;
  if (!parsed) {
    const repairUser =
      "Required JSON schema:\n" +
      jsonInstruction() +
      "\n\nReply to convert into that schema:\n" +
      poe.text.slice(0, 4000);
    const rep = await callPoe(env, REPAIR_MODEL, REPAIR_SYSTEM, repairUser);
    if (rep.ok) {
      totalInput += rep.inputTokens;
      totalOutput += rep.outputTokens;
      callCost += costUsd(REPAIR_MODEL, rep.inputTokens, rep.outputTokens);
      parsed = parseRubric(rep.text);
      repaired = true;
    }
  }

  // Persist usage counters and cost regardless of parse outcome, since the Poe
  // call happened and cost real budget.
  const now = new Date().toISOString();
  await env.EVAL_DB.prepare(
    `UPDATE access_tokens
        SET used_today = used_today + 1,
            used_total = used_total + 1,
            cost_this_month = cost_this_month + ?,
            last_used = ?
      WHERE token = ?`,
  )
    .bind(callCost, now, token)
    .run();

  if (!parsed) {
    return json(
      req,
      env,
      {
        ok: false,
        error: "unparseable_response",
        message: "The judge reply could not be parsed into scores, even after a repair attempt.",
      },
      502,
    );
  }

  const overall = parsed.scores.overall;
  const composite = compositeScore(parsed.scores);
  // The study thresholds the DIRECT overall score for its good/bad metric, so
  // the direct overall drives the decision here. The composite is returned
  // alongside for transparency but does not change the good/bad call.
  const decisionGood = overall >= GOOD_THRESHOLD;

  const dimensions = DIMENSIONS.map((d) => ({
    name: d,
    score: parsed!.scores[d],
    reason: parsed!.reasons[d] || "",
  }));

  // Log this fresh evaluation to the private submissions table. This also seeds
  // the cache so an identical (model, content) repeat is served without a Poe
  // call. Best effort: a logging failure must not fail the user's evaluation.
  try {
    await insertSubmission(env, {
      email: row.email ?? null,
      content: trimmed,
      contentHash,
      model,
      scores: parsed.scores,
      overall,
      composite: Number(composite.toFixed(2)),
      decision: decisionGood ? "good" : "bad",
      cached: false,
      cost: Number(callCost.toFixed(6)),
    });
  } catch {
    // ignore
  }

  return json(req, env, {
    ok: true,
    model,
    reasoning: parsed.rationale,
    overall_reason: parsed.reasons.overall || "",
    dimensions,
    overall,
    composite: Number(composite.toFixed(2)),
    good_threshold: GOOD_THRESHOLD,
    decision: decisionGood ? "good" : "bad",
    repaired,
    usage: {
      input_tokens: totalInput,
      output_tokens: totalOutput,
      token_source: poe.tokenSource,
      call_cost_usd: Number(callCost.toFixed(6)),
    },
  });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(req, env) });
    }
    if (req.method === "GET" && url.pathname === "/health") {
      return json(req, env, { ok: true, service: "eval-proxy" });
    }
    if (req.method === "POST" && url.pathname === "/request-access") {
      return handleRequestAccess(req, env);
    }
    if (req.method === "POST" && url.pathname === "/evaluate") {
      return handleEvaluate(req, env);
    }
    return json(req, env, { ok: false, error: "not_found" }, 404);
  },
};
