# eval-proxy

A Cloudflare Worker that powers a live demo of the content quality evaluation
study. It holds the privileged Poe, Resend, and Turnstile keys server-side so
the static frontend can stay static, and it bounds spend with layered guards
backed by a D1 database.

This is SEPARATE infrastructure from the Python study pipeline in this repo. It
shares no state with `src/`, `scripts/`, `configs/`, `data/`, or the study
`docs/`. All study knowledge the demo needs (rubric, per-model prompts,
composite weights, decision threshold, price table, caps) lives in one file:
`src/rubric-config.ts`. Those values are now the FINAL study values, aligned to
`configs/config.yaml`, `configs/prices.yaml`, `src/rubric.py`,
`prompts/judge/<model>/<version>.txt`, `outputs/best_prompt_versions.json`, and
`outputs/dimension_reduction.json`. They are no longer placeholders.

## What it does

Two endpoints, both gated by a Cloudflare Turnstile human check verified
server-side:

- `POST /request-access` takes an email and a Turnstile token, verifies the
  challenge, rate-limits per email and per IP, mints a random access token,
  stores it in D1, and emails it to the user via Resend. It always returns the
  same neutral success ("if that email is valid, a token is on its way") so it
  never leaks which emails exist.
- `POST /evaluate` takes an access token, a Turnstile token, the content text,
  and one chosen model of `gpt-5.2`, `claude-haiku-4.5`, `gemini-2.5-flash`,
  `perplexity-sonar` (the study's four judges). It runs every guard below, then
  calls the Poe OpenAI-compatible endpoint with that model's selected best judge
  prompt and the rubric, parses the JSON (with a repair fallback), and returns
  the reasoning, all seven per-dimension scores, the direct overall score, the
  refined fitted composite score, and a good or bad decision. The decision is
  driven by the DIRECT overall score at or above the threshold (6), matching the
  study's good/bad metric. The composite is returned for transparency and does
  not change the decision.

A `GET /health` liveness endpoint is also provided.

### Checks on `/evaluate`

The cheap, non-consuming checks run first, then the per-`(model, content_hash)`
cache is consulted, and ONLY a cache miss goes on to the consuming guards and
the Poe call. A repeat of the exact same passage with the same model is always
returned for free and never counts against any limit.

Before the cache lookup (order preserved):

1. Valid Turnstile token.
2. Access token exists and is not revoked.
3. Input validation: known model (one of the four), non-empty content within
   `MAX_INPUT_CHARS`.

Cache lookup by `(model, content_hash)`:

- HIT: return the stored result (`cached: true`) with zero cost and NO Poe call.
  It does NOT spend the per-token daily quota, a per-IP slot, the monthly cost,
  or a global slot. The request is still logged (`cached = 1`, cost 0). The model
  is part of the key, so the same passage judged by a different model misses.

Only on a cache MISS, the consuming guards run in order, then Poe is called:

4. Per-token daily quota not exceeded.
5. Per-IP daily call cap (default 3 per IP per day), to deter someone cycling
   tokens or emails from one address. Only fresh (non-cached) evaluations count.
6. Per-user MONTHLY cost cap not reached. If it is, the request is refused
   WITHOUT calling Poe and returns a warm collective message plus a support
   link (see `SUPPORT_URL`).
7. Hard GLOBAL daily call cap. A slot is reserved before the call so total
   spend is bounded even under concurrency. If the call fails, the slot is
   released.

If any guard trips, the Worker refuses with a clear message and does not call
Poe. The fresh result is then logged (`cached = 0`, real cost).

### Cost accounting

Each successful call's dollar cost is computed from the response token usage
and a per-model price table:

```
cost = input_tokens * input_price + output_tokens * output_price
```

Prices are dollars per 1,000,000 tokens, sourced from the study's
`configs/prices.yaml` (the single source of truth) and mirrored in
`src/rubric-config.ts` (`PRICE_TABLE`). Note that Poe bills in its own compute
points, so these dollar prices are documented planning approximations. If the
response carries no usage block, tokens are estimated from text length.

Cost accumulates per access token over a rolling CALENDAR MONTH
(`cost_this_month`, reset when `month_start` rolls over). The per-user monthly
cost cap (`MONTHLY_COST_CAP_USD`, default 5.00) is the soft budget per user. The
global daily call cap is the hard backstop for the whole demo.

## D1 schema

`schema.sql` defines four tables:

- `access_tokens(token, email, quota_per_day, used_today, used_total, day,
  cost_this_month, month_start, revoked, created_at, last_used)` one row per
  minted token. `day` and `month_start` drive the daily-quota and monthly-cost
  rollovers.
- `counters(name, day, count)` holds the global daily call counter under
  `name = 'global_calls'`.
- `rate_limits(key, count, window_start)` per-key counters. Keys are
  `ip:<ip>:<day>` for the per-IP `/request-access` cap and `eval_ip:<ip>:<day>`
  for the per-IP `/evaluate` cap. The calendar day is embedded in the key, so a
  new day starts fresh.
- `submissions(id, created_at, email, content, content_hash, model, <seven
  dimensions>, overall, composite, decision, cached, cost_usd)` the private
  per-`(model, content_hash)` cache and request log. Fresh rows carry
  `cached = 0` and the real cost, cache-hit rows carry `cached = 1` and
  `cost_usd = 0`. The cache lookup returns the newest row for a
  `(model, content_hash)`. If you have an older database that predates the
  `cached` and `cost_usd` columns, run the two `ALTER TABLE` statements noted at
  the bottom of `schema.sql` once.

## Where secrets come from

Nothing secret is hardcoded. The Worker reads everything from its env:

- Secrets (via `wrangler secret put`): `POE_API_KEY`, `RESEND_API_KEY`,
  `TURNSTILE_SECRET_KEY`.
- Vars (in `wrangler.toml [vars]`): `SITE_NAME`, `FROM_ADDR`,
  `ALLOWED_ORIGINS`, `TURNSTILE_SITE_KEY` (public), `SUPPORT_URL`.
- Optional numeric overrides (vars): `MAX_INPUT_CHARS`,
  `PER_TOKEN_DAILY_QUOTA`, `GLOBAL_DAILY_CALL_CAP`, `EVAL_IP_DAILY_CAP`,
  `MONTHLY_COST_CAP_USD`, `IP_DAILY_REQUEST_CAP`, `EMAIL_COOLDOWN_HOURS`,
  `DEFAULT_TOKEN_QUOTA_PER_DAY`. If unset, the defaults in
  `src/rubric-config.ts` (`LIMIT_DEFAULTS`) apply.
- Optional endpoint bases (vars): `TURNSTILE_VERIFY_URL`, `RESEND_API_BASE`,
  `POE_BASE_URL`. Leave unset in production. They exist so local validation can
  point at a stub and hit no network.

## Study config (final)

All study values live in `src/rubric-config.ts` and are final:

- `MODELS` the four study judges: `gpt-5.2`, `claude-haiku-4.5`,
  `gemini-2.5-flash`, `perplexity-sonar`.
- `MODEL_SYSTEM_PROMPTS` each model's selected best prompt version, embedded
  verbatim and reconstructed with the study's own prompt renderer
  (`src/prompt_tuning.py`): gpt-5.2 v1, claude-haiku-4.5 v3, gemini-2.5-flash
  v3, perplexity-sonar v2 (confirmed against `outputs/best_prompt_versions.json`).
- `DIMENSIONS`, `DIMENSION_HELP`, `DIMENSION_ENDPOINTS` the seven rubric
  dimensions and anchors, copied from `src/rubric.py`.
- `COMPOSITE_DIMENSIONS`, `COMPOSITE_WEIGHTS`, `COMPOSITE_INTERCEPT` the study's
  refined fitted composite over `{neutrality, verifiability, coverage,
  readability}` with the fitted weights and intercept from
  `outputs/dimension_reduction.json` (`refined_rubric`). The three dropped
  dimensions (clarity, structure, informativeness) carry weight 0.
- `GOOD_THRESHOLD` = 6.0 (`analysis.good_threshold`), applied to the DIRECT
  overall score (`DECISION_FIELD = "overall"`), which is the value the study
  thresholds for its good/bad precision, recall, and F1.
- `PRICE_TABLE` the four models' input/output prices from `configs/prices.yaml`.

Deployment-specific values you still set for your own account:

- `SUPPORT_URL` your support or donation page (or set the `SUPPORT_URL` var).
- `LIMIT_DEFAULTS` the caps and quotas (each overridable with a wrangler var).

## Deploy runbook

Every step below requires YOUR OWN Cloudflare, Resend, and Turnstile accounts
and YOUR OWN keys. This project neither ships nor requires any real secret. The
config values (models, prompts, weights, threshold, prices) are already final,
so deploying is only account setup plus your own keys.

`deploy.sh` automates the scriptable, non-secret steps and refuses to run
anything that would spend money or need your credentials on your behalf. It
does NOT log you in and does NOT deploy until you have logged in yourself. It
contains no secret values.

### Manual account actions you must do yourself (before running deploy.sh)

These cannot be scripted safely because they involve your accounts and secrets:

1. Log in to Cloudflare in this shell:
   ```
   npx wrangler login
   ```
2. Turnstile: in the Cloudflare dashboard create a Turnstile site for your
   frontend domain. Note the SITE KEY (public) and the SECRET KEY.
3. Resend: create a Resend account, add and VERIFY your sending domain (DNS
   records), and create a Resend API key. Pick a `FROM_ADDR` on that verified
   domain, for example `Content Quality Demo <demo@yourdomain.com>`.
4. Have your `POE_API_KEY` ready (from your Poe account).
5. Edit the non-secret vars in `wrangler.toml`:
   - `SITE_NAME`, `FROM_ADDR`, `ALLOWED_ORIGINS` (comma separated, for example
     your GitHub Pages origin), `TURNSTILE_SITE_KEY` (the public site key),
     `SUPPORT_URL`.
   - Optionally set the cost and rate caps: `MONTHLY_COST_CAP_USD` (default
     5.00), `GLOBAL_DAILY_CALL_CAP`, `EVAL_IP_DAILY_CAP` (default 3),
     `PER_TOKEN_DAILY_QUOTA`, `MAX_INPUT_CHARS`.

### Scripted steps (deploy.sh)

Once you are logged in and the vars are set, run:
```
./deploy.sh
```
It will, in order:
- check `npx wrangler whoami` and STOP with instructions if you are not logged
  in (it never logs in for you),
- run `npm install` if `node_modules` is missing,
- create the D1 database `eval-proxy-db` only if it does not already exist, and
  print the `database_id` for you to paste into `wrangler.toml` if the
  placeholder is still there (it will pause until you have done so),
- apply `schema.sql` to the remote D1 (safe to re-run, all DDL is IF NOT
  EXISTS),
- echo the exact `wrangler secret put` commands you must run yourself (it does
  NOT read, store, or pass any secret),
- run `npx wrangler deploy` and print the Worker URL.

Pass `--dry-run` to print every command without executing anything.

### After deploy.sh

6. Set the three secrets (you will be prompted to paste each value; nothing is
   stored by the script):
   ```
   npx wrangler secret put POE_API_KEY
   npx wrangler secret put RESEND_API_KEY
   npx wrangler secret put TURNSTILE_SECRET_KEY
   ```
   If you set them before the first deploy, deploy again so the running Worker
   picks them up (`npx wrangler deploy`).
7. Point the frontend at the Worker. Open `public/index.html`, set
   `WORKER_BASE_URL` to the deployed URL (no trailing slash) and
   `TURNSTILE_SITE_KEY` to your site key. Host the page anywhere static (for
   example GitHub Pages) on an origin listed in `ALLOWED_ORIGINS`.

## Frontend

`public/index.html` is a self-contained page (no framework). Part A requests an
access token by email with a Turnstile widget. Part B is the evaluator: paste
content, pick one of the four models, enter the token, and submit. It renders
the reasoning, the dimension scores, the composite, and the good or bad
decision. When the monthly cost cap is reached it shows the warm message and a
Support the project button pointing at `SUPPORT_URL`. Fill in `WORKER_BASE_URL`
and `TURNSTILE_SITE_KEY` at the top after you deploy.

## Local development and validation

Run the Worker locally with a local D1 (miniflare under the hood):

```
npm install
npx wrangler d1 execute eval-proxy-db --local --file=./schema.sql
npx wrangler dev
```

`wrangler dev` serves at `http://localhost:8787`. To exercise it without any
real network or cost, set `TURNSTILE_VERIFY_URL`, `RESEND_API_BASE`, and
`POE_BASE_URL` to a local stub server (for example via a `.dev.vars` file) so
the three outbound calls hit your stub instead of the real services.

Offline test suite (no network, no cost, no wrangler needed):

```
npm run validate
```

This drives the REAL `worker.fetch` handler against a real in-memory SQLite
database loaded from `schema.sql`, with a stubbed `fetch` that fakes Turnstile,
Resend, and Poe. It covers: request-access (token minted and email stub
called), request-access with a failing Turnstile, the per-email cooldown, the
evaluate happy path (parsed scores from a stubbed Poe reply), the JSON-repair
fallback, every guard (bad token, missing Turnstile, bad model, oversized
input, per-token daily quota, per-IP daily cap, monthly cost cap, global daily
cap, and a judge error releasing the reserved global slot), and the cache: a
repeat of the same `(model, content)` is served from the cache with no Poe call
and does NOT spend the per-token quota, per-IP slot, monthly cost, or global cap
(while a fresh miss does), and the model is part of the cache key so the same
passage judged by a different model misses.

Typecheck the Worker source:

```
npm run typecheck
```
