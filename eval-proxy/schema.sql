-- eval-proxy D1 schema.
--
-- Apply with:
--   npx wrangler d1 execute eval-proxy-db --remote --file=./schema.sql
-- For local validation:
--   npx wrangler d1 execute eval-proxy-db --local --file=./schema.sql
--
-- Re-running is safe: all DDL uses IF NOT EXISTS.

-- Access tokens minted by /request-access and validated by /evaluate.
CREATE TABLE IF NOT EXISTS access_tokens (
  token           TEXT PRIMARY KEY,     -- 64 hex chars, random
  email           TEXT NOT NULL,        -- lowercased recipient
  quota_per_day   INTEGER NOT NULL,     -- per-token daily /evaluate quota
  used_today      INTEGER NOT NULL DEFAULT 0,  -- calls counted for `day`
  used_total      INTEGER NOT NULL DEFAULT 0,  -- lifetime calls
  day             TEXT NOT NULL,        -- UTC date (YYYY-MM-DD) used_today counts for
  cost_this_month REAL NOT NULL DEFAULT 0,     -- accumulated dollar cost for month_start
  month_start     TEXT NOT NULL,        -- UTC month (YYYY-MM) cost_this_month counts for
  revoked         INTEGER NOT NULL DEFAULT 0,  -- 1 disables the token
  created_at      TEXT NOT NULL,        -- ISO 8601 UTC
  last_used       TEXT                  -- ISO 8601 UTC of the last /evaluate call
);

CREATE INDEX IF NOT EXISTS idx_tokens_email   ON access_tokens(email);
CREATE INDEX IF NOT EXISTS idx_tokens_created ON access_tokens(created_at);

-- Global counters. Currently holds the hard global daily call cap counter under
-- name = 'global_calls'. `day` is the UTC date the count belongs to. When a new
-- day arrives the count is reset on the next increment (see worker.ts).
CREATE TABLE IF NOT EXISTS counters (
  name  TEXT PRIMARY KEY,
  day   TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0
);

-- Generic per-key rate-limit counters. Keys used:
--   ip:<ip>:<day>         per-IP daily cap on /request-access
--   eval_ip:<ip>:<day>    per-IP daily cap on /evaluate calls
-- window_start records when the key was first seen (for auditing). Keys embed
-- the calendar day, so a new day naturally starts a fresh key.
CREATE TABLE IF NOT EXISTS rate_limits (
  key          TEXT PRIMARY KEY,
  count        INTEGER NOT NULL DEFAULT 0,
  window_start TEXT NOT NULL
);

-- Private submission cache and request log. One row per /evaluate request. This
-- lives only in the operator's own D1, private to their Cloudflare account. It
-- serves two purposes:
--   1. cache: a repeat of the SAME (model, content_hash) returns the stored
--      scores without a new Poe call, so repeats cost nothing, and
--   2. log: a private record of every request (email, passage, scores), marking
--      whether it was served from cache and what it cost.
-- content_hash is the SHA-256 hex of the trimmed passage text. Fresh (cache
-- miss) rows carry cached = 0 and the real call cost. Cache-hit rows carry
-- cached = 1 and cost_usd = 0 (no Poe call was made). The cache lookup returns
-- the newest row for a (model, content_hash), so hit rows are safe duplicates of
-- the fresh scores.
CREATE TABLE IF NOT EXISTS submissions (
  id              TEXT PRIMARY KEY,     -- random submission id (uuid)
  created_at      TEXT NOT NULL,        -- ISO 8601 UTC
  email           TEXT,                 -- the access token's email, if known
  content         TEXT NOT NULL,        -- the evaluated passage
  content_hash    TEXT NOT NULL,        -- SHA-256 hex of the trimmed passage
  model           TEXT NOT NULL,        -- chosen judge model
  clarity         REAL,
  neutrality      REAL,
  verifiability   REAL,
  coverage        REAL,
  structure       REAL,
  readability     REAL,
  informativeness REAL,
  overall         REAL NOT NULL,        -- direct holistic overall score
  composite       REAL NOT NULL,        -- refined fitted composite
  decision        TEXT NOT NULL,        -- 'good' or 'bad'
  cached          INTEGER NOT NULL DEFAULT 0,  -- 1 if served from cache, 0 if fresh
  cost_usd        REAL NOT NULL DEFAULT 0      -- dollar cost of this request (0 on a cache hit)
);

-- Migration for an existing database that predates the cached / cost_usd
-- columns. SQLite has no ADD COLUMN IF NOT EXISTS, so run these two once by hand
-- if your submissions table was created before this revision (they error
-- harmlessly if the columns already exist):
--   ALTER TABLE submissions ADD COLUMN cached INTEGER NOT NULL DEFAULT 0;
--   ALTER TABLE submissions ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0;

-- The cache lookup key: newest row for a given model and content hash.
CREATE INDEX IF NOT EXISTS idx_submissions_lookup  ON submissions(model, content_hash);
CREATE INDEX IF NOT EXISTS idx_submissions_created ON submissions(created_at);
