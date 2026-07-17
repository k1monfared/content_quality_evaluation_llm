/**
 * validate.ts
 *
 * Local, offline validation of the eval-proxy Worker. No network and no cost.
 *
 * It drives the REAL worker.fetch handler with:
 *   - a real in-memory SQLite database (node:sqlite) wrapped to look like D1,
 *     loaded from the real schema.sql, so the actual SQL runs, and
 *   - a stubbed global fetch that fakes Turnstile, Resend, and Poe, so no
 *     external service is ever contacted.
 *
 * Run with:  npm run validate
 *   (node --experimental-strip-types test/validate.ts)
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import worker from "../src/worker.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCHEMA = readFileSync(join(__dirname, "..", "schema.sql"), "utf8");

// --------------------------------------------------------------------------
// Minimal D1-compatible wrapper over node:sqlite.
// --------------------------------------------------------------------------
function makeD1(db: DatabaseSync) {
  function statement(sql: string, params: unknown[]) {
    return {
      bind(...args: unknown[]) {
        return statement(sql, args);
      },
      async run() {
        const info = db.prepare(sql).run(...(params as any[]));
        return { success: true, meta: { changes: info.changes } };
      },
      async first(_col?: string) {
        const row = db.prepare(sql).get(...(params as any[]));
        if (row === undefined) return null;
        if (_col) return (row as Record<string, unknown>)[_col];
        return row;
      },
      async all() {
        const rows = db.prepare(sql).all(...(params as any[]));
        return { success: true, results: rows };
      },
    };
  }
  return {
    prepare(sql: string) {
      return statement(sql, []);
    },
  };
}

// --------------------------------------------------------------------------
// Stubbed external services, controlled per scenario.
// --------------------------------------------------------------------------
const STUB = {
  turnstileOk: true,
  poeMode: "good" as "good" | "bad_json" | "repair" | "error",
  emailCalls: [] as { to: unknown }[],
  poeCalls: [] as { model: string }[],
};

const REPAIR_MODEL = "claude-haiku-4.5";

function rubricJson(overall: number, dim: number) {
  const dims = [
    "clarity",
    "neutrality",
    "verifiability",
    "coverage",
    "structure",
    "readability",
    "informativeness",
  ];
  const obj: Record<string, unknown> = {};
  for (const d of dims) obj[d] = { reason: `reason for ${d}`, score: dim };
  obj.overall = { reason: "holistic reason", score: overall };
  return JSON.stringify(obj);
}

globalThis.fetch = (async (input: any, init?: any) => {
  const url = typeof input === "string" ? input : input.url;
  // Turnstile verify.
  if (url.includes("siteverify")) {
    return new Response(JSON.stringify({ success: STUB.turnstileOk }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  // Resend email.
  if (url.includes("/emails")) {
    let to: unknown = null;
    try {
      to = JSON.parse(init.body).to;
    } catch {}
    STUB.emailCalls.push({ to });
    return new Response(JSON.stringify({ id: "stub-email-id" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  // Poe chat completions.
  if (url.includes("/chat/completions")) {
    const bodyObj = JSON.parse(init.body);
    const model = bodyObj.model as string;
    STUB.poeCalls.push({ model });
    if (STUB.poeMode === "error") {
      return new Response("upstream boom", { status: 500 });
    }
    let content: string;
    if (STUB.poeMode === "good") {
      content = rubricJson(8, 7);
    } else if (STUB.poeMode === "bad_json") {
      content = "the passage is fine, no json here";
    } else {
      // repair mode: the judge returns junk, the repair model returns valid JSON.
      content = model === REPAIR_MODEL ? rubricJson(4, 5) : "not valid json at all";
    }
    return new Response(
      JSON.stringify({
        choices: [{ message: { content } }],
        usage: { prompt_tokens: 1200, completion_tokens: 180 },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  throw new Error("unexpected fetch to " + url);
}) as any;

// --------------------------------------------------------------------------
// Test harness plumbing.
// --------------------------------------------------------------------------
const db = new DatabaseSync(":memory:");
db.exec(SCHEMA);
const EVAL_DB = makeD1(db);

function baseEnv(overrides: Record<string, unknown> = {}) {
  return {
    POE_API_KEY: "stub-poe",
    RESEND_API_KEY: "stub-resend",
    TURNSTILE_SECRET_KEY: "stub-turnstile",
    FROM_ADDR: "Demo <demo@example.com>",
    SITE_NAME: "Demo",
    ALLOWED_ORIGINS: "",
    SUPPORT_URL: "https://example.com/support",
    TURNSTILE_VERIFY_URL: "https://stub.local/siteverify",
    RESEND_API_BASE: "https://stub.local/resend",
    POE_BASE_URL: "https://stub.local/poe",
    EVAL_DB,
    ...overrides,
  } as any;
}

function req(path: string, body: unknown, ip = "10.0.0.1") {
  return new Request("https://worker.local" + path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "cf-connecting-ip": ip,
    },
    body: JSON.stringify(body),
  });
}

function tokenHex(seed: string) {
  // 64 hex chars, deterministic per seed for readable test data.
  let s = "";
  for (let i = 0; i < 64; i++) s += ((seed.charCodeAt(i % seed.length) + i) % 16).toString(16);
  return s;
}

function insertToken(opts: {
  token: string;
  quota?: number;
  used_today?: number;
  cost_this_month?: number;
  revoked?: number;
}) {
  const now = new Date();
  const day = now.toISOString().slice(0, 10);
  const month = now.toISOString().slice(0, 7);
  db.prepare(
    `INSERT INTO access_tokens
       (token, email, quota_per_day, used_today, used_total, day,
        cost_this_month, month_start, revoked, created_at, last_used)
     VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL)`,
  ).run(
    opts.token,
    "user@example.com",
    opts.quota ?? 10,
    opts.used_today ?? 0,
    day,
    opts.cost_this_month ?? 0,
    month,
    opts.revoked ?? 0,
    now.toISOString(),
  );
}

let passed = 0;
let failed = 0;
function check(name: string, cond: boolean, detail = "") {
  if (cond) {
    passed++;
    console.log(`PASS  ${name}`);
  } else {
    failed++;
    console.log(`FAIL  ${name}  ${detail}`);
  }
}

async function call(path: string, body: unknown, ip?: string, env = baseEnv()) {
  const resp = await worker.fetch(req(path, body, ip), env);
  const data = (await resp.json()) as any;
  return { status: resp.status, data };
}

// --------------------------------------------------------------------------
// Scenarios
// --------------------------------------------------------------------------
async function run() {
  // 1. request-access happy path: token minted, email stub called.
  STUB.turnstileOk = true;
  STUB.emailCalls = [];
  {
    const { status, data } = await call("/request-access", {
      email: "alice@example.com",
      turnstileToken: "tok",
    });
    const row = db
      .prepare(`SELECT count(*) AS n FROM access_tokens WHERE email = ?`)
      .get("alice@example.com") as { n: number };
    check(
      "request-access happy: neutral success",
      status === 200 && data.ok === true && /token is on its way/i.test(data.message),
      JSON.stringify(data),
    );
    check("request-access happy: token minted", row.n === 1, `n=${row.n}`);
    check(
      "request-access happy: email stub called once",
      STUB.emailCalls.length === 1 && STUB.emailCalls[0].to === "alice@example.com",
      JSON.stringify(STUB.emailCalls),
    );
  }

  // 2. request-access with failing Turnstile: refused, no token, no email.
  STUB.turnstileOk = false;
  STUB.emailCalls = [];
  {
    const { status, data } = await call("/request-access", {
      email: "bob@example.com",
      turnstileToken: "bad",
    });
    const row = db
      .prepare(`SELECT count(*) AS n FROM access_tokens WHERE email = ?`)
      .get("bob@example.com") as { n: number };
    check(
      "request-access turnstile fail: refused",
      status === 400 && data.ok === false && data.error === "turnstile_failed",
      JSON.stringify(data),
    );
    check("request-access turnstile fail: no token minted", row.n === 0);
    check("request-access turnstile fail: no email sent", STUB.emailCalls.length === 0);
  }

  // 3. request-access cooldown: second request within window mints nothing new.
  STUB.turnstileOk = true;
  STUB.emailCalls = [];
  {
    await call("/request-access", { email: "carol@example.com", turnstileToken: "t" });
    await call("/request-access", { email: "carol@example.com", turnstileToken: "t" });
    const row = db
      .prepare(`SELECT count(*) AS n FROM access_tokens WHERE email = ?`)
      .get("carol@example.com") as { n: number };
    check("request-access cooldown: only one token", row.n === 1, `n=${row.n}`);
    check("request-access cooldown: only one email", STUB.emailCalls.length === 1);
  }

  // 4. evaluate happy path: parsed scores from stubbed Poe response.
  STUB.turnstileOk = true;
  STUB.poeMode = "good";
  {
    const tk = tokenHex("happy");
    insertToken({ token: tk });
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "A well written passage.", model: "gpt-5.2" },
      "10.1.0.1",
    );
    check(
      "evaluate happy: ok with 7 dimensions",
      status === 200 && data.ok === true && Array.isArray(data.dimensions) && data.dimensions.length === 7,
      JSON.stringify(data).slice(0, 200),
    );
    check(
      "evaluate happy: overall, composite, decision present",
      typeof data.overall === "number" && typeof data.composite === "number" && (data.decision === "good" || data.decision === "bad"),
      JSON.stringify({ o: data.overall, c: data.composite, d: data.decision }),
    );
    check(
      "evaluate happy: decision good (direct overall 8 >= 6)",
      data.decision === "good",
      String(data.decision),
    );
    // Refined fitted composite over {neutrality, verifiability, coverage,
    // readability}, all dims = 7:
    //   -0.106 + 7*(0.0495 + 0.277 + 0.4139 + 0.3138) = 7.2734 -> 7.27
    check(
      "evaluate happy: refined fitted composite value",
      Math.abs(data.composite - 7.27) < 1e-9,
      `composite=${data.composite}`,
    );
    const row = db.prepare(`SELECT used_today, cost_this_month FROM access_tokens WHERE token = ?`).get(tk) as {
      used_today: number;
      cost_this_month: number;
    };
    check("evaluate happy: usage incremented", row.used_today === 1, `used_today=${row.used_today}`);
    check("evaluate happy: cost accrued", row.cost_this_month > 0, `cost=${row.cost_this_month}`);
    // cost = 1200/1e6*1.25 + 180/1e6*10 = 0.0015 + 0.0018 = 0.0033
    check(
      "evaluate happy: cost computed from tokens x price",
      Math.abs(row.cost_this_month - 0.0033) < 1e-9,
      `cost=${row.cost_this_month}`,
    );
  }

  // 5. evaluate bad token.
  STUB.turnstileOk = true;
  STUB.poeMode = "good";
  {
    const { status, data } = await call(
      "/evaluate",
      { token: "f".repeat(64), turnstileToken: "t", content: "text", model: "gpt-5.2" },
      "10.2.0.1",
    );
    check(
      "evaluate bad token: refused 401",
      status === 401 && data.error === "bad_token",
      JSON.stringify(data),
    );
  }

  // 6. evaluate missing Turnstile token.
  {
    const tk = tokenHex("nots");
    insertToken({ token: tk });
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "", content: "text", model: "gpt-5.2" },
      "10.3.0.1",
    );
    check(
      "evaluate missing turnstile: refused",
      status === 400 && data.error === "turnstile_failed",
      JSON.stringify(data),
    );
  }

  // 7. evaluate oversized input.
  {
    const tk = tokenHex("big");
    insertToken({ token: tk });
    const big = "x".repeat(9000);
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: big, model: "gpt-5.2" },
      "10.4.0.1",
    );
    check(
      "evaluate oversized: refused 413",
      status === 413 && data.error === "content_too_large",
      JSON.stringify(data),
    );
  }

  // 8. evaluate over per-token daily quota.
  {
    const tk = tokenHex("quota");
    insertToken({ token: tk, quota: 1, used_today: 1 });
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "text", model: "gpt-5.2" },
      "10.5.0.1",
    );
    check(
      "evaluate over daily quota: refused 429",
      status === 429 && data.error === "daily_quota_exceeded",
      JSON.stringify(data),
    );
  }

  // 9. evaluate over monthly cost cap: friendly collective "we" message + support url.
  {
    const tk = tokenHex("cost");
    insertToken({ token: tk, cost_this_month: 5.0 });
    const before = STUB.poeCalls.length;
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "text", model: "gpt-5.2" },
      "10.6.0.1",
    );
    check(
      "evaluate over monthly cost cap: capped flag",
      data.ok === false && data.capped === true && data.error === "monthly_cost_cap_reached",
      JSON.stringify(data),
    );
    check(
      "evaluate over monthly cost cap: collective we message + support url",
      /we have reached our token limit/i.test(data.message) &&
        /supporting us/i.test(data.message) &&
        data.support_url === "https://example.com/support",
      JSON.stringify(data),
    );
    check(
      "evaluate over monthly cost cap: no Poe call made",
      STUB.poeCalls.length === before,
      `poeCalls delta=${STUB.poeCalls.length - before}`,
    );
  }

  // 10. evaluate over global daily hard cap (cap set to 0 via env).
  {
    const tk = tokenHex("global");
    insertToken({ token: tk });
    const before = STUB.poeCalls.length;
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "text", model: "gpt-5.2" },
      "10.7.0.1",
      baseEnv({ GLOBAL_DAILY_CALL_CAP: "0" }),
    );
    check(
      "evaluate over global cap: refused 503",
      status === 503 && data.error === "global_cap_reached",
      JSON.stringify(data),
    );
    check(
      "evaluate over global cap: no Poe call made",
      STUB.poeCalls.length === before,
      `poeCalls delta=${STUB.poeCalls.length - before}`,
    );
  }

  // 11. evaluate per-IP daily cap: 3 fresh evaluations succeed, 4th refused, all
  // from one IP. Each call uses UNIQUE content so every one is a cache miss that
  // consumes an IP slot (cache hits would bypass the per-IP cap by design).
  {
    const ip = "10.8.0.9";
    for (let i = 0; i < 3; i++) {
      const tk = tokenHex("ipcap" + i);
      insertToken({ token: tk });
      const r = await call(
        "/evaluate",
        { token: tk, turnstileToken: "t", content: "per-IP cap passage number " + i, model: "gpt-5.2" },
        ip,
      );
      check(`evaluate per-IP cap: call ${i + 1} of 3 allowed`, r.data.ok === true, JSON.stringify(r.data).slice(0, 120));
    }
    const tk4 = tokenHex("ipcap4");
    insertToken({ token: tk4 });
    const before = STUB.poeCalls.length;
    const { status, data } = await call(
      "/evaluate",
      { token: tk4, turnstileToken: "t", content: "per-IP cap passage number 3", model: "gpt-5.2" },
      ip,
    );
    check(
      "evaluate per-IP cap: 4th refused 429",
      status === 429 && data.error === "ip_daily_limit_reached",
      JSON.stringify(data),
    );
    check(
      "evaluate per-IP cap: 4th made no Poe call",
      STUB.poeCalls.length === before,
      `delta=${STUB.poeCalls.length - before}`,
    );
  }

  // 12. evaluate bad model.
  {
    const tk = tokenHex("badmodel");
    insertToken({ token: tk });
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "text", model: "not-a-model" },
      "10.9.0.1",
    );
    check("evaluate bad model: refused 400", status === 400 && data.error === "bad_model", JSON.stringify(data));
  }

  // 13. evaluate repair fallback: judge returns junk, repair model returns valid JSON.
  STUB.poeMode = "repair";
  {
    const tk = tokenHex("repair");
    insertToken({ token: tk });
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "text", model: "gemini-2.5-flash" },
      "10.10.0.1",
    );
    check(
      "evaluate repair path: ok and repaired flag set",
      status === 200 && data.ok === true && data.repaired === true && data.dimensions.length === 7,
      JSON.stringify(data).slice(0, 160),
    );
  }

  // 14. evaluate judge error: refused 502, global slot released.
  STUB.poeMode = "error";
  {
    const tk = tokenHex("err");
    insertToken({ token: tk });
    const today = new Date().toISOString().slice(0, 10);
    const beforeRow = db
      .prepare(`SELECT count FROM counters WHERE name='global_calls' AND day=?`)
      .get(today) as { count: number } | undefined;
    const before = beforeRow?.count ?? 0;
    const { status, data } = await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content: "a unique error-path passage", model: "gpt-5.2" },
      "10.11.0.1",
    );
    const afterRow = db
      .prepare(`SELECT count FROM counters WHERE name='global_calls' AND day=?`)
      .get(today) as { count: number } | undefined;
    const after = afterRow?.count ?? 0;
    check(
      "evaluate judge error: refused 502",
      status === 502 && data.error === "judge_unavailable",
      JSON.stringify(data),
    );
    check(
      "evaluate judge error: reserved global slot released",
      after === before,
      `before=${before} after=${after}`,
    );
  }

  // 15. cache: same (model, content) is served from the submissions cache with
  // no new Poe call, and only one submission row is stored.
  STUB.turnstileOk = true;
  STUB.poeMode = "good";
  {
    const tk1 = tokenHex("cacheA");
    const tk2 = tokenHex("cacheB");
    insertToken({ token: tk1 });
    insertToken({ token: tk2 });
    const content = "A uniquely cacheable passage for the cache test.";
    const before = STUB.poeCalls.length;
    const r1 = await call(
      "/evaluate",
      { token: tk1, turnstileToken: "t", content, model: "gpt-5.2" },
      "10.20.0.1",
    );
    const afterFirst = STUB.poeCalls.length;
    const r2 = await call(
      "/evaluate",
      { token: tk2, turnstileToken: "t", content, model: "gpt-5.2" },
      "10.20.0.1",
    );
    const afterSecond = STUB.poeCalls.length;
    check(
      "cache: first call is a fresh Poe call",
      r1.data.ok === true && afterFirst === before + 1,
      `delta=${afterFirst - before}`,
    );
    check(
      "cache: second call served from cache (cached flag)",
      r2.data.ok === true && r2.data.cached === true,
      JSON.stringify(r2.data).slice(0, 160),
    );
    check(
      "cache: second call made NO new Poe call",
      afterSecond === afterFirst,
      `delta=${afterSecond - afterFirst}`,
    );
    check(
      "cache: cached scores match the fresh result",
      r2.data.overall === r1.data.overall && r2.data.composite === r1.data.composite && r2.data.dimensions.length === 7,
      JSON.stringify({ a: r1.data.overall, b: r2.data.overall, ca: r1.data.composite, cb: r2.data.composite }),
    );
    check(
      "cache: cached usage is zero cost from the cache source",
      r2.data.usage.call_cost_usd === 0 && r2.data.usage.token_source === "cache",
      JSON.stringify(r2.data.usage),
    );
    // Exactly one FRESH (cached = 0) row seeds the cache, and the repeat is
    // logged as its own cached (cached = 1, zero cost) request row.
    const freshRows = db
      .prepare(`SELECT count(*) AS n FROM submissions WHERE content = ? AND model = ? AND cached = 0`)
      .get(content, "gpt-5.2") as { n: number };
    const hitRows = db
      .prepare(`SELECT count(*) AS n FROM submissions WHERE content = ? AND model = ? AND cached = 1`)
      .get(content, "gpt-5.2") as { n: number };
    const hitCost = db
      .prepare(`SELECT cost_usd FROM submissions WHERE content = ? AND model = ? AND cached = 1 LIMIT 1`)
      .get(content, "gpt-5.2") as { cost_usd: number };
    check("cache: exactly one fresh submission row seeds the cache", freshRows.n === 1, `n=${freshRows.n}`);
    check("cache: the repeat is logged as a cached row", hitRows.n === 1, `n=${hitRows.n}`);
    check("cache: the cached log row records zero cost", hitCost.cost_usd === 0, JSON.stringify(hitCost));
    // The repeat must NOT decrement the repeating token's per-token daily quota.
    const tk2row = db
      .prepare(`SELECT used_today, cost_this_month FROM access_tokens WHERE token = ?`)
      .get(tk2) as { used_today: number; cost_this_month: number };
    check(
      "cache: repeat does NOT spend the token's daily quota or cost",
      tk2row.used_today === 0 && tk2row.cost_this_month === 0,
      JSON.stringify(tk2row),
    );
  }

  // 16. submission log: a fresh evaluation writes the scores and decision to the
  // private submissions table, with the token's email.
  STUB.poeMode = "good";
  {
    const tk = tokenHex("logrow");
    insertToken({ token: tk });
    const content = "A passage that will be logged with its scores.";
    await call(
      "/evaluate",
      { token: tk, turnstileToken: "t", content, model: "gpt-5.2" },
      "10.21.0.1",
    );
    const s = db
      .prepare(
        `SELECT email, model, overall, composite, decision, clarity, neutrality
           FROM submissions WHERE content = ?`,
      )
      .get(content) as any;
    check(
      "submission log: row written with email, model, and a decision",
      !!s && s.model === "gpt-5.2" && s.email === "user@example.com" && (s.decision === "good" || s.decision === "bad"),
      JSON.stringify(s),
    );
    check(
      "submission log: scores stored (overall 8, composite 7.27, dims present)",
      !!s && s.overall === 8 && Math.abs(s.composite - 7.27) < 1e-9 && s.clarity === 7 && s.neutrality === 7,
      JSON.stringify(s),
    );
  }

  // 17. cache hit bypasses ALL consuming guards: it must return the stored
  // result even when the per-token quota is exhausted, the monthly cost cap is
  // reached, and the global daily cap is zero, and make NO Poe call and spend
  // nothing. A fresh miss, by contrast, DOES consume the token's quota and cost.
  STUB.turnstileOk = true;
  STUB.poeMode = "good";
  {
    const ip = "10.30.0.1";
    const content = "A passage used to prime the cache for the bypass test.";
    // Prime the cache with one fresh evaluation (a cache miss).
    const primeTk = tokenHex("primeX");
    insertToken({ token: primeTk });
    const beforePrime = STUB.poeCalls.length;
    const rp = await call(
      "/evaluate",
      { token: primeTk, turnstileToken: "t", content, model: "gpt-5.2" },
      ip,
    );
    check(
      "cache bypass: prime is a fresh miss (Poe called, not cached)",
      rp.data.ok === true && rp.data.cached !== true && STUB.poeCalls.length === beforePrime + 1,
      JSON.stringify({ cached: rp.data.cached, delta: STUB.poeCalls.length - beforePrime }),
    );
    const primeRow = db
      .prepare(`SELECT used_today, cost_this_month FROM access_tokens WHERE token = ?`)
      .get(primeTk) as { used_today: number; cost_this_month: number };
    check(
      "cache bypass: the fresh miss DID consume quota and cost",
      primeRow.used_today === 1 && primeRow.cost_this_month > 0,
      JSON.stringify(primeRow),
    );

    // Now repeat the SAME passage and model with a token whose daily quota is
    // exhausted and whose monthly cost is over the cap, from the same IP, under
    // a zero global cap. Every consuming guard would reject a miss, so success
    // proves the cache hit bypasses all of them.
    const hitTk = tokenHex("hitX");
    insertToken({ token: hitTk, quota: 1, used_today: 1, cost_this_month: 999 });
    const beforeHit = STUB.poeCalls.length;
    const ipCountBefore = (db
      .prepare(`SELECT count FROM rate_limits WHERE key LIKE ?`)
      .get("eval_ip:" + ip + ":%") as { count: number } | undefined)?.count ?? 0;
    const rh = await call(
      "/evaluate",
      { token: hitTk, turnstileToken: "t", content, model: "gpt-5.2" },
      ip,
      baseEnv({ GLOBAL_DAILY_CALL_CAP: "0" }),
    );
    const ipCountAfter = (db
      .prepare(`SELECT count FROM rate_limits WHERE key LIKE ?`)
      .get("eval_ip:" + ip + ":%") as { count: number } | undefined)?.count ?? 0;
    check(
      "cache bypass: cached result returned despite exhausted quota, cost cap, and zero global cap",
      rh.data.ok === true && rh.data.cached === true,
      JSON.stringify(rh.data).slice(0, 160),
    );
    check(
      "cache bypass: no Poe call on the cache hit",
      STUB.poeCalls.length === beforeHit,
      `delta=${STUB.poeCalls.length - beforeHit}`,
    );
    const hitRow = db
      .prepare(`SELECT used_today, cost_this_month FROM access_tokens WHERE token = ?`)
      .get(hitTk) as { used_today: number; cost_this_month: number };
    check(
      "cache bypass: hit did NOT change the token's quota or cost",
      hitRow.used_today === 1 && hitRow.cost_this_month === 999,
      JSON.stringify(hitRow),
    );
    check(
      "cache bypass: hit did NOT consume a per-IP slot",
      ipCountAfter === ipCountBefore,
      `before=${ipCountBefore} after=${ipCountAfter}`,
    );
  }

  // 18. the model is part of the cache key: the same passage judged by a
  // DIFFERENT model must be a cache miss and make a fresh Poe call.
  STUB.turnstileOk = true;
  STUB.poeMode = "good";
  {
    const content = "A single passage that two different models will judge.";
    const tkA = tokenHex("mkeyA");
    const tkB = tokenHex("mkeyB");
    insertToken({ token: tkA });
    insertToken({ token: tkB });
    const before = STUB.poeCalls.length;
    const rA = await call(
      "/evaluate",
      { token: tkA, turnstileToken: "t", content, model: "gpt-5.2" },
      "10.31.0.1",
    );
    const afterA = STUB.poeCalls.length;
    const rB = await call(
      "/evaluate",
      { token: tkB, turnstileToken: "t", content, model: "gemini-2.5-flash" },
      "10.31.0.2",
    );
    const afterB = STUB.poeCalls.length;
    check(
      "model cache key: first model is a fresh call",
      rA.data.ok === true && afterA === before + 1,
      `delta=${afterA - before}`,
    );
    check(
      "model cache key: same passage, different model is NOT served from cache",
      rB.data.ok === true && rB.data.cached !== true && afterB === afterA + 1,
      JSON.stringify({ cached: rB.data.cached, delta: afterB - afterA }),
    );
    const rows = db
      .prepare(`SELECT model FROM submissions WHERE content = ? AND cached = 0 ORDER BY model`)
      .all(content) as { model: string }[];
    check(
      "model cache key: two distinct fresh rows stored, one per model",
      rows.length === 2 && rows[0].model === "gemini-2.5-flash" && rows[1].model === "gpt-5.2",
      JSON.stringify(rows),
    );
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

run().catch((e) => {
  console.error("harness crashed:", e);
  process.exit(1);
});
